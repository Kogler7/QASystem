from fastmcp.server import FastMCP
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Union
from .executor import IntentRecognitionExecutor, execute_intent_recognition

app = FastMCP(
    name="Intent Recognition MCP",
    version="1.0.0"
)


class IntentRecognitionRequest(BaseModel):
    problem_model: Union[str, Dict[str, Any]] = Field(
        ...,
        description="问题模型，可以是字符串或字典。\n"
                    "字符串示例: '最近 12 个月的采购金额月度走势是怎样的？'\n"
                    "字典示例: {\"problem_id\": \"QKB-001\", \"description\": \"...\"}"
    )


@app.tool(
    name="recognize_intent",
    description="执行意图识别，根据用户输入识别对应的业务意图",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def recognize_intent(request: IntentRecognitionRequest) -> Dict[str, Any]:
    try:
        executor = IntentRecognitionExecutor()
        return executor.recognize(request.problem_model)
    except Exception as e:
        return {
            "understanding": "意图识别异常",
            "pass": False,
            "intent": {},
            "detail": {},
            "error_message": str(e),
            "execution_time_ms": 0.0
        }


class BatchIntentRecognitionRequest(BaseModel):
    requests: List[IntentRecognitionRequest] = Field(..., description="批量识别请求")
    stop_on_failure: Optional[bool] = Field(False, description="是否遇到失败就停止")


@app.tool(
    name="batch_recognize_intent",
    description="批量执行意图识别",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def batch_recognize_intent(request: BatchIntentRecognitionRequest) -> Dict[str, Any]:
    import time
    start_time = time.monotonic()
    results = []
    all_successful = True
    
    try:
        executor = IntentRecognitionExecutor()
        for req in request.requests:
            result = executor.recognize(req.problem_model)
            results.append(result)
            if not result.get("pass", False) and request.stop_on_failure:
                all_successful = False
                break
        else:
            all_successful = all(r.get("pass", False) for r in results)
    except Exception as e:
        all_successful = False
        results.append({
            "understanding": "批量识别异常",
            "pass": False,
            "intent": {},
            "detail": {},
            "error_message": str(e),
            "execution_time_ms": 0.0
        })
    
    total_time = (time.monotonic() - start_time) * 1000
    
    return {
        "results": results,
        "all_successful": all_successful,
        "total_time_ms": round(total_time, 2)
    }


if __name__ == "__main__":
    import asyncio
    print("🟢 启动 Intent Recognition MCP HTTP 服务...")
    print(f"📡 服务地址: http://0.0.0.0:8890")
    asyncio.run(app.run_http_async(host="0.0.0.0", port=8891))
