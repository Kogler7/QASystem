from __future__ import annotations

import pytest

from backend.app.execution import supervisor as supervisor_mod


def _capture_core_worker_env(monkeypatch, *, vlm_enabled: str, gpu_ids: list[str]) -> list[dict]:
    """运行 _start_core_workers 并收集各 spawn 调用的 env（不真正起进程）。"""
    monkeypatch.setattr(supervisor_mod, '_gpu_ids', lambda: gpu_ids)
    monkeypatch.setattr('docker.model_services.worker_record', lambda: {'enabled': vlm_enabled == 'true', 'port': '8100', 'device': 'worker'})
    monkeypatch.delenv("MINERU_VLM_SERVER_URL", raising=False)
    captured: list[dict] = []

    sup = supervisor_mod.RuntimeSupervisor()
    sup.gpu_ids = gpu_ids

    def fake_spawn(name, command, *, env=None):
        captured.append(dict(env or {}))
        return object()

    monkeypatch.setattr(sup, "_spawn", fake_spawn)
    sup._start_core_workers()
    return captured


def test_core_workers_not_injected_dead_vlm_url_when_vlm_disabled(monkeypatch):
    # 回归：VLLM_ENABLED=false 时引擎不会启动，若仍向核心 worker 注入
    # 127.0.0.1:8100 的 VLM 端点，扫描版 PDF 会选中 vlm-http-client 连接失败，
    # 且因「URL 已配置」跳过本地 MinerU pipeline 自动回退（official_01/07 评测失败根因）。
    envs = _capture_core_worker_env(monkeypatch, vlm_enabled="false", gpu_ids=["4", "5"])
    assert envs, "应至少 spawn 一个核心进程"
    for env in envs:
        assert "MINERU_VLM_SERVER_URL" not in env
        assert env.get("MINERU_VLM_BACKEND") != "vlm-http-client"


def test_core_workers_injected_vlm_url_when_vlm_enabled(monkeypatch):
    envs = _capture_core_worker_env(monkeypatch, vlm_enabled="true", gpu_ids=["4", "5"])
    assert envs
    worker_urls = [
        env.get("MINERU_VLM_SERVER_URL")
        for env in envs
        if env.get("EXECUTION_WORKER_ID", "").startswith("cpu-")
    ]
    assert worker_urls, "cpu worker 应注入 VLM 端点"
    assert all(url and url.startswith("http://127.0.0.1:81") for url in worker_urls)


def test_planned_engine_ports_follows_vlm_enabled(monkeypatch):
    monkeypatch.delenv("MINERU_VLM_SERVER_URL", raising=False)
    sup = supervisor_mod.RuntimeSupervisor()
    sup.gpu_ids = ["4", "5"]

    sup.model_record = {"enabled": False}
    assert sup._planned_engine_ports() == []

    sup.model_record = {"enabled": True}
    assert sup._planned_engine_ports() == [sup.base_port, sup.base_port + 1]
