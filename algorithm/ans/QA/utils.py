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
    model = engine or config_getenv("GRAPH_LLM_MODEL", "Qwen/Qwen3-8B")
    default_host = "host.docker.internal" if os.path.exists("/.dockerenv") else "127.0.0.1"
    base_url = config_getenv("GRAPH_LLM_BASE_URL", f"http://{default_host}:9999").rstrip("/")
    key = api_key or config_getenv("GRAPH_LLM_API_KEY", "")
    verify_ssl = config_getenv("GRAPH_LLM_VERIFY_SSL", "true").lower() not in {
        "false",
        "0",
        "no",
        "off",
    }
    retries = max(1, int(config_getenv("GRAPH_LLM_RETRIES", "3")))
    timeout = float(config_getenv("GRAPH_LLM_TIMEOUT_SECONDS", "60"))
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
                client = OpenAI(base_url=base_url, api_key=key, http_client=http_client)
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
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
