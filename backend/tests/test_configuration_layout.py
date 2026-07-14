from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_root_is_the_only_configuration_source():
    assert (ROOT / ".env.example").is_file()
    assert (ROOT / "Dockerfile").is_file()
    assert (ROOT / "docker-compose.yml").is_file()
    assert (ROOT / "package-deploy.ps1").is_file()
    assert (ROOT / "build-image.ps1").is_file()
    assert (ROOT / "docker" / "verify_runtime.py").is_file()
    assert not (ROOT / "agent" / ".env.example").exists()
    assert not (ROOT / "docker" / ".env.deploy.example").exists()
    assert not (ROOT / "docker" / "docker-compose.deploy.yml").exists()


def test_environment_template_uses_module_specific_llm_keys():
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    for prefix in ("KG_EXTRACT_LLM_", "GRAPH_LLM_", "AGENT_LLM_"):
        assert prefix in template
    assert "INTENT_LLM_" not in template
    assert "QA_LLM_" not in template
    assert "DIFY_" not in template
    assert "\nLLM_BASE_URL=" not in template
    assert "\nOPENAI_BASE_URL=" not in template


def test_compose_reuses_root_environment_without_variable_duplication():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- .env" in compose
    assert "./deploy/qasystem-runtime:/app" not in compose
    assert "backend-data:/app/backend/data" in compose
    assert "agent-output:/app/agent/image_result" in compose
    assert "KG_EXTRACT_LLM_" not in compose
    assert "INTENT_LLM_" not in compose
    assert "QA_LLM_" not in compose
    assert "AGENT_LLM_" not in compose


def test_deploy_script_only_generates_runtime_assets():
    script = (ROOT / "package-deploy.ps1").read_text(encoding="utf-8")
    assert "qasystem-runtime" in script
    assert "docker-compose.deploy.yml" not in script
    assert ".env.deploy.example" not in script


def test_dockerfile_performs_complete_runtime_verification():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    verifier = (ROOT / "docker" / "verify_runtime.py").read_text(encoding="utf-8")
    assert "pip check" in dockerfile
    assert 'pip install "/app/agent[api]"' in dockerfile
    assert "--no-deps" not in dockerfile
    assert "verify_qasystem_runtime.py" in dockerfile
    assert "from backend.app.main import app as backend_app" in verifier
    assert "from agentcode.api import app as agent_app" in verifier
    assert '"mcp"' in verifier
    assert '"loguru"' in verifier


def test_agent_module_reads_its_own_llm_namespace(monkeypatch):
    from agentcode.adapter import _load_env

    monkeypatch.setenv("AGENT_LLM_BASE_URL", "http://agent-llm:9000")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "agent-key")
    monkeypatch.setenv("AGENT_LLM_MODEL", "agent-model")
    config = _load_env()
    assert config["base_url"] == "http://agent-llm:9000"
    assert config["api_key"] == "agent-key"
    assert config["model"] == "agent-model"
