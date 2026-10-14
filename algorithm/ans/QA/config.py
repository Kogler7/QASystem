from qasystem_config.configuration import getenv as config_getenv
# config.py
import os
from dataclasses import dataclass
from qasystem_config.configuration import bootstrap_local_environment


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
    GRAPH_LLM_MODEL: str = ""
    GRAPH_LLM_API_KEY: str = ""

    @classmethod
    def from_env(cls):
        """从环境变量加载配置"""
        from external_services.llm.service import resolve_chat_service
        service, llm_api_key = resolve_chat_service('graph', required=True)
        llm_model = service['model']
        return cls(
            GRAPH_LLM_MODEL=llm_model,
            GRAPH_LLM_API_KEY=llm_api_key,
        )

    @classmethod
    def load(cls):
        """只从仓库根目录加载图谱检索配置。"""
        bootstrap_local_environment()
        return cls.from_env()
