"""vLLM healthcheck that performs one process-scoped multimodal warmup."""

from __future__ import annotations

import base64
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path


def _request(url: str, payload: dict | None = None, timeout: float = 10) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return {}


def _process_start_id() -> str:
    try:
        server_pid = os.getenv("VLLM_SERVER_PID", "1")
        fields = Path(f"/proc/{server_pid}/stat").read_text(encoding="ascii").split()
        return fields[21]
    except (OSError, IndexError):
        return str(os.getpid())


def _png_data_url() -> str:
    width = height = 32
    rows = b"".join(b"\x00" + bytes([255, 255, 255]) * width for _ in range(height))
    def chunk(name: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + name + body + struct.pack(">I", zlib.crc32(name + body) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def main() -> int:
    if os.getenv("VLLM_WARMUP_ENABLED", "true").strip().lower() in {"0", "false", "off", "no"}:
        return 0
    base_url = os.getenv("VLLM_WARMUP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    timeout = float(os.getenv("VLLM_WARMUP_TIMEOUT_SECONDS", "180"))
    marker = Path(os.getenv("VLLM_WARMUP_MARKER", "/tmp/qasystem-vllm-warmup.done"))
    process_id = _process_start_id()
    try:
        _request(f"{base_url}/health", timeout=min(timeout, 10))
        models = _request(f"{base_url}/v1/models", timeout=min(timeout, 10))
        model_name = os.getenv("VLLM_SERVED_MODEL_NAME", "")
        if not model_name:
            model_name = str(((models.get("data") or [{}])[0]).get("id") or "mineru-vlm")
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == process_id:
            return 0
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Return OK."},
                {"type": "image_url", "image_url": {"url": _png_data_url()}},
            ]}],
            "max_tokens": 1,
            "temperature": 0,
        }
        started = time.monotonic()
        _request(f"{base_url}/v1/chat/completions", payload, timeout=timeout)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(process_id, encoding="utf-8")
        print(f"vLLM warmup complete in {time.monotonic() - started:.1f}s", flush=True)
        return 0
    except (OSError, ValueError, KeyError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"vLLM readiness/warmup failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
