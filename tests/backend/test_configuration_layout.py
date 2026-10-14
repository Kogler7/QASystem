from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_compose_has_no_second_defaults_catalog_and_all_variables_are_declared():
    import json
    import re
    compose = (ROOT / 'docker-compose.yml').read_text(encoding='utf8')
    catalog = json.loads((ROOT / 'qasystem_config/config-declarations.json').read_text(encoding='utf8'))
    assert not re.search(r'\$\{[A-Z][A-Z0-9_]*:-', compose)
    assert set(re.findall(r'\$\{([A-Z][A-Z0-9_]*)', compose)) <= catalog['items'].keys()


def test_root_is_the_only_configuration_source():
    assert (ROOT / ".env.example").is_file()
    assert (ROOT / "Dockerfile").is_file()
    assert not (ROOT / "Dockerfile.runtime").exists()
    assert (ROOT / "docker-compose.yml").is_file()
    assert (ROOT / "package-deploy.ps1").is_file()
    assert (ROOT / "build-image.ps1").is_file()
    assert (ROOT / "download-models.ps1").is_file()
    assert (ROOT / "run-offline-validation.ps1").is_file()
    assert (ROOT / "docker" / "verify_runtime.py").is_file()
    assert (ROOT / "docker" / "download_models.py").is_file()
    assert (ROOT / "docker" / "docker-compose.offline-test.yml").is_file()
    assert (ROOT / "docker" / "offline_mock_llm.py").is_file()
    assert (ROOT / "docker" / "offline_validation.py").is_file()
    assert not (ROOT / "docker" / "make-runtime-base.sh").exists()
    assert not (ROOT / "docker" / "apply-runtime.sh").exists()
    assert not (ROOT / "docker" / "RUNTIME_BASE.md").exists()
    assert (ROOT / "backend" / "app" / "health.py").is_file()
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
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "- .env" in compose
    assert "QASYSTEM_LOAD_DOTENV=" not in template
    assert "./deploy/qasystem-runtime:/app" not in compose
    assert "backend-data:/app/backend/data" in compose
    assert "agent-output:/app/agent/image_result" in compose
    assert "KG_EXTRACT_LLM_" not in compose
    assert "INTENT_LLM_" not in compose
    assert "QA_LLM_" not in compose
    assert "AGENT_LLM_" not in compose
    env_keys = {line.split("=", 1)[0].strip() for line in template.splitlines()
                if "=" in line and not line.lstrip().startswith("#")}
    assert "CUDA_VISIBLE_DEVICES" not in env_keys
    assert "CLEF_CUDA_VISIBLE_DEVICES" not in env_keys
    assert "GPU_DEVICE_IDS=4,5,6,7" in template
    assert "QASYSTEM_ENV_IMAGE=qasystem-env-v1" in template
    assert "QASYSTEM_SOURCE_DIR=." in template
    assert "MCP_EXECUTION_MODE=sync" in template
    assert "MCP_EXECUTION_MODE: ${MCP_EXECUTION_MODE:-sync}" not in compose
    assert "python -m app.execution.supervisor" in compose
    assert "BACKEND_BIND=0.0.0.0" in template
    assert "GPU_DEVICE_IDS:" not in compose
    assert "execution-gpu-worker" not in compose
    assert "execution-vlm-worker" not in compose
    assert "vllm-0:" not in compose
    assert "AGENT_MCP_URL" not in env_keys
    assert "environment: *qasystem-process-environment" in compose
    assert "${QASYSTEM_SOURCE_DIR:?Run ops config to generate configuration}:/app:ro" in compose
    assert "${AGENT_HOST_PORT:?Run ops config to generate configuration}:9180" in compose
    assert "MCP_PORT" not in env_keys
    assert "AGENT_MCP_PORT" not in env_keys


def test_backend_proxies_agent_under_its_public_route():
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    assert 'app.add_api_route("/agent"' in main
    assert 'app.add_api_route("/agent/{path:path}"' in main
    assert 'upstream = internal_agent_url()' in main


