# config.py
import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    """项目配置类"""

    # ===== Neo4j 配置 =====
    NEO4J_URI: str = "bolt://10.126.62.88:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "neo4j2025"

    # ===== LLM 配置 =====
    LLM_TYPE: str = "glm-4.7"
    LLM_API_KEY: str = "92ede9374dca443485951a9b7396bb1a.0hoHMDMujZgXrgmk"
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
        """从环境变量加载配置（用于生产环境）"""
        return cls(
            NEO4J_URI=os.getenv("NEO4J_URI", cls.NEO4J_URI),
            NEO4J_PASSWORD=os.getenv("NEO4J_PASSWORD", cls.NEO4J_PASSWORD),
            LLM_API_KEY=os.getenv("LLM_API_KEY", cls.LLM_API_KEY),
            # ...
        )


# 创建全局配置实例
config = Config()