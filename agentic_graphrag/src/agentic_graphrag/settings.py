from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    tool_client: str = os.getenv("AGENTIC_GRAPHRAG_TOOL_CLIENT", "local_mock")
    mcp_config: str = os.getenv("AGENTIC_GRAPHRAG_MCP_CONFIG", "./configs/mcp_servers.example.json")
    max_depth: int = int(os.getenv("AGENTIC_GRAPHRAG_MAX_DEPTH", "3"))
    max_evidence: int = int(os.getenv("AGENTIC_GRAPHRAG_MAX_EVIDENCE", "12"))


settings = Settings()
