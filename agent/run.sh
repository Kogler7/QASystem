#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

if [ -f .env ]; then
    set -a
    source <(grep -E '^(AGENT_LLM_API_KEY|AGENT_TEMPLATE_MAX_TOKENS|AGENT_TEMPLATE_TEMPERATURE|AGENT_TEMPLATE_TIMEOUT_SECONDS|AGENT_TEMPLATE_TOOL_CHOICE)=' .env)
    set +a
fi

if command -v conda &>/dev/null; then
    source "$(conda info --base 2>/dev/null || echo /opt/anaconda3)/etc/profile.d/conda.sh" 2>/dev/null || true
    conda activate qasystem 2>/dev/null || true
fi

PYTHONPATH="$(pwd):$(pwd)/agent"
export PYTHONPATH

exec python -m agentcode.cli "$@"
