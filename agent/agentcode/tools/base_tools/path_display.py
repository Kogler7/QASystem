from __future__ import annotations

import json
from typing import Any


class PathDisplayTool:
    """Expose the Skill-facing solution-path display as a local Agent tool.

    Skills call ``problem-resolve-path-display`` after semantic-align and
    plan-execute. The tool does not invent a path; it validates and echoes
    the Agent-supplied fields so SSE clients can render a stable process event.
    """

    TOOL_NAME = "problem-resolve-path-display"

    def __init__(self) -> None:
        self._tool_schemas: dict[str, dict[str, Any]] = {}
        self._tool_handlers: dict[str, Any] = {}
        self._register()

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return list(self._tool_schemas.values())

    def get_tool_definitions(self) -> dict[str, dict[str, Any]]:
        return dict(self._tool_schemas)

    def get_tool_handlers(self) -> dict[str, Any]:
        return dict(self._tool_handlers)

    def _register(self) -> None:
        self._tool_schemas[self.TOOL_NAME] = {
            "name": self.TOOL_NAME,
            "description": (
                "向调用方展示本次问题求解路径。仅在 semantic-align 与 plan-execute "
                "均已完成后调用。solution_path 必须原样取自方案类模板步骤，"
                "不要改写、不要混入导航路径、节点 ID 或工具名。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "用户原始问题",
                    },
                    "selected_tools": {
                        "description": "实际使用的工具列表，仅内部参考",
                    },
                    "tool_list": {
                        "description": "可用工具列表，仅内部参考",
                    },
                    "reasoning_summary": {
                        "type": "string",
                        "description": "问题求解路径的简要说明",
                    },
                    "solution_path": {
                        "description": "方案类模板中的步骤文本，按模板顺序原样给出",
                    },
                },
                "required": ["question", "solution_path"],
            },
        }
        self._tool_handlers[self.TOOL_NAME] = self._make_handler()

    def _make_handler(self):
        def handler(**kwargs: Any) -> str:
            kwargs.pop("_agent_timeout_s", None)
            kwargs.pop("_agent_cancel_token", None)
            question = kwargs.get("question")
            solution_path = kwargs.get("solution_path")
            if not isinstance(question, str) or not question.strip():
                return "Error: question is required"
            if solution_path is None or solution_path == "":
                return "Error: solution_path is required"
            payload = {
                "question": question.strip(),
                "selected_tools": kwargs.get("selected_tools") or [],
                "tool_list": kwargs.get("tool_list") or [],
                "reasoning_summary": kwargs.get("reasoning_summary") or "",
                "solution_path": solution_path,
            }
            return json.dumps(payload, ensure_ascii=False)

        return handler
