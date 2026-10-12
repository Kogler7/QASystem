import asyncio

from fastapi.responses import JSONResponse

from backend.app import health
from backend.app.main import health as health_endpoint


def test_full_health_reports_failed_runtime_check(monkeypatch):
    monkeypatch.setattr(
        health,
        "runtime_checks",
        lambda: {"docling": {"ok": False, "reason": "目录不存在或为空"}},
    )
    monkeypatch.setattr(health, "dependency_checks", lambda: {})

    result = health.full_health()

    assert result["status"] == "failed"
    assert result["failed"] == ["docling"]
    assert result["checks"]["docling"]["reason"] == "目录不存在或为空"


def test_basic_health_waits_for_supervisor_runtime_marker(tmp_path, monkeypatch):
    marker = tmp_path / "runtime-ready"
    monkeypatch.setenv("EXECUTION_RUNTIME_READY_FILE", str(marker))

    pending = asyncio.run(health_endpoint(full=False, validate_models=False))
    assert isinstance(pending, JSONResponse)
    assert pending.status_code == 503

    marker.write_text("ready\n", encoding="ascii")
    assert asyncio.run(health_endpoint(full=False, validate_models=False)) == {"status": "ok"}


def test_full_health_reports_all_checks_when_runtime_is_ready(monkeypatch):
    monkeypatch.setattr(health, "runtime_checks", lambda: {"docling": {"ok": True}})
    monkeypatch.setattr(health, "dependency_checks", lambda: {})

    result = health.full_health()

    assert result["status"] == "ok"
    assert result["failed"] == []


def test_dependency_checks_reports_unreachable_configured_service(monkeypatch):
    monkeypatch.setenv("DATABASE_HOST", "mysql.example")
    monkeypatch.setenv("DATABASE_PORT", "3306")
    monkeypatch.setenv("KG_EXTRACT_LLM_BASE_URL", "")
    monkeypatch.setenv("APPLICATION_SERVICE_URL", "")
    monkeypatch.setenv("FILE_SERVICE_URL", "")
    monkeypatch.setattr(
        health.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("connection refused")),
    )

    checks = health.dependency_checks()

    assert checks["mysql"]["ok"] is False
    assert checks["mysql"]["reason"] == "connection refused"



def test_runtime_health_requires_mineru4_in_main_python(monkeypatch):
    from algorithm.preproc.preproc_src.processors import mineru4_processor
    monkeypatch.setenv("MINERU4_PYTHON", "/unused/legacy/python")
    monkeypatch.setattr(health, "_directory_check", lambda path: {"ok": True})
    monkeypatch.setattr(health, "_optional_directory_check", lambda name: {"ok": True})
    for major in (None, 2, 4, 5):
        monkeypatch.setattr(mineru4_processor, "mineru_major_version", lambda: major)
        check = health.runtime_checks()["mineru_module"]
        assert check["ok"] is (major == 4)
        assert check["major_version"] == major
