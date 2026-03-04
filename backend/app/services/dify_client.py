from typing import Any, Dict, Optional

import httpx

from ..config import settings


class DifyClient:
    def __init__(self) -> None:
        self.base_url = settings.DIFY_API_URL.rstrip("/")
        self.chat_api_key = settings.DIFY_API_KEY
        self.knowledge_api_key = settings.DIFY_KNOWLEDGE_API_KEY
        self.workflow_api_key = settings.DIFY_WORKFLOW_API_KEY
        self.workflow_input_var = settings.DIFY_WORKFLOW_INPUT_VAR or "query"
        self.workflow_output_var = settings.DIFY_WORKFLOW_OUTPUT_VAR or "text"

    async def chat(
        self,
        message: str,
        user_id: str = "default",
        conversation_id: Optional[str] = None,
        history: Optional[list[dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/chat-messages"
        headers = {
            "Authorization": f"Bearer {self.chat_api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "query": message,
            "user": user_id,
            "inputs": {},
            "response_mode": "blocking",
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if history:
            payload["conversation"] = history

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            return resp.json()

    async def workflow_run(
        self,
        inputs: Dict[str, Any],
        user_id: str = "default",
        response_mode: str = "blocking",
    ) -> Dict[str, Any]:
        """执行 Dify 工作流。inputs 键名需与工作流中「开始」节点的变量名一致。"""
        url = f"{self.base_url}/workflows/run"
        headers = {
            "Authorization": f"Bearer {self.workflow_api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "inputs": inputs,
            "response_mode": response_mode,
            "user": user_id,
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 401:
                raise ValueError(
                    "Dify 工作流 401 Unauthorized：请检查 DIFY_WORKFLOW_API_KEY 是否来自「工作流」应用的 API 访问页，"
                    "且密钥未过期、未重新生成。在 Dify 控制台打开该工作流应用 → API 访问 → 复制 API Key。"
                )
            resp.raise_for_status()
            return resp.json()

    async def query_knowledge(self, question: str, user_id: str = "default") -> Dict[str, Any]:
        url = f"{self.base_url}/retrieve"
        headers = {
            "Authorization": f"Bearer {self.knowledge_api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "query": question,
            "user": user_id,
            "retrieval_setting": {
                "top_k": 3,
                "score_threshold": 0.5,
            },
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            return resp.json()


dify_client = DifyClient()

