import os
from pathlib import Path

# ==========================================
# 1. 基础常量 (解决旧代码的 ImportError)
# ==========================================
MAX_BATCH_SIZE = 1000
NODE_BATCH_SIZE = 500
CLASS_BATCH_SIZE = 100

# 1. 数据库配置（优先从环境变量读取，避免云地址无法解析时阻塞本地使用）
# 本地 Neo4j 示例: bolt://localhost:7687 或 bolt://127.0.0.1:7687
# Neo4j Aura 云: neo4j+s://xxxx.databases.neo4j.io（不写端口，驱动默认 7687）
NEO4J_URI = os.getenv("CONFLICT_NEO4J_URI") or os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("CONFLICT_NEO4J_USER") or os.getenv("NEO4J_USER", "neo4j")
NEO4J_PWD = os.getenv("CONFLICT_NEO4J_PWD") or os.getenv("NEO4J_PASSWORD", "neo4j2025")

# 2.  就是这行！检查名字对不对，有没有漏掉 S
COMMON_PREFIXES = """
@prefix ex: <http://example.org/> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
"""

# ==========================================
# 3. 本地 QWEN 大模型配置 (适配本地部署)
# ==========================================
# 本地部署通常不需要 Key，或者随便填一个字符串
LOCAL_LLM_KEY = "sk-local-qwen" # 本地模型随便填

LOCAL_LLM_URL = "http://localhost:11434/v1" # 你的本地接口地址

LOCAL_MODEL_NAME = "qwen2.5-7b-instruct" # 你的 QWEN 模型名

TEMPERATURE = 0  # 设为 0 保证逻辑生成的确定性
JSON_MODE = True  # 强制要求 QWEN 输出 JSON 格式

# ==========================================
# 4. 路径锁定 (基于项目根目录)
# ==========================================
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RESULT_DIR = BASE_DIR / "results"

# 自动创建必要文件夹
DATA_DIR.mkdir(parents=True, exist_ok=True)
RESULT_DIR.mkdir(parents=True, exist_ok=True)

# 核心逻辑文件路径
INSERT_TTL = DATA_DIR / "insert.ttl"
DELETE_TTL = DATA_DIR / "delete.ttl"
SHAPE_TTL = DATA_DIR / "shape.ttl"
SHAPE_LP = DATA_DIR / "shape.lp"
STRATEGY_LP = DATA_DIR / "repair_strategy.lp"

# 审计与结果路径
FACTS_LP = RESULT_DIR / "facts.lp"
REPAIR_RESULT = RESULT_DIR / "repair_result.txt"

# ==========================================
# 5. 配置单例对象 (方便新模块调用)
# ==========================================
class Config:
    def __init__(self):
        # 批量把上面的变量塞进来
        for key, value in globals().items():
            if key.isupper():
                setattr(self, key, value)

# 实例化对象，让 from src.config import config 生效
config = Config()

# ==========================================
# 6. 辅助工具：获取 Neo4j 客户端
# ==========================================
def get_neo4j_client():
    from src.store.neo4j_client import Neo4jClient
    return Neo4jClient(uri=NEO4J_URI, user=NEO4J_USER, password=NEO4J_PWD)

print(f"✅ 配置文件加载成功 | 模式: 本地 QWEN ({LOCAL_MODEL_NAME}) | 数据库: {NEO4J_URI}")