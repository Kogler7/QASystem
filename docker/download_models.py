"""Download the document-parsing models into the externally mounted /models."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


MODELS_DIR = Path("/models")
DOCLING_DIR = MODELS_DIR / "docling-models"
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

MINERU_SOURCES = ("huggingface", "modelscope")



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


def _download_mineru(source: str, settings: dict, tier: str) -> dict:
    """Prepare 4.x resources, then write completion receipts after validation."""
    if source not in MINERU_SOURCES:
        raise ValueError("Unsupported MinerU download source")
    from qasystem_config.mineru_adapter import required_repositories, model_report
    repositories = required_repositories(settings, tier)
    base = Path(settings['base_dir'])
    (base / '.locks').mkdir(parents=True, exist_ok=True)
    for name in repositories:
        folder = base / name
        folder.mkdir(parents=True, exist_ok=True)
        marker = folder / '.mineru_complete'
        marker.unlink(missing_ok=True)
        owner = 'jinzhenj' if name.endswith('-GGUF') else 'opendatalab' if source == 'huggingface' else 'OpenDataLab'
        repository = owner + '/' + name
        progress('Downloading MinerU 4: ' + repository)
        if source == 'huggingface':
            from huggingface_hub import snapshot_download
            snapshot_download(repo_id=repository, local_dir=str(folder))
        else:
            from modelscope import snapshot_download
            snapshot_download(model_id=repository, local_dir=str(folder))
    report = model_report(settings, tier, require_receipt=False)
    if not report['ok']:
        raise RuntimeError('MinerU 4 download incomplete: ' + '; '.join(report['missing'] + report['invalid']))
    for name in repositories:
        (base / name / '.mineru_complete').touch()
    progress('MinerU 4 model files validated: ' + str(report['checked_files']))
    return report


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
    parser.add_argument("--source", choices=MINERU_SOURCES, default="modelscope")
    parser.add_argument("--skip-docling", action="store_true")
    parser.add_argument("--skip-mineru", action="store_true", help="Skip MinerU 4 model preparation")
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
    if not args.skip_mineru:
        from qasystem_config.mineru_adapter import settings
        from qasystem_config.configuration import getenv
        _download_mineru(args.source, settings(), getenv('MINERU4_TIER'))
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
        "BGE-small-zh": EMBEDDING_MODELS["bge_small_zh"][1],
    }
    skipped = {
        "Docling": args.skip_docling,
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
