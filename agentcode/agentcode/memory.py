from __future__ import annotations

import json
import os
import uuid
from typing import Protocol

from .models import AgentState


class MemoryStore(Protocol):
    def save_checkpoint(self, state: AgentState) -> str:
        """Persist state and return checkpoint id."""

    def load_checkpoint(self, checkpoint_id: str) -> AgentState:
        """Load state by checkpoint id."""


class NoopMemoryStore:
    def __init__(self) -> None:
        self._states: dict[str, AgentState] = {}

    def save_checkpoint(self, state: AgentState) -> str:
        checkpoint_id = str(uuid.uuid4())
        self._states[checkpoint_id] = state.model_copy(deep=True)
        return checkpoint_id

    def load_checkpoint(self, checkpoint_id: str) -> AgentState:
        if checkpoint_id not in self._states:
            raise KeyError(f"Unknown checkpoint_id: {checkpoint_id}")
        return self._states[checkpoint_id].model_copy(deep=True)


class JsonlMemoryStore:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def save_checkpoint(self, state: AgentState) -> str:
        checkpoint_id = str(uuid.uuid4())
        payload = {
            "checkpoint_id": checkpoint_id,
            "state": state.model_dump(mode="json"),
        }
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return checkpoint_id

    def load_checkpoint(self, checkpoint_id: str) -> AgentState:
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                payload = json.loads(line)
                if payload.get("checkpoint_id") == checkpoint_id:
                    return AgentState.model_validate(payload["state"])
        raise KeyError(f"Unknown checkpoint_id: {checkpoint_id}")
