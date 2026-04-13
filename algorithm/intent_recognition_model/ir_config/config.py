import os
from typing import Optional

from dotenv import load_dotenv
from pathlib import Path

# 单独运行本目录脚本时加载同级 .env（不覆盖已有环境变量）。
# 通过 FastAPI 启动后端时，环境已由 backend/app/config.py 加载 backend/.env 与仓库根 .env。
path = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=path / ".env", override=False)


def _intent_models_base_dir(project_root: Path) -> Path:
    raw = os.getenv("INTENT_MODEL_BASE_DIR", str(project_root / "models"))
    base = Path(raw)
    if not base.is_absolute():
        base = (project_root / base).resolve()
    else:
        base = base.resolve()
    return base


def _looks_like_hf_model_dir(d: Path) -> bool:
    """判断目录是否像已解压的 Transformers 模型根目录。"""
    if not d.is_dir():
        return False
    return (
        (d / "config.json").is_file()
        or (d / "tokenizer_config.json").is_file()
        or (d / "pytorch_model.bin").is_file()
        or (d / "model.safetensors").is_file()
    )


def _default_bge_dir(base_dir: Path) -> str:
    """
    在 INTENT_MODEL_BASE_DIR 下解析模型目录：
    - 若 base_dir 本身就是模型目录（直接指向 bge_m3_model 等），用它；
    - 否则在 base_dir 下依次找子目录 bge_m3、bge_m3_model。
    """
    if _looks_like_hf_model_dir(base_dir):
        return str(base_dir.resolve())
    for name in ("bge_m3", "bge_m3_model"):
        p = (base_dir / name).resolve()
        if p.is_dir():
            return str(p)
    return str((base_dir / "bge_m3").resolve())


def _resolve_explicit_model_path(raw: str, project_root: Path) -> str:
    """仅根据 MODEL_PATH 解析出候选路径（不检查是否存在）。"""
    p = Path(raw)
    if p.is_absolute():
        return str(p.resolve())
    cand_cwd = (Path.cwd() / raw).resolve()
    cand_root = (project_root / raw).resolve()
    if cand_cwd.is_dir():
        return str(cand_cwd)
    if cand_root.is_dir():
        return str(cand_root)
    if raw.startswith("..") or raw.startswith("./") or raw.startswith(".\\"):
        return str(cand_cwd)
    return str(cand_root)


def _resolve_model_path(raw: Optional[str], project_root: Path) -> str:
    """
    BGE-M3 模型目录：
    - 未配置 MODEL_PATH：仅用 INTENT_MODEL_BASE_DIR（默认 仓库根/models）解析；
    - 配置了 MODEL_PATH 且目录存在：使用该路径；
    - 配置了 MODEL_PATH 但目录不存在：回退到 INTENT_MODEL_BASE_DIR（避免根目录 .env 里
      遗留的 MODEL_PATH=../bge_m3_model 覆盖掉 INTENT_MODEL_BASE_DIR）。
    """
    fallback = _default_bge_dir(_intent_models_base_dir(project_root))

    if raw is not None:
        raw = raw.strip()
    if not raw:
        return fallback

    candidate = _resolve_explicit_model_path(raw, project_root)
    if Path(candidate).is_dir():
        return candidate
    if Path(fallback).is_dir():
        return fallback
    return candidate


class Config:
    def __init__(self):
        _root = Path(__file__).resolve().parent.parent  # intent_recognition_model
        _project_root = _root.parent.parent  # 仓库根 qasystem

        self.data_dir = os.getenv("DATA_DIR") or str(_root / "data" / "intent.json")
        self.collection_name = os.getenv("COLLECTION_NAME", "intent_rag")
        self.milvus_uri = os.getenv("MILVUS_URI", "http://localhost:19530")
        self.milvus_consistency_level = os.getenv("MILVUS_CONSISTENCY_LEVEL", "Strong")
        self.max_drop_retries = int(os.getenv("MAX_DROP_RETRIES", "3"))
        self.drop_retry_delay = int(os.getenv("DROP_RETRY_DELAY", "2"))
        self.model_path = _resolve_model_path(os.getenv("MODEL_PATH"), _project_root)
        self.log_path = os.getenv("LOG_PATH") or str(_project_root / "logs")
        # LLM 配置：与 algorithm/common/llm_client 环境变量统一，便于一处配置多处生效
        self.api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "qwen"
        self.llm_base_url = os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "http://localhost:9999"
        self.llm_model = os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "Qwen/Qwen3-8B"
        # Neo4j 配置
        self.neo4j_uri = os.getenv("NEO4J_URI") or "neo4j://localhost:7687"
        self.neo4j_username = os.getenv("NEO4J_USERNAME") or "neo4j"
        self.neo4j_password = os.getenv("NEO4J_PASSWORD") or "neo4j2025"
        # Tools file path
        self.tools_file = os.getenv("TOOLS_FILE") or str(_root / "data" / "tools.md")


if __name__ == "__main__":
    c = Config()
    print(c.milvus_uri, c.collection_name, c.model_path, c.log_path)
