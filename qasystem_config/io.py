"""Pure configuration text parsing and atomic file I/O."""
import json
import os
from pathlib import Path
import re
import tempfile

def read_env(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if match:
            value = match[2]
            if value.startswith('"') and value.endswith('"'):
                try:
                    value = json.loads(value)
                except ValueError:
                    value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0]
            values[match[1]] = value
    return values


def update_env(text: str, changes: dict[str, str]) -> str:
    pending = dict(changes)
    lines = []
    for line in text.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", line)
        if match and match[1] in changes:
            key = match[1]
            if key not in pending:
                continue  # Remove duplicate definitions of changed keys.
            lines.append(f"{key}={json.dumps(pending.pop(key), ensure_ascii=False)}")
        else:
            lines.append(line)
    lines.extend(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in pending.items())
    return "\n".join(lines) + "\n"


def atomic_write(path: Path, text: str) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".llm-env-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


