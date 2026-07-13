from typing import Any

from mcp_services.runtime import mcp
from mcp_services.result import ToolCode, invoke_tool, tool_error, tool_success


@mcp.tool(name="recognize_intent")
def recognize_intent(problem_model: str | dict[str, Any]) -> dict[str, Any]:
    """Recognize the business intent represented by a question or problem model."""
    return invoke_tool(
        lambda: _recognize(problem_model),
        ToolCode.REASONING_FAILED,
        "意图识别",
    )


def _recognize(problem_model):
    from algorithm.intent_recognition_model.services.intent import IntentRecognitionExecutor

    return IntentRecognitionExecutor().recognize(problem_model)


@mcp.tool(name="batch_recognize_intent")
def batch_recognize_intent(
    requests: list[str | dict[str, Any]],
    stop_on_failure: bool = False,
) -> dict[str, Any]:
    """Recognize intents for multiple problem models."""
    import time

    started = time.monotonic()
    if not requests:
        return tool_error(ToolCode.INVALID_ARGUMENT, "requests 不能为空")
    try:
        from algorithm.intent_recognition_model.services.intent import IntentRecognitionExecutor

        executor = IntentRecognitionExecutor()
        results = []
        for request in requests:
            result = executor.recognize(request)
            results.append(result)
            if stop_on_failure and not result.get("pass", False):
                break
        return tool_success(
            {
                "results": results,
                "all_successful": bool(results) and all(result.get("pass", False) for result in results),
                "total_time_ms": round((time.monotonic() - started) * 1000, 2),
            },
            "批量意图识别完成",
        )
    except Exception as exc:
        return tool_error(
            ToolCode.REASONING_FAILED,
            "批量意图识别失败",
            {"type": type(exc).__name__, "message": str(exc)},
        )
