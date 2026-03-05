import os
from dotenv import load_dotenv
from pathlib import Path
path = Path(__file__).parent.parent
load_dotenv(dotenv_path=path / '.env')


class Config:
    def __init__(self):
        self.data_dir = os.getenv("DATA_DIR")
        self.collection_name = os.getenv("COLLECTION_NAME")
        self.milvus_uri = os.getenv("MILVUS_URI")
        self.milvus_consistency_level = os.getenv("MILVUS_CONSISTENCY_LEVEL")
        self.max_drop_retries = os.getenv("MAX_DROP_RETRIES")
        self.drop_retry_delay = os.getenv("DROP_RETRY_DELAY")
        # 项目根（config 在 algorithm/intent_recognition_model/config/）
        _project_root = Path(__file__).resolve().parent.parent.parent.parent
        # 默认 BGE-M3 模型路径（可通过 MODEL_PATH 覆盖）
        _default_model_dir = Path("/mnt/data3/srv/qasystem/models")
        self.model_path = os.getenv("MODEL_PATH") or str(_default_model_dir / "bge_m3")
        self.log_path = os.getenv("LOG_PATH") or str(_project_root / "logs")
        self.api_key = os.getenv("DEEPSEEK_API_KEY")

if __name__ == "__main__":
    config = Config()
    print(config.milvus_uri)
    print(config.collection_name)
    print(config.data_dir)
    print(config.milvus_consistency_level)
    print(config.max_drop_retries)
    print(config.drop_retry_delay)
    print(config.model_path)
    print(config.log_path)