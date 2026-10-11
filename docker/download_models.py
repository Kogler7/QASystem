"""Download the document-parsing models into the externally mounted /models."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


MODELS_DIR = Path("/models")
DOCLING_DIR = MODELS_DIR / "docling-models"
MINERU_PIPELINE_DIR = MODELS_DIR / "mineru" / "pipeline"
MINERU_VLM_DIR = MODELS_DIR / "mineru" / "vlm"
EMBEDDING_MODELS = {
    "bge_small_zh": ("BAAI/bge-small-zh-v1.5", MODELS_DIR / "bge-small-zh-v1.5"),
}
JEV_MODEL = ("C-Tianyu/NanoJev", MODELS_DIR / "nanojev")
JEV_REVISION = "unified-games-v1"
CLEF_MODEL = ("Cloudflare/clef-flash", MODELS_DIR / "clef-flash")
CLEF_REVISION = "17f0b0ad64efb65d273590632833508766b2aae6"
JEV_ALLOW_PATTERNS = (
    "best.safetensors",
    "config.json",
    "backbone_config/**",
    "tokenizer/**",
    "source/**",
)

MINERU_REPOSITORIES = {
    "huggingface": {
        "pipeline": "opendatalab/PDF-Extract-Kit-1.0",
        "vlm": "opendatalab/MinerU2.5-2509-1.2B",
    },
    "modelscope": {
        "pipeline": "OpenDataLab/PDF-Extract-Kit-1.0",
        "vlm": "OpenDataLab/MinerU2.5-2509-1.2B",
    },
}


def progress(message: str) -> None:
    print(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}", flush=True)


def _download_clef_model() -> None:
    from huggingface_hub import snapshot_download

    repository, destination = CLEF_MODEL
    destination.mkdir(parents=True, exist_ok=True)
    snapshot_download(repo_id=repository, revision=CLEF_REVISION, local_dir=destination)
    required = ["config.json", "joint_schema_model.py", "joint_head.safetensors",
                "joint_head_config.json", "tokenizer.json", "tokenizer_config.json",
                "processor_config.json", "model.safetensors.index.json"]
    missing = [name for name in required if not (destination / name).is_file()]
    if missing:
        raise RuntimeError(f"Clef download incomplete: {missing}")
    index = json.loads((destination / "model.safetensors.index.json").read_text())
    shards = set(index.get("weight_map", {}).values())
    if not shards or any(not (destination / name).is_file() for name in shards):
        raise RuntimeError("Clef download missing backbone shards")


def _download_mineru(source: str, model_type: str, destination: Path) -> None:
    repository = MINERU_REPOSITORIES[source][model_type]
    destination.mkdir(parents=True, exist_ok=True)
    if source == "huggingface":
        from huggingface_hub import snapshot_download

        snapshot_download(repo_id=repository, local_dir=destination)
    else:
        from modelscope import snapshot_download

        snapshot_download(model_id=repository, local_dir=str(destination))


def _download_embedding_models() -> None:
    from huggingface_hub import snapshot_download

    for repository, destination in EMBEDDING_MODELS.values():
        destination.mkdir(parents=True, exist_ok=True)
        snapshot_download(repo_id=repository, local_dir=destination)


def _download_jev_model() -> None:
    from huggingface_hub import snapshot_download

    repository, destination = JEV_MODEL
    destination.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=repository,
        revision=JEV_REVISION,
        local_dir=destination,
        allow_patterns=list(JEV_ALLOW_PATTERNS),
    )
    required_files = ("best.safetensors", "config.json", "source/scripts/predict_toy_decisions.py",
                      "source/scripts/train_toy_decisions.py")
    missing = [name for name in required_files if not (destination / name).is_file()]
    missing.extend(name for name in ("backbone_config", "tokenizer")
                   if not (destination / name).is_dir() or not any((destination / name).iterdir()))
    if missing:
        raise RuntimeError(f"NanoJev download incomplete: {missing}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=tuple(MINERU_REPOSITORIES), default="modelscope")
    parser.add_argument("--skip-docling", action="store_true")
    parser.add_argument("--skip-mineru-pipeline", action="store_true")
    parser.add_argument("--skip-mineru-vlm", action="store_true")
    parser.add_argument("--skip-embedding", action="store_true")
    parser.add_argument(
        "--jev",
        action="store_true",
        help="Download C-Tianyu/NanoJev into /models/nanojev",
    )
    parser.add_argument("--clef", action="store_true", help="Download pinned Clef-flash into /models/clef-flash")
    args = parser.parse_args()

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    if not args.skip_docling:
        progress("Downloading Docling")
        from docling.utils.model_downloader import download_models

        download_models(output_dir=DOCLING_DIR, progress=True)
    if not args.skip_mineru_pipeline:
        progress("Downloading MinerU pipeline")
        _download_mineru(args.source, "pipeline", MINERU_PIPELINE_DIR)
    if not args.skip_mineru_vlm:
        progress("Downloading MinerU VLM")
        _download_mineru(args.source, "vlm", MINERU_VLM_DIR)
    if not args.skip_embedding:
        progress("Downloading embeddings")
        _download_embedding_models()
    if args.jev:
        progress("Downloading NanoJev")
        _download_jev_model()
    if args.clef:
        progress("Downloading Clef-flash")
        _download_clef_model()

    expected = {
        "Docling": DOCLING_DIR,
        "MinerU Pipeline": MINERU_PIPELINE_DIR,
        "MinerU VLM": MINERU_VLM_DIR,
        "BGE-small-zh": EMBEDDING_MODELS["bge_small_zh"][1],
    }
    skipped = {
        "Docling": args.skip_docling,
        "MinerU Pipeline": args.skip_mineru_pipeline,
        "MinerU VLM": args.skip_mineru_vlm,
        "BGE-small-zh": args.skip_embedding,
    }
    if args.jev:
        expected["Jev"] = JEV_MODEL[1]
        skipped["Jev"] = False
    if args.clef:
        expected["Clef"] = CLEF_MODEL[1]
        skipped["Clef"] = False
    for name, path in expected.items():
        if not skipped[name] and not any(path.iterdir()):
            raise RuntimeError(f"{name} model download produced no files: {path}")
        if not skipped[name]:
            progress(f"{name}: {path}")


if __name__ == "__main__":
    main()
