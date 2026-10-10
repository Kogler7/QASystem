from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


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
    assert "QASYSTEM_LOAD_DOTENV=false" in template
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
    assert "CLEF_CUDA_VISIBLE_DEVICES" in env_keys
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
    assert "AGENT_MCP_URL=http://backend:8000/mcp" in template
    assert "environment:" not in compose
    assert "${QASYSTEM_SOURCE_DIR:-.}:/app:ro" in compose
    assert "${AGENT_HOST_PORT:-9180}:9180" in compose
    assert "MCP_PORT" not in template


def test_backend_proxies_agent_under_its_public_route():
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    assert 'app.add_api_route("/agent"' in main
    assert 'app.add_api_route("/agent/{path:path}"' in main
    assert '"http://agent:9180"' in main


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
    assert "opencv-python==4.11.0.86" in mineru_requirements
    assert "opencv-python-headless==4.11.0.86" in mineru_requirements
    assert '"opencv-python": "4.11.0.86"' in verifier
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


def test_agent_module_reads_its_own_llm_namespace(monkeypatch):
    from agentcode import adapter
    from agentcode.adapter import _load_env

    # _load_env 会把仓库根 .env 整体 load 进进程环境（override=False），
    # 污染后续用例的执行层配置；本用例只关心 AGENT_LLM_* 命名空间，屏蔽掉该副作用。
    monkeypatch.setattr(adapter, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "http://agent-llm:9000")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "agent-key")
    monkeypatch.setenv("AGENT_LLM_MODEL", "agent-model")
    config = _load_env()
    assert config["base_url"] == "http://agent-llm:9000"
    assert config["api_key"] == "agent-key"
    assert config["model"] == "agent-model"
