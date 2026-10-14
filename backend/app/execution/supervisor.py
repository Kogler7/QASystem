from __future__ import annotations
from qasystem_config.configuration import getenv as config_getenv
from qasystem_config.startup_contract import runtime_ready_file

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

from .resources import GpuInventory
from .process_control import ControlSocket, ProcessController, CONTROL_SOCKET_PATH


LOGGER = logging.getLogger("qasystem.supervisor")
CPU_RESOURCE_CLASSES = "cpu.document_orchestrator,cpu.chunk,cpu.graph,cpu.chart,io.llm,io.db,io.graph,workflow.knowledge"


@dataclass(slots=True)
class ChildProcess:
    name: str
    process: subprocess.Popen


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int = 256) -> int:
    try:
        return min(max(int(config_getenv(name, str(default))), minimum), maximum)
    except (TypeError, ValueError):
        return default


def _gpu_ids() -> list[str]:
    if is_remote_only():
        return []
    return [item.physical_index for item in GpuInventory(cache_seconds=0).snapshots(force=True)]


def _auto_cpu_workers(cpu_count: int, configured: str) -> int:
    configured = configured.strip().lower()
    if configured != "auto":
        if not configured.isdigit() or not 1 <= int(configured) <= 32:
            raise ValueError('CPU 进程数必须为 auto 或 1–32')
        return int(configured)
    if is_remote_only():
        return 1
    return max(2, min(8, cpu_count // 4))


def _auto_vlm_workers(cpu_count: int, gpu_count: int) -> int:
    if gpu_count <= 0:
        return 0
    configured = config_getenv("EXECUTION_VLM_WORKERS_PER_GPU", "auto").strip().lower()
    if configured != "auto":
        return _env_int("EXECUTION_VLM_WORKERS_PER_GPU", 2, minimum=1, maximum=8)
    cores_per_gpu = max(1, cpu_count // max(gpu_count, 1))
    return max(1, min(4, cores_per_gpu // 4))


class RuntimeSupervisor:
    def __init__(self, *, ready_file: Path | None = None, cpu_workers: str | None = None) -> None:
        self.children: list[ChildProcess] = []
        self.engines: list[tuple[str, int, subprocess.Popen]] = []
        self.stopping = False
        self.gpu_ids = _gpu_ids()
        self.cpu_count = os.cpu_count() or 4
        from qasystem_config.configuration import approved_execution_plan
        if cpu_workers is None:
            self.cpu_plan = approved_execution_plan()
        else:
            from qasystem_config.configuration import read_registry, should_load_local_dotenv
            if not should_load_local_dotenv():
                raise ValueError('受管部署不能通过启动参数覆盖 CPU 配置')
            _, revision = read_registry(with_revision=True)
            if revision is not None:
                raise ValueError('受管部署不能通过启动参数覆盖 CPU 配置')
            self.cpu_plan = {'revision': None, 'cpu_workers': str(cpu_workers)}
        self.cpu_worker_count = _auto_cpu_workers(self.cpu_count, self.cpu_plan['cpu_workers'])
        from qasystem_config.model_registry import worker_record
        self.model_record = worker_record()
        from qasystem_config.model_registry import physical_gpu
        selected_gpu = physical_gpu(self.model_record or {})
        if selected_gpu:
            if selected_gpu not in self.gpu_ids: raise ValueError('MinerU 指定GPU不可用，拒绝回退')
            self.gpu_ids = [selected_gpu]
        self.base_port = int((self.model_record or {}).get('port', '8100'))
        self.ready_file = Path(ready_file) if ready_file is not None else runtime_ready_file()
        self.ready_file.unlink(missing_ok=True)
        self.cpu_controller = ProcessController(authorize=self._approved_cpu_count, on_event=self._record_cpu_control)
        self.control_socket = None

    def _approved_cpu_count(self, role: str, revision: int) -> int:
        from qasystem_config.configuration import approved_execution_plan
        plan = approved_execution_plan(revision=revision)
        configured = plan['cpu_workers']
        if configured == 'auto':
            return 1 if is_remote_only() else max(2, min(8, self.cpu_count // 4))
        return int(configured)

    def _record_cpu_control(self, event):
        if event.get('revision') is None:
            return
        from qasystem_config.config_database import append_event
        directory = Path('/app/runtime-config/observed')
        if not directory.is_dir() or not os.access(directory, os.W_OK):
            raise ValueError('进程应用记录目录不可写')
        append_event(directory, {'event': 'process-control', 'approved_revision': event['revision'], **event})

    def stop(self, *_args) -> None:
        self.stopping = True

    def _spawn(self, name: str, command: list[str], *, env: dict[str, str] | None = None, supervised: bool = True) -> subprocess.Popen:
        LOGGER.info("starting %s: %s", name, shlex.join(command))
        process = subprocess.Popen(command, env=env or os.environ.copy(), start_new_session=True)
        if supervised:
            self.children.append(ChildProcess(name, process))
        return process

    def _engine_command(self, port: int) -> list[str]:
        command = [
            sys.executable,
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--model",
            '/models/' + self.model_record['model_dir'],
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--served-model-name",
            self.model_record.get('engine_options', {}).get('served_model', 'mineru-vlm'),
            "--gpu-memory-utilization",
            self.model_record['utilization'],
            "--max-num-seqs",
            self.model_record['max_num_seqs'],
        ]
        command.extend(shlex.split(self.model_record.get('engine_options', {}).get('extra_args', '')))
        return command

    def _planned_engine_ports(self) -> list[int]:
        """与 _start_engines 同一判定：本次运行是否会真正拉起本地 vLLM 引擎。

        核心 worker 的 VLM 端点注入必须以这里为准——引擎不启动时绝不能
        注入指向空地址的 URL，否则解析选中 vlm-http-client 后必然连接失败，
        且会因「URL 已配置」跳过本地 MinerU 4.x 的自动回退。
        """
        if is_remote_only():
            return []
        if not self.gpu_ids or not self.model_record or not self.model_record.get('enabled'):
            return []
        return [self.base_port + index for index in range(len(self.gpu_ids))]

    def _start_engines(self) -> None:
        if is_remote_only():
            LOGGER.info("remote-only profile: skip local vLLM engines")
            return
        if self.engines:
            raise RuntimeError('解析引擎已启动，拒绝重复启动')
        ports = self._planned_engine_ports()
        if not ports:
            LOGGER.warning("no vLLM engine started; detected GPUs=%s", self.gpu_ids)
            return
        from qasystem_config.model_registry import validate_record
        validate_record('mineru-vlm', self.model_record, root=Path('/models'), check_files=True)
        for index, gpu_id in enumerate(self.gpu_ids):
            port = ports[index]
            env = os.environ.copy()
            env.update({"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": gpu_id})
            process = self._spawn(f"vllm-gpu-{gpu_id}", self._engine_command(port), env=env)
            self.engines.append((gpu_id, port, process))

        with ThreadPoolExecutor(max_workers=len(self.engines)) as executor:
            futures = [executor.submit(self._wait_until_warm, gpu_id, port, process) for gpu_id, port, process in self.engines]
            for future in futures:
                future.result()

    def _wait_until_warm(self, gpu_id: str, port: int, process: subprocess.Popen) -> None:
        deadline = time.monotonic() + float(config_getenv("VLLM_STARTUP_TIMEOUT_SECONDS", "900"))
        readiness_script = Path(config_getenv("VLLM_READINESS_SCRIPT", "/usr/local/bin/vllm_readiness.py"))
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
                    timeout=float(config_getenv("VLLM_WARMUP_TIMEOUT_SECONDS", "300")) + 15,
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
        if 'cpu' in self.cpu_controller.roles:
            raise RuntimeError('核心执行进程已启动，拒绝重复启动网关或 CPU worker')
        api_workers = _env_int("API_WORKERS", 1 if is_remote_only() else 2, minimum=1, maximum=16)
        # 仅当 vLLM 引擎计划启动时才注入 VLM 端点（与 _start_engines 同一判定）；
        # 引擎不启动时不得注入指向空地址的 URL，否则解析选中 vlm-http-client
        # 后必然连接失败，且会因「URL 已配置」跳过本地 MinerU 4.x 自动回退。
        vlm_endpoints = [
            f"http://127.0.0.1:{port}" for port in self._planned_engine_ports()
        ]
        api_env = os.environ.copy()
        if vlm_endpoints:
            api_env["MINERU_VLM_BACKEND"] = "vlm-http-client"
            api_env["MINERU_VLM_SERVER_URL"] = vlm_endpoints[0]
        self._spawn(
            "api",
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", str(api_workers)],
            env=api_env,
        )
        self.cpu_controller.register('cpu', self._spawn_cpu_worker, self.cpu_worker_count)
        self.cpu_controller.roles['cpu']['planned_revision'] = self.cpu_plan['revision']
        self.cpu_controller.roles['cpu']['applied_revision'] = self.cpu_plan['revision']
        if self.cpu_plan['revision'] is not None:
            self._record_cpu_control({'revision': self.cpu_plan['revision'], 'role': 'cpu',
                'action': 'startup', 'state': 'applied', 'processes': self.cpu_controller.status()['cpu']['processes']})

    def _spawn_cpu_worker(self, name: str) -> subprocess.Popen:
        index = int(name.split('-')[1])
        vlm_endpoints = [f'http://127.0.0.1:{port}' for port in self._planned_engine_ports()]
        env = os.environ.copy()
        env["EXECUTION_WORKER_ID"] = name
        if vlm_endpoints:
            env["MINERU_VLM_BACKEND"] = "vlm-http-client"
            env["MINERU_VLM_SERVER_URL"] = vlm_endpoints[index % len(vlm_endpoints)]
        process = self._spawn(
            f"cpu-worker-{index}",
            [sys.executable, "-m", "app.execution.worker", "--resource-class", CPU_RESOURCE_CLASSES],
            env=env,
            supervised=False,
        )
        return process

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
            self.cpu_worker_count,
            vlm_workers,
            auxiliary_workers,
        )
        self.ready_file.write_text("ready\n", encoding="ascii")

    def _terminate(self) -> None:
        cpu_processes = self.cpu_controller.begin_shutdown()
        self.children.extend(ChildProcess('cpu-worker', process) for process in cpu_processes)
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
            self.control_socket = ControlSocket(CONTROL_SOCKET_PATH, self.cpu_controller)
            self.control_socket.start()
            failed_cpu_pids = set()
            while not self.stopping:
                for process in self.cpu_controller.status()['cpu']['processes']:
                    if process['state'] == 'failed' and process['pid'] not in failed_cpu_pids:
                        failed_cpu_pids.add(process['pid'])
                        LOGGER.error('CPU worker exited: name=%s pid=%s; gateway remains running; use ops runtime apply', process['name'], process['pid'])
                for child in self.children:
                    exit_code = child.process.poll()
                    if exit_code is not None:
                        LOGGER.error("%s exited unexpectedly with code %s", child.name, exit_code)
                        return exit_code or 1
                time.sleep(1)
            return 0
        finally:
            self.cpu_controller.begin_shutdown()
            if self.control_socket is not None and hasattr(self.control_socket, 'thread'):
                self.control_socket.close()
            self.ready_file.unlink(missing_ok=True)
            self._terminate()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description='QASystem Backend runtime supervisor')
    parser.add_argument('--cpu-workers', help='仅未托管开发启动允许显式指定 auto 或 1–32')
    args = parser.parse_args()
    logging.basicConfig(
        level=config_getenv("EXECUTION_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    supervisor = RuntimeSupervisor(cpu_workers=args.cpu_workers)
    signal.signal(signal.SIGTERM, supervisor.stop)
    signal.signal(signal.SIGINT, supervisor.stop)
    raise SystemExit(supervisor.run())


if __name__ == "__main__":
    main()
