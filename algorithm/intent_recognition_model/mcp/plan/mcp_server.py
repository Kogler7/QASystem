from fastmcp.server import FastMCP
from pydantic import BaseModel, Field
from typing import Dict, Any, Union
from .executor import PlanExecutor

app = FastMCP(
    name="Problem Solving Plan MCP",
    version="1.0.0"
)


class GetPlanFromIntentRequest(BaseModel):
    intent_id: str = Field(..., description="意图 ID，来自意图识别结果 intent.intent_id 字段")
    domain: str = Field(..., description="域名称，来自意图识别结果 intent.domain 字段")


def _normalize_plan_args(data: Union[Dict[str, Any], BaseModel]) -> GetPlanFromIntentRequest:
    """将 LLM 可能传来的各种参数格式归一化为 GetPlanFromIntentRequest。

    容错处理三种情况：
        1. 标准格式: {"intent_id": "...", "domain": "..."}
        2. 包裹格式: {"request": {"intent_id": "...", "domain": "..."}}
        3. 意图结果整体传入: {"intent": {"intent_id": "...", "domain": "..."}, ...}
           → 从 intent 子对象中提取 intent_id 和 domain
    """
    if isinstance(data, BaseModel):
        return data
    if isinstance(data, dict):
        # 情况 2: {"request": {...}}
        if "request" in data and isinstance(data["request"], dict):
            data = data["request"]
        # 情况 3: LLM 把整个 intent 结果传进来了 → 从 intent 子对象提取
        if "intent_id" not in data and "intent" in data and isinstance(data["intent"], dict):
            intent = data["intent"]
            return GetPlanFromIntentRequest(
                intent_id=str(intent.get("intent_id", "")),
                domain=str(intent.get("domain", "")),
            )
        return GetPlanFromIntentRequest(**data)
    raise ValueError(f"Unsupported argument type: {type(data)}")


@app.tool(
    name="get_plan_from_intent",
    description="""根据意图 ID 和域名称从知识图谱中查询对应的问题求解执行计划。

    适用场景：意图识别完成后，拿到 intent_id 和 domain，再调用本工具获取结构化的执行步骤。

    输入来源（来自 intent-recognition MCP 的 recognize_intent 工具）：
        - intent_id: 意图识别结果 intent.intent_id 字段（必需）
        - domain:    意图识别结果 intent.domain 字段（必需）

    返回：
        - pass:            是否成功找到计划
        - plan.description: 计划描述文本
        - plan.steps:       执行步骤列表（JSON 数组）""",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def get_plan_from_intent(request: Union[GetPlanFromIntentRequest, Dict[str, Any]]) -> Dict[str, Any]:
    try:
        req = _normalize_plan_args(request)
        if not req.intent_id or not req.domain:
            return {
                "understanding": "缺少必要参数: intent_id 或 domain 为空。"
                                 "请确保调用本工具前已获取意图识别结果，"
                                 "并从 intent 对象中提取 intent_id 和 domain 字段。",
                "pass": False,
                "execution_time_ms": 0.0,
            }
        executor = PlanExecutor()
        return executor.get_plan_from_intent(
            intent_id=req.intent_id,
            domain=req.domain,
        )
    except Exception as e:
        return {
            "understanding": f"获取计划异常: {str(e)}",
            "pass": False,
            "error_message": str(e),
            "execution_time_ms": 0.0,
        }


if __name__ == "__main__":
    import sys
    import asyncio

    if len(sys.argv) > 1 and sys.argv[1] == "stdio":
        print("🟢 启动 Problem Solving Plan MCP stdio 服务...")
        asyncio.run(app.run_stdio_async())
    else:
        print("🟢 启动 Problem Solving Plan MCP HTTP 服务...")
        print("📡 服务地址: http://0.0.0.0:8892")
        print("🔧 使用方式:")
        print("   - OpenAPI 文档: http://localhost:8892/docs")
        print("   - 启动 stdio 模式: python mcp_server.py stdio")
        asyncio.run(app.run_http_async(host="0.0.0.0", port=8892))
