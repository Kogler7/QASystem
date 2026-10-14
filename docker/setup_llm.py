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








def project_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docker-compose.yml").is_file():
            return parent
    raise RuntimeError("未找到 docker-compose.yml，请传 --project-dir 指定部署目录")


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
    utilization = float(env.get("utilization", "0.90"))
    if not math.isfinite(utilization) or not 0 < utilization < 1:
        raise ValueError("utilization 必须大于 0 且小于 1")
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


def main(argv=None):
    """Compatibility launcher for the sole configuration UI."""
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if "--legacy" in args:
        progress("旧单模型配置入口已删除；请运行 ops config，在模型分区审核迁移")
        return 1
    try:
        from docker.configure import main as configure_main
    except ImportError:
        from configure import main as configure_main
    return configure_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
