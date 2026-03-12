# config.py
import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv


@dataclass
class Config:
    """项目配置类"""

    # ===== Neo4j 配置 =====
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = ""

    # ===== LLM 配置 =====
    LLM_TYPE: str = "glm-4.7"
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
        return cls(
            NEO4J_URI=os.getenv("NEO4J_URI", cls.NEO4J_URI),
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
        """加载配置的优先级：环境变量 > .env 文件 > 默认值"""
        # 获取当前文件所在目录
        current_dir = os.path.dirname(os.path.abspath(__file__))
        # 构建 .env 文件的完整路径
        env_path = os.path.join(current_dir, env_file)
        
        # 加载 .env 文件
        if os.path.exists(env_path):
            load_dotenv(env_path)
            print(f"已加载配置文件: {env_path}")
        else:
            print(f"未找到配置文件: {env_path}")
        
        # 从环境变量加载配置
        return cls.from_env()


# 创建全局配置实例
config = Config.load()
