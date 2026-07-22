from backend.app import health


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


def test_full_health_reports_all_checks_when_runtime_is_ready(monkeypatch):
    monkeypatch.setattr(health, "runtime_checks", lambda: {"docling": {"ok": True}})
    monkeypatch.setattr(health, "dependency_checks", lambda: {})

    result = health.full_health()

    assert result["status"] == "ok"
    assert result["failed"] == []


def test_dependency_checks_reports_unreachable_configured_service(monkeypatch):
    monkeypatch.setenv("DATABASE_HOST", "mysql.example")
    monkeypatch.setenv("DATABASE_PORT", "3306")
    monkeypatch.setenv("NEO4J_URI", "")
    monkeypatch.setenv("CONFIDENCE_ES_HOSTS", "")
    monkeypatch.setenv("KG_EXTRACT_LLM_BASE_URL", "")
    monkeypatch.setenv("FILE_SERVICE_URL", "")
    monkeypatch.setattr(
        health.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("connection refused")),
    )

    checks = health.dependency_checks()

    assert checks["mysql"]["ok"] is False
    assert checks["mysql"]["reason"] == "connection refused"
