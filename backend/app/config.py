from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict  # type: ignore[reportMissingImports]

# 先加载 .env 到 os.environ，再定义 Settings（算法子模块多读 os.getenv）。
# 统一约定：
#   - Docker / 部署场景：根 .env 为主；
#   - 本机 uvicorn 场景：backend/.env 为主，避免误用 Docker 专用地址（如 db/host.docker.internal）。
_BACKEND_DIR = Path(__file__).resolve().parent.parent
_ROOT_DIR = _BACKEND_DIR.parent


def _running_in_container() -> bool:
    return Path("/.dockerenv").exists()


def _set_algorithm_compat_env() -> None:
    """
    统一算法模块的历史变量名，避免 Docker 部署时因变量名不一致导致组件不可用。

    当前主要兼容：
    - algorithm/ans/QA: `LLM_TYPE`（历史） <- `LLM_MODEL`（统一）
    - intent_recognition_model: `NEO4J_USERNAME`（历史） <- `NEO4J_USER`（统一）
    - 旧 OpenAI 兼容变量 <- 统一 LLM 变量
    """
    llm_model = (os.getenv("LLM_MODEL") or "").strip()
    if llm_model and not (os.getenv("LLM_TYPE") or "").strip():
        os.environ["LLM_TYPE"] = llm_model

    neo4j_user = (os.getenv("NEO4J_USER") or "").strip()
    if neo4j_user and not (os.getenv("NEO4J_USERNAME") or "").strip():
        os.environ["NEO4J_USERNAME"] = neo4j_user

    llm_base_url = (os.getenv("LLM_BASE_URL") or "").strip()
    if llm_base_url and not (os.getenv("OPENAI_BASE_URL") or "").strip():
        os.environ["OPENAI_BASE_URL"] = llm_base_url

    llm_api_key = (os.getenv("LLM_API_KEY") or "").strip()
    if llm_api_key and not (os.getenv("OPENAI_API_KEY") or "").strip():
        os.environ["OPENAI_API_KEY"] = llm_api_key


if _running_in_container():
    for _env_path in (_ROOT_DIR / ".env", _BACKEND_DIR / ".env"):
        if _env_path.is_file():
            load_dotenv(_env_path, override=False)
else:
    # 本机优先 backend/.env，并允许覆盖根 .env 里的同名键。
    if (_ROOT_DIR / ".env").is_file():
        load_dotenv(_ROOT_DIR / ".env", override=False)
    if (_BACKEND_DIR / ".env").is_file():
        load_dotenv(_BACKEND_DIR / ".env", override=True)

_set_algorithm_compat_env()


class Settings(BaseSettings):
    """应用配置：环境变量与已加载的 .env（见模块顶部 load_dotenv）。"""

    # Dify
    DIFY_API_URL: str = "https://api.dify.ai/v1"
    DIFY_API_KEY: str = ""
    DIFY_KNOWLEDGE_API_KEY: str = ""

    DIFY_USE_WORKFLOW: bool = False
    DIFY_WORKFLOW_API_KEY: str = ""
    DIFY_WORKFLOW_INPUT_VAR: str = "query"
    DIFY_WORKFLOW_OUTPUT_VAR: str = "text"
    DIFY_WORKFLOW_RESPONSE_MODE: str = "blocking"
    DIFY_WORKFLOW_TIMEOUT_SECONDS: int = 120

    JWT_SECRET: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    ENVIRONMENT: str = "development"

    DATABASE_URL: str = "mysql+asyncmy://rail_user:123456@127.0.0.1:3306/rail_assistant"

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
