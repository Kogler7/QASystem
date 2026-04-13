import json
import logging
from typing import Annotated, Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..deps.auth import get_current_user
from ..models.user import User
from ..services.dify_client import dify_client

router = APIRouter(prefix="/api", tags=["chat"])
logger = logging.getLogger(__name__)


def _to_str(val: Any) -> str:
    """将任意值转为非空字符串或空串。"""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        return (val.get("text") or val.get("content") or "").strip() or str(val).strip()
    return str(val).strip()


def _get_workflow_output_text(outputs: dict, preferred_key: str) -> str:
    """从工作流 outputs 中提取文本，兼容多种变量名和嵌套结构（含按节点 id 包装）。"""
    if not outputs:
        return ""

    # 1) 先按常见键名取
    candidates = [preferred_key, "text", "result", "output", "answer", "response"]
    for key in candidates:
        val = outputs.get(key)
        if val is None:
            continue
        s = _to_str(val)
        if s:
            return s

    # 2) 再遍历所有值
    for val in outputs.values():
        s = _to_str(val)
        if s:
            return s

    # 3) 嵌套：若值是 dict（如按节点 id 包装），递归查找
    for val in outputs.values():
        if isinstance(val, dict):
            nested = _get_workflow_output_text(val, preferred_key)
            if nested:
                return nested

    return ""


class HistoryItem(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    """前端可传 message 或 content，都会作为发往 Dify 的输入（工作流中对应 query）。"""
    message: Optional[str] = None
    content: Optional[str] = None
    history: Optional[List[HistoryItem]] = None
    userId: Optional[str] = None
    context: Optional[dict[str, Any]] = None


class ChatResponse(BaseModel):
    success: bool
    reply: str
    conversation_id: Optional[str] = None
    error: Optional[str] = None


@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    body: ChatRequest,
    current_user: Annotated[User, Depends(get_current_user)],
) -> ChatResponse:
    query_text = (body.message or body.content or "").strip()
    if not query_text:
        raise HTTPException(status_code=400, detail="message or content is required")

    user_id = body.userId or current_user.id
    use_workflow = settings.DIFY_USE_WORKFLOW and dify_client.workflow_api_key
    use_chat = dify_client.chat_api_key
    
    if not use_workflow and not use_chat:
        error_msg = "Dify API配置错误：未配置DIFY_API_KEY或DIFY_WORKFLOW_API_KEY"
        logger.error(error_msg)
        return ChatResponse(
            success=False,
            reply=error_msg,
            conversation_id=None,
            error=error_msg
        )
    
    try:
        if use_workflow:
            inputs = {}
            if dify_client.workflow_input_var and dify_client.workflow_input_var.strip():
                inputs[dify_client.workflow_input_var.strip()] = query_text
            dify_resp = await dify_client.workflow_run(
                inputs=inputs,
                user_id=user_id,
                response_mode="blocking",
            )
            data = dify_resp.get("data") or {}
            if data.get("status") == "failed":
                err_msg = data.get("error") or "工作流执行失败"
                return ChatResponse(
                    success=False, reply=err_msg, conversation_id=None, error=err_msg
                )
            raw_outputs = data.get("outputs")
            if isinstance(raw_outputs, str):
                try:
                    raw_outputs = json.loads(raw_outputs)
                except Exception:
                    raw_outputs = {}
            outputs = raw_outputs if isinstance(raw_outputs, dict) else {}
            answer = _get_workflow_output_text(
                outputs,
                dify_client.workflow_output_var,
            )
            # 若 outputs 为空或未解析到文本，尝试从 data 根层级取（兼容部分版本）
            if not answer and data:
                answer = _get_workflow_output_text(
                    data,
                    dify_client.workflow_output_var,
                )
            if not answer and (outputs or data):
                logger.warning(
                    "工作流返回的 outputs 中未解析到文本，请核对 Dify 结束节点输出变量名。outputs 结构: %s",
                    outputs,
                )
            conversation_id = dify_resp.get("workflow_run_id") or dify_resp.get("task_id")
        else:
            dify_resp = await dify_client.chat(
                message=query_text,
                user_id=user_id,
                conversation_id=(body.context or {}).get("conversation_id") if body.context else None,
                history=[h.model_dump() for h in body.history] if body.history else None,
            )
            answer = (
                dify_resp.get("answer")
                or dify_resp.get("outputs", {}).get("text")
                or dify_resp.get("message")
                or ""
            )
            conversation_id = dify_resp.get("conversation_id") or dify_resp.get("id")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"处理聊天请求异常: {e}")
        return ChatResponse(success=False, reply=str(e), conversation_id=None, error=str(e))

    return ChatResponse(
        success=True,
        reply=answer or "暂无回复内容。",
        conversation_id=conversation_id,
        error=None,
    )

