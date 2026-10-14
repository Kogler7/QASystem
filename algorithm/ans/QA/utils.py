from docker.configuration import getenv as config_getenv
import os
import time

import httpx
from openai import OpenAI


def run_llm(
    prompt: str,
    temperature: float,
    max_tokens: int,
    api_key: str,
    engine: str = "Qwen/Qwen3-8B",
) -> str:
    """调用图谱检索专用 LLM；失败时重试并返回明确错误标记。"""
    from external_services.llm.service import resolve_chat_service, model_call
    service, key = resolve_chat_service('graph', required=True)
    base_url, model = service['base_url'], service['model']
    connection = service.get('connection') or {}
    verify_ssl = connection.get('ca_bundle') or connection.get('verify_ssl', True)
    retries = 3  # Graph algorithm policy, not per-deployment configuration.
    timeout = float(connection.get('timeout_seconds', 60))
    messages = [
        {
            "role": "system",
            "content": (
                "你是知识图谱检索辅助模型。只能依据输入中的问题与图谱候选信息判断，"
                "不得引入外部业务知识。直接输出最终结果（用户要求的格式，通常是 JSON），"
                "不要输出任何思考过程、解释、Markdown 或理由性文字。若你具备推理/思考能力，"
                "也请直接给出最终 JSON，不要先输出思考内容。"
            ),
        },
        {"role": "user", "content": prompt},
    ]

    last_error = "unknown error"
    for attempt in range(retries):
        try:
            with httpx.Client(verify=verify_ssl, timeout=timeout) as http_client:
                client = OpenAI(base_url=base_url, api_key=key, http_client=http_client, max_retries=0)
                payload = dict(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                with model_call(service, payload, timeout=timeout) as remaining:
                    response = client.chat.completions.create(**payload, timeout=remaining)
            message = response.choices[0].message
            content = getattr(message, "content", None)
            if content and content.strip():
                return content.strip()
            # 推理模型（如 Qwen 系列）在 content 为空时，可能把输出放在 reasoning_content。
            # 兜底读取并交由调用方做 <think> 剥离 + JSON 提取；仍为空才视为失败。
            reasoning = getattr(message, "reasoning_content", None)
            if reasoning and reasoning.strip():
                return reasoning.strip()
            last_error = "empty response"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < retries:
            time.sleep(min(2**attempt, 4))

    return f"LLM_ERROR: {last_error}"
