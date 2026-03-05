from typing import Any, Dict, Optional

import httpx

from ..config import settings
import logging

logger = logging.getLogger(__name__)


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
        if not self.chat_api_key or not self.chat_api_key.strip():
            raise ValueError("DIFY_API_KEY 未配置或为空，请检查 .env 文件")
        
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

        try:
            timeout_config = httpx.Timeout(30.0, connect=10.0, read=30.0, write=10.0, pool=10.0)
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException as e:
            logger.error(f"Dify聊天API请求超时: {e}")
            raise ValueError("Dify API响应超时，请稍后重试") from e
        except httpx.HTTPStatusError as e:
            logger.error(f"Dify聊天API HTTP错误: {e.response.status_code} - {e.response.text[:200]}")
            raise ValueError(f"Dify API错误: {e.response.status_code}") from e
        except httpx.RequestError as e:
            logger.error(f"Dify聊天API请求错误: {e}")
            raise ValueError(f"无法连接到Dify API: {str(e)}") from e
        except Exception as e:
            logger.exception(f"Dify聊天API异常: {e}")
            raise ValueError(f"Dify API调用失败: {str(e)}") from e

    async def workflow_run(
        self,
        inputs: Dict[str, Any],
        user_id: str = "default",
        response_mode: str = "blocking",
    ) -> Dict[str, Any]:
        """执行 Dify 工作流。inputs 键名需与工作流中「开始」节点的变量名一致。"""
        if not self.workflow_api_key or not self.workflow_api_key.strip():
            raise ValueError("DIFY_WORKFLOW_API_KEY 未配置或为空，请检查 .env 文件")
        
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
        try:
            timeout_config = httpx.Timeout(35.0, connect=10.0, read=35.0, write=10.0, pool=10.0)
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 401:
                    logger.error("Dify工作流401错误: API Key无效")
                    raise ValueError(
                        "Dify 工作流 401 Unauthorized：请检查 DIFY_WORKFLOW_API_KEY 是否来自「工作流」应用的 API 访问页，"
                        "且密钥未过期、未重新生成。在 Dify 控制台打开该工作流应用 → API 访问 → 复制 API Key。"
                    )
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException as e:
            logger.error(f"Dify工作流API请求超时: {e}")
            raise ValueError("Dify工作流执行超时，请稍后重试") from e
        except httpx.HTTPStatusError as e:
            logger.error(f"Dify工作流API HTTP错误: {e.response.status_code} - {e.response.text[:200]}")
            raise ValueError(f"Dify工作流API错误: {e.response.status_code}") from e
        except httpx.RequestError as e:
            logger.error(f"Dify工作流API请求错误: {e}")
            raise ValueError(f"无法连接到Dify工作流API: {str(e)}") from e
        except Exception as e:
            logger.exception(f"Dify工作流API异常: {e}")
            raise ValueError(f"Dify工作流API调用失败: {str(e)}") from e

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

        try:
            # 设置为15秒，确保在前端20秒超时前返回
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException as e:
            logger.error(f"Dify知识库API请求超时: {e}")
            raise ValueError("Dify知识库查询超时，请稍后重试") from e
        except httpx.HTTPStatusError as e:
            logger.error(f"Dify知识库API HTTP错误: {e.response.status_code} - {e.response.text[:200]}")
            raise ValueError(f"Dify知识库API错误: {e.response.status_code}") from e
        except httpx.RequestError as e:
            logger.error(f"Dify知识库API请求错误: {e}")
            raise ValueError(f"无法连接到Dify知识库API: {str(e)}") from e


dify_client = DifyClient()

