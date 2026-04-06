from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict  # type: ignore[reportMissingImports]

# 先加载 .env 到 os.environ，再定义 Settings（算法子模块多读 os.getenv）
# 顺序：仓库根 .env（可选，Docker/团队统一变量）→ backend/.env（本地开发覆盖）
_BACKEND_DIR = Path(__file__).resolve().parent.parent
_ROOT_DIR = _BACKEND_DIR.parent
for _env_path, _override in ((_ROOT_DIR / ".env", False), (_BACKEND_DIR / ".env", True)):
    if _env_path.is_file():
        load_dotenv(_env_path, override=_override)


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
