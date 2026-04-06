import os
from dotenv import load_dotenv
from pathlib import Path

# .env 在 intent_recognition_model 根目录
path = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=path / ".env")


class Config:
    def __init__(self):
        _root = Path(__file__).resolve().parent.parent  # intent_recognition_model
        _project_root = _root.parent.parent  # 仓库根 qasystem
        _default_model_dir = Path(
            os.getenv("INTENT_MODEL_BASE_DIR", str(_project_root / "models"))
        )

        self.data_dir = os.getenv("DATA_DIR") or str(_root / "data" / "intent.json")
        self.collection_name = os.getenv("COLLECTION_NAME", "intent_rag")
        self.milvus_uri = os.getenv("MILVUS_URI", "http://localhost:19530")
        self.milvus_consistency_level = os.getenv("MILVUS_CONSISTENCY_LEVEL", "Strong")
        self.max_drop_retries = int(os.getenv("MAX_DROP_RETRIES", "3"))
        self.drop_retry_delay = int(os.getenv("DROP_RETRY_DELAY", "2"))
        self.model_path = os.getenv("MODEL_PATH") or str(_default_model_dir / "bge_m3")
        self.log_path = os.getenv("LOG_PATH") or str(_project_root / "logs")
        # LLM 配置：与 algorithm/common/llm_client 环境变量统一，便于一处配置多处生效
        self.api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.llm_base_url = os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "http://localhost:9999"
        self.llm_model = os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "Qwen/Qwen3-8B"
        # Neo4j 配置
        self.neo4j_uri = os.getenv("NEO4J_URI") or "neo4j://localhost:7687"
        self.neo4j_username = (
            os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER") or "neo4j"
        )
        self.neo4j_password = os.getenv("NEO4J_PASSWORD") or "neo4j2025"


if __name__ == "__main__":
    c = Config()
    print(c.milvus_uri, c.collection_name, c.model_path, c.log_path, c.neo4j_uri)
