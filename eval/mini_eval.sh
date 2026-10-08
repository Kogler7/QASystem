#!/bin/bash
set -euo pipefail

KIT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "${KIT_DIR}/mini_eval.py" "$@"
