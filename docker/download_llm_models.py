"""Provision or verify revision-pinned LLM weights in the shared model root."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path


MANIFEST = Path(__file__).with_name("llm-models.json")


def progress(message: str) -> None:
    print(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}", flush=True)


def verify(directory: Path, files: list[dict]) -> None:
    for entry in files:
        path = directory / entry["rfilename"]
        if not path.is_file() or path.stat().st_size != entry["size"]:
            raise RuntimeError(f"Missing or incomplete model file: {path}")
        expected = entry.get("lfs", {}).get("sha256")
        if expected:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise RuntimeError(f"SHA256 mismatch: {path}")
        elif entry.get("blobId"):
            data = path.read_bytes()
            digest = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
            if digest != entry["blobId"]:
                raise RuntimeError(f"Git blob checksum mismatch: {path}")


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-dir", type=Path, default=Path("/models"))
    parser.add_argument("--model", action="append", choices=[item["repo"] for item in manifest])
    parser.add_argument("--verify-only", action="store_true", help="Offline; never access the network")
    args = parser.parse_args()
    for item in manifest:
        if args.model and item["repo"] not in args.model:
            continue
        directory = args.models_dir / "llm" / item["repo"].replace("/", "--")
        if not args.verify_only:
            from huggingface_hub import snapshot_download
            progress(f"Download {item['repo']} revision={item['revision']} into {directory}")
            snapshot_download(repo_id=item["repo"], revision=item["revision"], local_dir=directory,
                              allow_patterns=[entry["rfilename"] for entry in item["files"]])
        progress(f"Verify sizes and weight SHA256: {directory}")
        verify(directory, item["files"])
        progress(f"Ready: {item['repo']}")


if __name__ == "__main__":
    main()
