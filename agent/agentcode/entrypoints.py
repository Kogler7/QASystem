"""可选薄封装：路由层可只调 `run_chat_session`，避免直接依赖 `AgentRuntime` 方法名。"""

from __future__ import annotations

from .models import FinalResult, SessionConfig
from .runtime import AgentRuntime


def run_chat_session(runtime: AgentRuntime, config: SessionConfig, prompt: str) -> FinalResult:
    """Thin host entrypoint for generic chat route integration."""
    return runtime.run_session(config=config, user_message=prompt)


def run_answer_generation(runtime: AgentRuntime, config: SessionConfig, question: str) -> FinalResult:
    """Thin host entrypoint reserved for answer_generation flows."""
    return runtime.run_session(config=config, user_message=question)
