"""交互式部署已有权重的 vLLM 服务；离线运行，复用统一环境与 Compose。"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import io
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time

try:
    from docker.download_llm_models import verify, progress
except ImportError:
    from download_llm_models import verify, progress


def run(command: list[str], *, cwd: Path, timeout: int = 60) -> str:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Compose diagnostics can echo deployment secrets. Do not print them.
        raise RuntimeError(f"{command[0]} {command[1]} 执行失败（退出码 {result.returncode}）")
    return result.stdout.strip()


def read_env(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if match:
            value = match[2]
            if value.startswith('"') and value.endswith('"'):
                try:
                    value = json.loads(value)
                except ValueError:
                    value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0]
            values[match[1]] = value
    return values


def update_env(text: str, changes: dict[str, str]) -> str:
    pending = dict(changes)
    lines = []
    for line in text.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", line)
        if match and match[1] in changes:
            key = match[1]
            if key not in pending:
                continue  # Remove duplicate definitions of changed keys.
            lines.append(f"{key}={json.dumps(pending.pop(key), ensure_ascii=False)}")
        else:
            lines.append(line)
    lines.extend(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in pending.items())
    return "\n".join(lines) + "\n"


def atomic_write(path: Path, text: str) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".llm-env-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def project_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docker-compose.yml").is_file():
            return parent
    raise RuntimeError("未找到 docker-compose.yml，请传 --project-dir 指定部署目录")


def compose_prefix(root: Path) -> list[str]:
    supplement = root / "docker/docker-compose.llm.yml"
    if not supplement.is_file():
        supplement = root / "docker-compose.llm.yml"
    if not supplement.is_file():
        raise RuntimeError("缺少小模型 Compose 文件，请同步完整部署包")
    return ["docker", "compose", "--project-directory", str(root), "-f",
            str(root / "docker-compose.yml"), "-f", str(supplement), "--profile", "llm"]


def gpu_rows(raw: str) -> list[dict]:
    return [{"id": row[0].strip(), "name": row[1].strip(), "total": int(row[2]),
             "used": int(row[3]), "utilization": int(row[4])}
            for row in csv.reader(io.StringIO(raw)) if row]


def choose_gpu(gpus: list[dict], selected: str, env: dict, weight_bytes: int) -> dict:
    allowed = {x.strip() for x in env.get("GPU_DEVICE_IDS", "").split(",") if x.strip()}
    reserved = {x.strip() for x in env.get("EXECUTION_GPU_RESERVED_DEVICE_IDS", "").split(",") if x.strip()}
    gpu = next((gpu for gpu in gpus if gpu["id"] == selected), None)
    if gpu is None or (allowed and selected not in allowed):
        raise ValueError("选择的 GPU 不存在或不在 GPU_DEVICE_IDS 允许范围内")
    if selected in reserved:
        raise ValueError("该卡已被其他常驻服务预留，请选择其他 GPU")
    if gpu["used"] > 512 or gpu["utilization"] > 0:
        raise ValueError("该 GPU 正在使用，不能自动抢占或停止其他服务")
    utilization = float(env.get("LLM_GPU_MEMORY_UTILIZATION", "0.90"))
    if not math.isfinite(utilization) or not 0 < utilization < 1:
        raise ValueError("LLM_GPU_MEMORY_UTILIZATION 必须大于 0 且小于 1")
    budget = gpu["total"] * utilization * 1024**2
    if weight_bytes + 2 * 1024**3 > budget:
        raise ValueError("单卡显存预算不足以容纳权重及基本运行空间，请选更小模型或更大的 GPU")
    return gpu


def prompt(message: str, default: str = "") -> str:
    answer = input(f"{message}" + (f" [{default}]" if default else "") + ": ").strip()
    return answer or default


def wait_ready(command: list[str], root: Path, model: str, timeout: int = 600) -> None:
    probe = (
        "import json,urllib.request; "
        "urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5).close(); "
        "print(json.dumps([x['id'] for x in json.load(urllib.request.urlopen("
        "'http://127.0.0.1:8000/v1/models',timeout=5))['data']]))"
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            models = json.loads(run(command + ["exec", "-T", "llm", "python", "-c", probe],
                                    cwd=root, timeout=15))
            if model not in models:
                raise ValueError("服务实际加载的模型名与配置不一致")
            return
        except (RuntimeError, subprocess.TimeoutExpired):
            progress("等待 vLLM 加载权重并通过健康检查……")
            time.sleep(5)
    raise RuntimeError("模型服务未在 600 秒内就绪；保留配置及服务，请检查 llm 日志")


def wait_service(command: list[str], root: Path, service: str, port: int) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            run(command + ["exec", "-T", service, "curl", "-fsS", "--max-time", "5",
                           f"http://127.0.0.1:{port}/health"], cwd=root, timeout=15)
            return
        except (RuntimeError, subprocess.TimeoutExpired):
            progress(f"等待 {service} 健康检查……")
            time.sleep(5)
    raise RuntimeError(f"{service} 未在 120 秒内就绪")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path)
    args = parser.parse_args(argv)
    applied = False
    original = None
    try:
        root = (args.project_dir or project_dir()).resolve()
        command = compose_prefix(root)
        env_path = root / ".env"
        original = env_path.read_text(encoding="utf-8-sig") if env_path.exists() else None
        template = original if original is not None else (root / ".env.example").read_text(encoding="utf-8-sig")
        env = read_env(template)
        defaults = read_env((root / ".env.example").read_text(encoding="utf-8-sig"))
        effective = {**defaults, **env}
        env = effective
        image = env.get("QASYSTEM_ENV_IMAGE") or "qasystem-env-v1"
        if image.split(":")[0] != "qasystem-env-v1":
            raise ValueError("应用环境必须使用 qasystem-env-v1")
        progress("检查 Docker、Compose 和本地环境镜像（不拉取镜像）")
        run(["docker", "info", "--format", "{{.ServerVersion}}"], cwd=root)
        run(["docker", "compose", "version"], cwd=root)
        run(["docker", "image", "inspect", image, "--format", "{{.Id}}"], cwd=root)
        if run(["docker", "ps", "-q", "--filter", "label=com.docker.compose.service=llm"], cwd=root):
            raise ValueError("已有 llm 服务正在运行；本向导不自动替换常驻服务")
        progress("检查镜像中的 vLLM / Torch / Qwen3.5 支持")
        run(["docker", "run", "--rm", "--entrypoint", "python", image, "-c",
             "import vllm,torch; from transformers import Qwen3_5ForConditionalGeneration; "
             "assert tuple(int(x) for x in vllm.__version__.split('.')[:2]) >= (0,31)"], cwd=root, timeout=120)
        progress("通过容器检查 NVIDIA GPU 和驱动")
        gpus = gpu_rows(run(["docker", "run", "--rm", "--gpus", "all", "--entrypoint",
            "nvidia-smi", image, "--query-gpu=index,name,memory.total,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits"], cwd=root))
        model_root = Path(prompt("模型根目录 HOST_MODELS_DIR", env.get("HOST_MODELS_DIR", "./models"))).expanduser()
        model_root = (root / model_root).resolve() if not model_root.is_absolute() else model_root.resolve()
        catalog = json.loads(Path(__file__).with_name("llm-models.json").read_text(encoding="utf-8"))
        available = [item for item in catalog if (model_root / "llm" / item["repo"].replace("/", "--")).is_dir()]
        if not available:
            raise ValueError("未发现已有权重；请从联网准备机器复制 llm/ 权重目录，本向导不联网下载")
        for i, item in enumerate(available, 1):
            print(f"{i}. {item['repo']}")
        number = int(prompt("选择模型序号", "1"))
        if not 1 <= number <= len(available):
            raise ValueError("模型序号无效")
        model = available[number - 1]
        relative = "llm/" + model["repo"].replace("/", "--")
        for entry in model["files"]:
            progress(f"校验权重文件 {entry['rfilename']}")
            verify(model_root / relative, [entry])
        for gpu in gpus:
            print(f"GPU {gpu['id']}: {gpu['name']}，显存 {gpu['used']}/{gpu['total']} MiB，利用率 {gpu['utilization']}%")
        selected = prompt("选择专用 GPU 编号（不会停止其他服务）")
        weight_bytes = sum(x["size"] for x in model["files"] if x["rfilename"].endswith(".safetensors"))
        choose_gpu(gpus, selected, env, weight_bytes)
        port = int(prompt("宿主机回环端口", env.get("LLM_HOST_PORT", "9998")))
        length = int(prompt("上下文总长度（输入＋输出）", env.get("LLM_MAX_MODEL_LEN", "32768")))
        if not 1 <= port <= 65535 or not 1024 <= length <= 262144:
            raise ValueError("端口或上下文长度无效")
        with socket.socket() as check:
            check.bind(("127.0.0.1", port))
        reserved = {x.strip() for x in env.get("EXECUTION_GPU_RESERVED_DEVICE_IDS", "").split(",") if x.strip()}
        reserved.add(selected)
        changes = {key: value for key, value in defaults.items() if key.startswith("LLM_") and key not in read_env(template)}
        changes.update({"HOST_MODELS_DIR": model_root.as_posix(), "LLM_MODEL_DIR": relative,
            "LLM_SERVED_MODEL_NAME": model["repo"], "LLM_GPU_DEVICE_IDS": selected,
            "LLM_HOST_PORT": str(port), "LLM_BIND": "127.0.0.1", "LLM_MAX_MODEL_LEN": str(length),
            "EXECUTION_GPU_RESERVED_DEVICE_IDS": ",".join(sorted(reserved, key=int))})
        if prompt("将该模型设为容器内评测的默认 judge？y/n", "y").lower() == "y":
            changes.update(QASYSTEM_JUDGE_MODEL=model["repo"], QASYSTEM_JUDGE_BASE_URL="http://llm:8000/v1",
                           QASYSTEM_JUDGE_ADAPTER="chat")
        print(json.dumps(changes, ensure_ascii=False, indent=2))
        print("应用后启动 llm，并 recreate Backend/Agent 以更新 GPU 预留及配置；现有任务应先完成。")
        if prompt("应用配置并启动？输入 yes；其他输入取消", "no") != "yes":
            progress("已取消，未修改配置或常驻服务")
            return 0
        # The old prompt sequence cannot display and approve the full release delta.
        raise RuntimeError('旧版部署入口不再允许改配置或启动服务；请使用 docker/configure.py 的版本审核界面')
        # GPU ownership may have changed during hashing and user input.
        fresh = gpu_rows(run(["docker", "run", "--rm", "--gpus", "all", "--entrypoint",
            "nvidia-smi", image, "--query-gpu=index,name,memory.total,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits"], cwd=root))
        choose_gpu(fresh, selected, env, weight_bytes)
        if original is not None:
            backup = root / f".env.backup-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
            atomic_write(backup, original)
            progress(f"原配置已备份到 {backup.name}（包含凭据，请妥善保管）")
        atomic_write(env_path, update_env(template, changes))
        run(command + ["config", "--quiet"], cwd=root)
        progress("更新 Backend/Agent 的 GPU 预留和配置，再启动常驻 llm 服务")
        applied = True  # From this point, do not silently roll back live configuration.
        run(command + ["up", "-d", "--no-deps", "--force-recreate", "backend", "agent"], cwd=root, timeout=120)
        run(command + ["up", "-d", "--no-deps", "llm"], cwd=root, timeout=120)
        wait_ready(command, root, model["repo"])
        progress("模型已就绪，执行真实推理预热")
        warmup = (
            "import json,urllib.request; "
            f"body={{'model':{model['repo']!r},'messages':[{{'role':'user','content':'Reply OK.'}}],"
            "'max_tokens':32,'temperature':0,'chat_template_kwargs':{'enable_thinking':False}}; "
            "req=urllib.request.Request('http://127.0.0.1:8000/v1/chat/completions',"
            "data=json.dumps(body).encode(),headers={'Content-Type':'application/json'}); "
            "data=json.load(urllib.request.urlopen(req,timeout=60)); "
            "assert data['choices'][0]['message'].get('content','').strip(),'empty warmup response'"
        )
        run(command + ["exec", "-T", "llm", "python", "-c", warmup], cwd=root, timeout=90)
        progress("检查 Backend/Agent 健康状态")
        wait_service(command, root, "backend", 8000)
        wait_service(command, root, "agent", 9180)
        progress(f"部署完成：容器地址 http://llm:8000/v1；宿主机地址 http://127.0.0.1:{port}/v1")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, KeyboardInterrupt, EOFError) as exc:
        if not applied and "env_path" in locals() and "template" in locals():
            # Restore only if this invocation actually wrote its configuration.
            if "changes" in locals() and env_path.exists() and env_path.read_text(encoding="utf-8") == update_env(template, changes):
                if original is None:
                    env_path.unlink()
                else:
                    atomic_write(env_path, original)
        progress(f"未完成：{type(exc).__name__}" if isinstance(exc, KeyboardInterrupt) else f"未完成：{exc}")
        if applied:
            progress("已启动阶段的配置与容器保留，未自动停止服务；请检查 llm/Backend/Agent 状态")
        return 1


legacy_main = main


def main(argv=None):
    """Default to the configuration editor; retain the old flow explicitly."""
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if "--legacy" in args:
        args.remove("--legacy")
        return legacy_main(args)
    try:
        from docker.configure import main as configure_main
    except ImportError:
        from configure import main as configure_main
    return configure_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
