# config.py
import os
from dataclasses import dataclass
from dotenv import load_dotenv


def _running_in_container() -> bool:
    """判断当前是否运行在容器内。"""
    return os.path.exists("/.dockerenv")


def _normalize_host_docker_internal(value: str) -> str:
    """
    本机运行时将 host.docker.internal 回退到 127.0.0.1，
    避免 Linux 主机直接运行 uvicorn 时 DNS 无法解析。
    """
    if not value:
        return value
    if _running_in_container():
        return value
    return value.replace("host.docker.internal", "127.0.0.1")


@dataclass
class Config:
    """项目配置类"""

    # ===== LLM 配置 =====
    GRAPH_LLM_MODEL: str = "Qwen/Qwen3-8B"
    GRAPH_LLM_API_KEY: str = ""

    @classmethod
    def from_env(cls):
        """从环境变量加载配置"""
        llm_model = os.getenv("GRAPH_LLM_MODEL") or cls.GRAPH_LLM_MODEL
        llm_api_key = os.getenv("GRAPH_LLM_API_KEY") or cls.GRAPH_LLM_API_KEY
        return cls(
            GRAPH_LLM_MODEL=llm_model,
            GRAPH_LLM_API_KEY=llm_api_key,
        )

    @classmethod
    def load(cls):
        """只从仓库根目录加载图谱检索配置。"""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.abspath(os.path.join(current_dir, "..", "..", ".."))
        root_env_path = os.path.join(project_root, ".env")
        if os.path.exists(root_env_path):
            load_dotenv(root_env_path, override=False)
        return cls.from_env()
