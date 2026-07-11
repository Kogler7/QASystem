from typing import Any

from mcp_services.runtime import mcp


@mcp.tool(name="recognize_intent")
def recognize_intent(problem_model: str | dict[str, Any]) -> dict[str, Any]:
    """Recognize the business intent represented by a question or problem model."""
    from algorithm.intent_recognition_model.services.intent import IntentRecognitionExecutor

    return IntentRecognitionExecutor().recognize(problem_model)


@mcp.tool(name="batch_recognize_intent")
def batch_recognize_intent(
    requests: list[str | dict[str, Any]],
    stop_on_failure: bool = False,
) -> dict[str, Any]:
    """Recognize intents for multiple problem models."""
    import time

    from algorithm.intent_recognition_model.services.intent import IntentRecognitionExecutor

    started = time.monotonic()
    executor = IntentRecognitionExecutor()
    results = []
    for request in requests:
        result = executor.recognize(request)
        results.append(result)
        if stop_on_failure and not result.get("pass", False):
            break
    return {
        "results": results,
        "all_successful": all(result.get("pass", False) for result in results),
        "total_time_ms": round((time.monotonic() - started) * 1000, 2),
    }
