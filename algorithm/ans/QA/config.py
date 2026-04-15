# config.py
import os
from dataclasses import dataclass
from typing import Optional
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

    # ===== Neo4j 配置 =====
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "neo4j2025"  # 默认与 .env.example 一致；生产请通过 .env 或环境变量覆盖

    # ===== LLM 配置 =====
    LLM_TYPE: str = "Qwen/Qwen3-8B"
    LLM_API_KEY: str = ""
    MAX_LENGTH: int = 256
    TEMPERATURE_EXPLORATION: float = 0.4
    TEMPERATURE_REASONING: float = 0.0

    # ===== ToG 搜索配置 =====
    WIDTH: int = 3
    DEPTH: int = 3
    NUM_RETAIN_ENTITY: int = 5
    PRUNE_TOOLS: str = "llm"
    REMOVE_UNNECESSARY_REL: bool = True
    AUTO_ENTITY_LINKING: bool = True

    # ===== 可视化配置 =====
    VISUALIZATION_DIR: str = "visualizations"
    ENABLE_VISUALIZATION: bool = True

    # ===== 服务配置 =====
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 5000
    DEBUG: bool = True

    def __post_init__(self):
        """初始化后处理，比如创建目录"""
        os.makedirs(self.VISUALIZATION_DIR, exist_ok=True)

    @classmethod
    def from_env(cls):
        """从环境变量加载配置"""
        neo4j_uri = _normalize_host_docker_internal(os.getenv("NEO4J_URI", cls.NEO4J_URI))
        return cls(
            NEO4J_URI=neo4j_uri,
            NEO4J_USER=os.getenv("NEO4J_USER", cls.NEO4J_USER),
            NEO4J_PASSWORD=os.getenv("NEO4J_PASSWORD", cls.NEO4J_PASSWORD),
            LLM_TYPE=os.getenv("LLM_TYPE", cls.LLM_TYPE),
            LLM_API_KEY=os.getenv("LLM_API_KEY", cls.LLM_API_KEY),
            MAX_LENGTH=int(os.getenv("MAX_LENGTH", str(cls.MAX_LENGTH))),
            TEMPERATURE_EXPLORATION=float(os.getenv("TEMPERATURE_EXPLORATION", str(cls.TEMPERATURE_EXPLORATION))),
            TEMPERATURE_REASONING=float(os.getenv("TEMPERATURE_REASONING", str(cls.TEMPERATURE_REASONING))),
            WIDTH=int(os.getenv("WIDTH", str(cls.WIDTH))),
            DEPTH=int(os.getenv("DEPTH", str(cls.DEPTH))),
            NUM_RETAIN_ENTITY=int(os.getenv("NUM_RETAIN_ENTITY", str(cls.NUM_RETAIN_ENTITY))),
            PRUNE_TOOLS=os.getenv("PRUNE_TOOLS", cls.PRUNE_TOOLS),
            REMOVE_UNNECESSARY_REL=os.getenv("REMOVE_UNNECESSARY_REL", str(cls.REMOVE_UNNECESSARY_REL)).lower() == "true",
            AUTO_ENTITY_LINKING=os.getenv("AUTO_ENTITY_LINKING", str(cls.AUTO_ENTITY_LINKING)).lower() == "true",
            VISUALIZATION_DIR=os.getenv("VISUALIZATION_DIR", cls.VISUALIZATION_DIR),
            ENABLE_VISUALIZATION=os.getenv("ENABLE_VISUALIZATION", str(cls.ENABLE_VISUALIZATION)).lower() == "true",
            API_HOST=os.getenv("API_HOST", cls.API_HOST),
            API_PORT=int(os.getenv("API_PORT", str(cls.API_PORT))),
            DEBUG=os.getenv("DEBUG", str(cls.DEBUG)).lower() == "true"
        )

    @classmethod
    def load(cls, env_file: str = ".env"):
        """加载配置：容器内优先根 .env，本机优先 backend/.env，再补充 QA/.env。"""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.abspath(os.path.join(current_dir, "..", "..", ".."))
        backend_env_path = os.path.join(project_root, "backend", ".env")
        root_env_path = os.path.join(project_root, ".env")
        qa_env_path = os.path.join(current_dir, env_file)

        if _running_in_container():
            # 容器内：根 .env 为主，其他仅补充。
            for path in (root_env_path, backend_env_path, qa_env_path):
                if os.path.exists(path):
                    load_dotenv(path, override=False)
                    print(f"已加载配置文件: {path}")
        else:
            # 本机：backend/.env 为主，QA/.env 次之；根 .env 只作兜底补充。
            if os.path.exists(root_env_path):
                load_dotenv(root_env_path, override=False)
                print(f"已加载配置文件: {root_env_path}")
            if os.path.exists(backend_env_path):
                load_dotenv(backend_env_path, override=True)
                print(f"已加载配置文件: {backend_env_path}")
            if os.path.exists(qa_env_path):
                load_dotenv(qa_env_path, override=False)
                print(f"已加载配置文件: {qa_env_path}")
        
        # 将LLM相关配置设置到全局环境变量，供algorithm/common/llm_client使用
        llm_base_url = _normalize_host_docker_internal(os.getenv("LLM_BASE_URL", ""))
        if llm_base_url:
            os.environ["LLM_BASE_URL"] = llm_base_url
            print(f"设置LLM_BASE_URL环境变量: {llm_base_url}")
        
        llm_model = os.getenv("LLM_MODEL")
        if llm_model:
            os.environ["LLM_MODEL"] = llm_model
            print(f"设置LLM_MODEL环境变量: {llm_model}")
        
        llm_api_key = os.getenv("LLM_API_KEY")
        if llm_api_key:
            os.environ["LLM_API_KEY"] = llm_api_key
        
        llm_backend = os.getenv("LLM_BACKEND")
        if llm_backend:
            os.environ["LLM_BACKEND"] = llm_backend
            print(f"设置LLM_BACKEND环境变量: {llm_backend}")
        
        # 从环境变量加载配置
        return cls.from_env()


# 创建全局配置实例
config = Config.load()
