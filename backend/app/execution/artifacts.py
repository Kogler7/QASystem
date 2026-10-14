from __future__ import annotations
from docker.startup_contract import execution_data_directory

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class ArtifactStore:
    """Shared-filesystem artifact store used by the first worker deployment.

    The path is deliberately a reference in task messages. Production can replace
    this implementation with an S3/MinIO adapter without changing task contracts.
    """

    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = Path(root) if root is not None else execution_data_directory()
        self.payloads = self.root / "payloads"
        self.results = self.root / "results"
        self.blobs = self.root / "blobs"
        self.payloads.mkdir(parents=True, exist_ok=True)
        self.results.mkdir(parents=True, exist_ok=True)
        self.blobs.mkdir(parents=True, exist_ok=True)

    def _write(self, directory: Path, job_id: str, value: Any) -> str:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{job_id}.json"
        fd, temp_name = tempfile.mkstemp(prefix=f".{job_id}.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return str(target)

    @staticmethod
    def _read(reference: str | None) -> Any:
        if not reference:
            return None
        with Path(reference).open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def write_payload(self, job_id: str, value: Any) -> str:
        return self._write(self.payloads, job_id, value)

    def read_payload(self, reference: str | None) -> Any:
        return self._read(reference)

    def write_result(self, job_id: str, value: Any) -> str:
        return self._write(self.results, job_id, value)

    def read_result(self, reference: str | None) -> Any:
        return self._read(reference)

    def remove(self, reference: str | None) -> None:
        if reference:
            try:
                Path(reference).unlink(missing_ok=True)
            except OSError:
                pass

    def write_blob(self, blob_id: str, value: bytes) -> str:
        """Persist large binary input once for page-shard workers to share."""
        target = self.blobs / f"{blob_id}.bin"
        fd, temp_name = tempfile.mkstemp(prefix=f".{blob_id}.", suffix=".tmp", dir=self.blobs)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return str(target)

    @staticmethod
    def read_blob(reference: str | None) -> bytes:
        if not reference:
            return b""
        return Path(reference).read_bytes()

    def remove_blob(self, reference: str | None) -> None:
        self.remove(reference)
