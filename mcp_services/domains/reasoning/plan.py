from typing import Any

from mcp_services.runtime import mcp
from mcp_services.result import ToolCode, invoke_tool


@mcp.tool(name="get_plan_from_intent")
def get_plan_from_intent(intent_id: str, domain: str) -> dict[str, Any]:
    """Build a problem-solving plan from an intent id and business domain."""
    from algorithm.intent_recognition_model.services.plan import PlanExecutor

    return invoke_tool(
        lambda: PlanExecutor().get_plan_from_intent(intent_id, domain),
        ToolCode.REASONING_FAILED,
        "方案生成完成",
    )
