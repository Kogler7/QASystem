from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    # Dify
    DIFY_API_URL: str = "https://api.dify.ai/v1"
    DIFY_API_KEY: str = ""
    DIFY_KNOWLEDGE_API_KEY: str = ""

    # Dify 工作流（智能问答使用工作流时必填）
    DIFY_USE_WORKFLOW: bool = False
    DIFY_WORKFLOW_API_KEY: str = ""
    DIFY_WORKFLOW_INPUT_VAR: str = "query"
    DIFY_WORKFLOW_OUTPUT_VAR: str = "text"

    # JWT & auth
    JWT_SECRET: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # Database (default to MySQL style URL; can be overridden)
    DATABASE_URL: str = "mysql+asyncmy://root:123456@localhost:3306/rail_assistant"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

