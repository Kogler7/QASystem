"""Download the document-parsing models into the externally mounted /models."""

from __future__ import annotations

import argparse
from pathlib import Path


MODELS_DIR = Path("/models")
DOCLING_DIR = MODELS_DIR / "docling-models"
MINERU_PIPELINE_DIR = MODELS_DIR / "mineru" / "pipeline"
MINERU_VLM_DIR = MODELS_DIR / "mineru" / "vlm"
EMBEDDING_MODELS = {
    "bge_small_zh": ("BAAI/bge-small-zh-v1.5", MODELS_DIR / "bge-small-zh-v1.5"),
}

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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=tuple(MINERU_REPOSITORIES), default="modelscope")
    parser.add_argument("--skip-docling", action="store_true")
    parser.add_argument("--skip-mineru-pipeline", action="store_true")
    parser.add_argument("--skip-mineru-vlm", action="store_true")
    parser.add_argument("--skip-embedding", action="store_true")
    args = parser.parse_args()

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    if not args.skip_docling:
        from docling.utils.model_downloader import download_models

        download_models(output_dir=DOCLING_DIR, progress=True)
    if not args.skip_mineru_pipeline:
        _download_mineru(args.source, "pipeline", MINERU_PIPELINE_DIR)
    if not args.skip_mineru_vlm:
        _download_mineru(args.source, "vlm", MINERU_VLM_DIR)
    if not args.skip_embedding:
        _download_embedding_models()

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
    for name, path in expected.items():
        if not skipped[name] and not any(path.iterdir()):
            raise RuntimeError(f"{name} model download produced no files: {path}")
        if not skipped[name]:
            print(f"{name}: {path}", flush=True)


if __name__ == "__main__":
    main()
