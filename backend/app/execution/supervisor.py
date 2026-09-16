from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from algorithm.runtime_profile import is_remote_only

from .resources import GpuInventory, configured_gpu_ids


LOGGER = logging.getLogger("qasystem.supervisor")
CPU_RESOURCE_CLASSES = "cpu.document_orchestrator,cpu.chunk,cpu.graph,cpu.chart,io.llm,io.db,io.graph,workflow.knowledge"


@dataclass(slots=True)
class ChildProcess:
    name: str
    process: subprocess.Popen


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int = 256) -> int:
    try:
        return min(max(int(os.getenv(name, str(default))), minimum), maximum)
    except (TypeError, ValueError):
        return default


def _gpu_ids() -> list[str]:
    if is_remote_only():
        return []
    configured = configured_gpu_ids()
    if configured:
        return configured
    return [item.physical_index for item in GpuInventory(cache_seconds=0).snapshots(force=True)]


def _auto_cpu_workers(cpu_count: int) -> int:
    configured = os.getenv("EXECUTION_CPU_WORKERS", "auto").strip().lower()
    if configured != "auto":
        return _env_int("EXECUTION_CPU_WORKERS", 2, minimum=1, maximum=32)
    if is_remote_only():
        return 1
    return max(2, min(8, cpu_count // 4))


def _auto_vlm_workers(cpu_count: int, gpu_count: int) -> int:
    if gpu_count <= 0:
        return 0
    configured = os.getenv("EXECUTION_VLM_WORKERS_PER_GPU", "auto").strip().lower()
    if configured != "auto":
        return _env_int("EXECUTION_VLM_WORKERS_PER_GPU", 2, minimum=1, maximum=8)
    cores_per_gpu = max(1, cpu_count // max(gpu_count, 1))
    return max(1, min(4, cores_per_gpu // 4))


class RuntimeSupervisor:
    def __init__(self) -> None:
        self.children: list[ChildProcess] = []
        self.engines: list[tuple[str, int, subprocess.Popen]] = []
        self.stopping = False
        self.gpu_ids = _gpu_ids()
        self.cpu_count = os.cpu_count() or 4
        self.base_port = _env_int("VLLM_BASE_PORT", 8100, minimum=1024, maximum=65000)
        self.ready_file = Path(os.getenv("EXECUTION_RUNTIME_READY_FILE", "/tmp/qasystem-runtime-ready"))
        self.ready_file.unlink(missing_ok=True)

    def stop(self, *_args) -> None:
        self.stopping = True

    def _spawn(self, name: str, command: list[str], *, env: dict[str, str] | None = None) -> subprocess.Popen:
        LOGGER.info("starting %s: %s", name, shlex.join(command))
        process = subprocess.Popen(command, env=env or os.environ.copy(), start_new_session=True)
        self.children.append(ChildProcess(name, process))
        return process

    def _engine_command(self, port: int) -> list[str]:
        command = [
            sys.executable,
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--model",
            os.getenv("VLLM_MODEL_PATH", "/models/mineru/vlm"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--served-model-name",
            os.getenv("VLLM_SERVED_MODEL_NAME", "mineru-vlm"),
            "--gpu-memory-utilization",
            os.getenv("VLLM_GPU_MEMORY_UTILIZATION", "0.82"),
            "--max-num-seqs",
            os.getenv("VLLM_MAX_NUM_SEQS", "32"),
        ]
        command.extend(shlex.split(os.getenv("VLLM_EXTRA_ARGS", "")))
        return command

    def _start_engines(self) -> None:
        if is_remote_only():
            LOGGER.info("remote-only profile: skip local vLLM engines")
            return
        if not self.gpu_ids or os.getenv("VLLM_ENABLED", "true").lower() in {"0", "false", "off", "no"}:
            LOGGER.warning("no vLLM engine started; detected GPUs=%s", self.gpu_ids)
            return
        for index, gpu_id in enumerate(self.gpu_ids):
            port = self.base_port + index
            env = os.environ.copy()
            env.update({"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": gpu_id})
            process = self._spawn(f"vllm-gpu-{gpu_id}", self._engine_command(port), env=env)
            self.engines.append((gpu_id, port, process))

        with ThreadPoolExecutor(max_workers=len(self.engines)) as executor:
            futures = [executor.submit(self._wait_until_warm, gpu_id, port, process) for gpu_id, port, process in self.engines]
            for future in futures:
                future.result()

    def _wait_until_warm(self, gpu_id: str, port: int, process: subprocess.Popen) -> None:
        deadline = time.monotonic() + float(os.getenv("VLLM_STARTUP_TIMEOUT_SECONDS", "900"))
        readiness_script = Path(os.getenv("VLLM_READINESS_SCRIPT", "/usr/local/bin/vllm_readiness.py"))
        while time.monotonic() < deadline and not self.stopping:
            exit_code = process.poll()
            if exit_code is not None:
                raise RuntimeError(f"vLLM engine for GPU {gpu_id} exited with code {exit_code}")
            env = os.environ.copy()
            env.update(
                {
                    "VLLM_WARMUP_BASE_URL": f"http://127.0.0.1:{port}",
                    "VLLM_WARMUP_MARKER": f"/tmp/qasystem-vllm-{port}.done",
                    "VLLM_SERVER_PID": str(process.pid),
                    "VLLM_WARMUP_REPORT_FAILURE": "false",
                }
            )
            try:
                result = subprocess.run(
                    [sys.executable, str(readiness_script)],
                    env=env,
                    check=False,
                    timeout=float(os.getenv("VLLM_WARMUP_TIMEOUT_SECONDS", "300")) + 15,
                )
            except subprocess.TimeoutExpired:
                result = None
            if result is not None and result.returncode == 0:
                LOGGER.info("vLLM GPU %s ready and warm on port %s", gpu_id, port)
                return
            time.sleep(2)
        raise RuntimeError(f"vLLM engine for GPU {gpu_id} did not become ready")

    def _worker_env(
        self,
        gpu_id: str,
        port: int,
        worker_id: str,
        *,
        model: str,
        vlm_endpoint: bool,
    ) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
                "CUDA_VISIBLE_DEVICES": gpu_id,
                "EXECUTION_GPU_DEVICE": gpu_id,
                "EXECUTION_GPU_DEVICE_IDS": gpu_id,
                "EXECUTION_WORKER_ID": worker_id,
                "EXECUTION_GPU_LEASE_MODE": "external",
                "EXECUTION_MODEL": model,
            }
        )
        if vlm_endpoint:
            env.update(
                {
                    "MINERU_VLM_BACKEND": "vlm-http-client",
                    "MINERU_VLM_SERVER_URL": f"http://127.0.0.1:{port}",
                }
            )
        else:
            env.pop("MINERU_VLM_SERVER_URL", None)
        return env

    def _start_core_workers(self) -> None:
        api_workers = _env_int("API_WORKERS", 1 if is_remote_only() else 2, minimum=1, maximum=16)
        vlm_endpoints = [f"http://127.0.0.1:{self.base_port + index}" for index in range(len(self.gpu_ids))]
        api_env = os.environ.copy()
        if vlm_endpoints:
            api_env["MINERU_VLM_BACKEND"] = "vlm-http-client"
            api_env["MINERU_VLM_SERVER_URL"] = vlm_endpoints[0]
        self._spawn(
            "api",
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", str(api_workers)],
            env=api_env,
        )
        parse_gpus = [token.strip() for token in os.getenv("DOCPARSE_GPU_IDS", "4,5,6,7").split(",") if token.strip()]
        for index in range(_auto_cpu_workers(self.cpu_count)):
            env = os.environ.copy()
            env["EXECUTION_WORKER_ID"] = f"cpu-{index}"
            if parse_gpus:
                env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
                env["CUDA_VISIBLE_DEVICES"] = parse_gpus[index % len(parse_gpus)]
            if vlm_endpoints:
                env["MINERU_VLM_BACKEND"] = "vlm-http-client"
                env["MINERU_VLM_SERVER_URL"] = vlm_endpoints[index % len(vlm_endpoints)]
            self._spawn(
                f"cpu-worker-{index}",
                [sys.executable, "-m", "app.execution.worker", "--resource-class", CPU_RESOURCE_CLASSES],
                env=env,
            )

    def _start_gpu_workers(self) -> None:
        vlm_workers = _auto_vlm_workers(self.cpu_count, len(self.engines))
        auxiliary_workers = _env_int("EXECUTION_AUX_WORKERS_PER_GPU", 1, minimum=0, maximum=4)
        for gpu_id, port, _engine in self.engines:
            for index in range(vlm_workers):
                worker_id = f"vlm-{gpu_id}-{index}"
                self._spawn(
                    worker_id,
                    [sys.executable, "-m", "app.execution.worker", "--resource-class", "gpu.vlm", "--gpu-device", gpu_id],
                    env=self._worker_env(
                        gpu_id,
                        port,
                        worker_id,
                        model="mineru-vlm",
                        vlm_endpoint=True,
                    ),
                )
            for index in range(auxiliary_workers):
                worker_id = f"aux-{gpu_id}-{index}"
                self._spawn(
                    worker_id,
                    [sys.executable, "-m", "app.execution.worker", "--resource-class", "gpu.embedding,gpu.confidence", "--gpu-device", gpu_id],
                    env=self._worker_env(
                        gpu_id,
                        port,
                        worker_id,
                        model="embedding-confidence",
                        vlm_endpoint=False,
                    ),
                )
        LOGGER.info(
            "runtime ready: gpus=%s cpu_workers=%s vlm_workers_per_gpu=%s auxiliary_workers_per_gpu=%s",
            self.gpu_ids,
            _auto_cpu_workers(self.cpu_count),
            vlm_workers,
            auxiliary_workers,
        )
        self.ready_file.write_text("ready\n", encoding="ascii")

    def _terminate(self) -> None:
        for child in reversed(self.children):
            if child.process.poll() is None:
                child.process.terminate()
        deadline = time.monotonic() + 20
        for child in reversed(self.children):
            remaining = max(0.1, deadline - time.monotonic())
            try:
                child.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                child.process.kill()

    def run(self) -> int:
        try:
            self._start_core_workers()
            self._start_engines()
            self._start_gpu_workers()
            while not self.stopping:
                for child in self.children:
                    exit_code = child.process.poll()
                    if exit_code is not None:
                        LOGGER.error("%s exited unexpectedly with code %s", child.name, exit_code)
                        return exit_code or 1
                time.sleep(1)
            return 0
        finally:
            self.ready_file.unlink(missing_ok=True)
            self._terminate()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("EXECUTION_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    supervisor = RuntimeSupervisor()
    signal.signal(signal.SIGTERM, supervisor.stop)
    signal.signal(signal.SIGINT, supervisor.stop)
    raise SystemExit(supervisor.run())


if __name__ == "__main__":
    main()