def test_deploy_script_only_generates_runtime_assets():
    script = (ROOT / "package-deploy.ps1").read_text(encoding="utf-8")
    assert 'layout = "source-free-environment-image-with-read-only-source-mount"' in script
    assert 'runtime-context.tar' not in script
    assert 'IncludeEnvImage' in script
    assert 'qasystem-runtime-v1.tar' not in script
    assert 'qasystem-backend-latest.tar' not in script
    assert "robocopy" not in script
    assert "docker-compose.delivery.yml" not in script
    assert "docker-compose.deploy.yml" not in script
    assert ".env.deploy.example" not in script


def test_dockerfile_performs_complete_runtime_verification():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    build_script = (ROOT / "build-image.ps1").read_text(encoding="utf-8")
    mineru_requirements = (ROOT / "backend" / "requirements.mineru-extras.txt").read_text(encoding="utf-8")
    verifier = (ROOT / "docker" / "verify_runtime.py").read_text(encoding="utf-8")
    docling_processor = (
        ROOT / "algorithm" / "preproc" / "preproc_src" / "processors" / "docx_processor.py"
    ).read_text(encoding="utf-8")
    assert "pip check" in dockerfile
    assert "docling-tools models download" not in dockerfile
    assert 'pip install "/app/agent[api]"' not in dockerfile
    assert "verify_qasystem_runtime.py" not in dockerfile
    assert "COPY backend /app/backend" not in dockerfile
    assert "COPY algorithm /app/algorithm" not in dockerfile
    assert "opencv-python==4.13.0.92" in mineru_requirements
    assert "opencv-python-headless==4.13.0.92" in mineru_requirements
    assert '"opencv-python": "4.13.0.92"' in verifier
    assert "/app/docker/verify_runtime.py" in build_script
    assert "download_qasystem_models.py" not in dockerfile
    assert "from backend.app.main import app as backend_app" in verifier
    assert "from agentcode.api import app as agent_app" in verifier
    assert '"mcp"' in verifier
    assert '"loguru"' in verifier
    assert '"java"' in verifier
    assert "ofd-to-pdf.jar" in verifier
    assert '"Usage: ofd-to-pdf"' in verifier
    assert 'DOCLING_MODELS_PATH = "/models/docling-models"' in docling_processor
    assert "DOCLING_ARTIFACTS_PATH" not in dockerfile


def test_agent_module_uses_registered_service_and_ignores_legacy_namespace(monkeypatch):
    from agentcode import adapter
    from external_services.llm import service
    monkeypatch.setattr(adapter, 'bootstrap_local_environment', lambda: False)
    monkeypatch.setenv('AGENT_LLM_BASE_URL', 'http://retired.invalid')
    monkeypatch.setenv('AGENT_LLM_MODEL', 'retired-model')
    monkeypatch.setattr(service, 'resolve_chat_service', lambda *a, **kw: (
        {'base_url':'http://agent-llm:9000','model':'agent-model','service_id':'chosen'}, 'agent-key'))
    config = adapter._load_env()
    assert config['base_url'] == 'http://agent-llm:9000'
    assert config['api_key'] == 'agent-key' and config['model'] == 'agent-model'


def test_runtime_version_check_accepts_cuda_build_tag_but_rejects_release_drift(monkeypatch):
    import pytest
    from docker import verify_runtime
    monkeypatch.setattr(verify_runtime, "EXPECTED_DISTRIBUTIONS", {"torch": "2.13.0"})
    monkeypatch.setattr(verify_runtime.importlib.metadata, "version", lambda name: "2.13.0+cu130")
    verify_runtime._check_distribution_versions()
    monkeypatch.setattr(verify_runtime.importlib.metadata, "version", lambda name: "2.12.0+cu130")
    with pytest.raises(RuntimeError, match="runtime distribution version mismatches"):
        verify_runtime._check_distribution_versions()


def test_fixed_process_contract_and_runtime_mounts_are_shared():
    import yaml
    from qasystem_config.startup_contract import PROCESS_ENVIRONMENT
    compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
    for name in ('backend', 'agent'):
        service = compose['services'][name]
        assert service['environment'] == PROCESS_ENVIRONMENT
        assert './runtime-config:/app/runtime-config:ro' in service['volumes']
    template = (ROOT / '.env.example').read_text()
    for key in ('QASYSTEM_LOAD_DOTENV', 'QASYSTEM_RUNTIME_CONFIG_DIR', 'PYTHONPATH', 'PYTHONUNBUFFERED'):
        assert key + '=' not in template
