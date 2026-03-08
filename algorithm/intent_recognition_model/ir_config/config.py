import os
from dotenv import load_dotenv
from pathlib import Path

# .env 在 intent_recognition_model 根目录
path = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=path / ".env")


class Config:
    def __init__(self):
        _root = Path(__file__).resolve().parent.parent  # intent_recognition_model
        _project_root = _root.parent.parent  # 项目根 qasystem
        _default_model_dir = Path("/mnt/data3/srv/qasystem/models")

        self.data_dir = os.getenv("DATA_DIR") or str(_root / "data" / "intent.json")
        self.collection_name = os.getenv("COLLECTION_NAME", "intent_rag")
        self.milvus_uri = os.getenv("MILVUS_URI", "http://localhost:19530")
        self.milvus_consistency_level = os.getenv("MILVUS_CONSISTENCY_LEVEL", "Strong")
        self.max_drop_retries = int(os.getenv("MAX_DROP_RETRIES", "3"))
        self.drop_retry_delay = int(os.getenv("DROP_RETRY_DELAY", "2"))
        self.model_path = os.getenv("MODEL_PATH") or str(_default_model_dir / "bge_m3")
        self.log_path = os.getenv("LOG_PATH") or str(_project_root / "logs")


if __name__ == "__main__":
    c = Config()
    print(c.milvus_uri, c.collection_name, c.model_path, c.log_path)
