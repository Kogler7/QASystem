"""
统一大模型调用接口：供 NL_to_cypher、ans、rag、uie 等组件复用。
支持多种后端：ollama（completions）、openai（OpenAI 兼容 chat）、zhipu（智谱）。
配置优先从环境变量读取，也可在代码中传入覆盖。
"""
import os
import re
from typing import Optional, List, Dict, Any

import requests


# ---------- 环境变量（统一命名，兼容旧变量）----------
def _env(key: str, *fallback_keys: str, default: str = "") -> str:
    v = os.environ.get(key)
    for k in fallback_keys:
        if v:
            break
        v = os.environ.get(k)
    return (v or default).strip()


# 后端类型: ollama | openai | zhipu（本地 vLLM 使用 openai 兼容接口）
LLM_BACKEND = _env("LLM_BACKEND", "OLLAMA_BACKEND", default="openai")
# 请求地址：默认本地 vLLM；可覆盖为 OLLAMA_URL / OPENAI_BASE_URL 等
LLM_BASE_URL = _env("LLM_BASE_URL", "OLLAMA_URL", "OPENAI_BASE_URL", "VLLM_BASE_URL", default="http://localhost:9999")
# 模型名（vLLM 服务端加载的模型名；空时发 default 供本地 vLLM 使用）
LLM_MODEL = _env("LLM_MODEL", "OLLAMA_MODEL_NAME", "OPENAI_MODEL", "VLLM_MODEL", default="Qwen/Qwen3-8B")
# API Key（本地 vLLM 通常不需要；不写死，仅从环境读取）
LLM_API_KEY = _env("LLM_API_KEY", "OLLAMA_API_KEY", "OPENAI_API_KEY", default="")


def extract_final_response(response_text: str) -> str:
    """若包含 <think>...</think> 则只保留其后内容。"""
    if not response_text:
        return response_text
    if "<think>" in response_text and "</think>" in response_text:
        parts = response_text.split("</think>", 1)
        if len(parts) > 1:
            out = parts[1].strip()
            out = re.sub(r"^\n+", "", out)
            return out
    return response_text


def _norm_url(base: str, for_chat: bool = True) -> str:
    base = base.rstrip("/")
    if for_chat:
        if "/v1/chat/completions" in base:
            return base
        # base 已含 /v1 时只拼 /chat/completions，避免 .../v1/v1/chat/completions
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"
    else:
        if "/completions" in base:
            return base
        if base.endswith("/v1"):
            return f"{base}/completions"
        return f"{base}/v1/completions"


def _headers(api_key: Optional[str] = None) -> dict:
    key = (api_key or LLM_API_KEY)
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _model_name(model: Optional[str], fallback: str = "default") -> str:
    return (model or LLM_MODEL or fallback).strip() or fallback


def _call_ollama(
    prompt: str,
    *,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: int = 120,
) -> str:
    url = _norm_url(base_url or LLM_BASE_URL, for_chat=False)
    payload = {
        "model": _model_name(model),
        "prompt": prompt,
        "stream": False,
    }
    r = requests.post(url, json=payload, headers=_headers(api_key), timeout=timeout)
    r.raise_for_status()
    data = r.json()
    text = (data.get("response") or data.get("choices", [{}])[0].get("text") or "").strip()
    return extract_final_response(text)


def _call_openai(
    prompt: str,
    *,
    system_prompt: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    timeout: int = 120,
) -> str:
    url = _norm_url(base_url or LLM_BASE_URL, for_chat=True)
    messages: List[Dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": _model_name(model),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    r = requests.post(url, json=payload, headers=_headers(api_key), timeout=timeout)
    try:
        r.raise_for_status()
        data = r.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        return extract_final_response(text)
    except Exception as e:
        # 尽量把响应体打印出来，便于定位 400/401/403 的真实原因
        resp_text = ""
        try:
            resp_text = (r.text or "").strip()[:500]
        except Exception:
            pass
        import logging

        logging.error(
            "LLM openai request failed: url=%s status=%s err=%s body=%s",
            url,
            getattr(r, "status_code", None),
            e,
            resp_text,
        )
        return ""


def _call_zhipu(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    timeout: int = 120,
) -> str:
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    key = api_key or LLM_API_KEY
    if not key:
        raise ValueError("智谱需配置 LLM_API_KEY 或 OPENAI_API_KEY")
    payload = {
        "model": _model_name(model, "glm-4-flash"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    r = requests.post(url, json=payload, headers=_headers(key), timeout=timeout)
    r.raise_for_status()
    data = r.json()
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    return text


def complete(
    prompt: str,
    *,
    system_prompt: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    timeout: int = 120,
    backend: Optional[str] = None,
    strip_think: bool = True,
) -> str:
    """
    统一入口：根据配置的后端调用大模型，返回单轮回复文本。

    :param prompt: 用户输入/问题
    :param system_prompt: 系统提示（仅 openai/zhipu 生效）
    :param model: 模型名，不传则用环境变量
    :param base_url: 请求地址，不传则用环境变量
    :param api_key: API Key，不传则用环境变量 LLM_API_KEY / OPENAI_API_KEY
    :param temperature: 温度
    :param max_tokens: 最大生成长度
    :param timeout: 超时秒数
    :param backend: 指定本次调用的后端 ollama|openai|zhipu，不传则用环境变量 LLM_BACKEND
    :param strip_think: 是否去掉 <think>...</think> 内容
    :return: 模型回复文本，失败返回空字符串并打印错误
    """
    backend = (backend or LLM_BACKEND).lower()
    try:
        if backend == "ollama":
            text = _call_ollama(
                prompt, model=model, base_url=base_url, api_key=api_key, timeout=timeout
            )
        elif backend == "zhipu":
            text = _call_zhipu(
                prompt,
                model=model,
                api_key=api_key,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        else:
            # openai 或未识别的都走 OpenAI 兼容
            text = _call_openai(
                prompt,
                system_prompt=system_prompt,
                model=model,
                base_url=base_url,
                api_key=api_key,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        if not strip_think:
            return text
        return extract_final_response(text) if text else ""
    except Exception as e:
        import logging
        logging.error(f"LLM 调用错误 [{backend}]: {e}")
        return ""


def chat(
    messages: List[Dict[str, str]],
    *,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    timeout: int = 120,
    backend: Optional[str] = None,
) -> str:
    """
    多轮对话接口（OpenAI messages 格式）。仅 openai / zhipu 支持；ollama 会将 messages 合并为单条 prompt 调用。
    """
    backend = (backend or LLM_BACKEND).lower()
    if backend == "ollama":
        parts = []
        for m in messages:
            role = (m.get("role") or "user").lower()
            content = (m.get("content") or "").strip()
            if content:
                parts.append(f"{role}: {content}")
        return complete(
            "\n".join(parts),
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            backend="ollama",
        )
    url = _norm_url(base_url or LLM_BASE_URL, for_chat=True)
    if backend == "zhipu":
        url = _norm_url(base_url or LLM_BASE_URL, for_chat=True)
    payload = {
        "model": _model_name(model),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    try:
        r = requests.post(url, json=payload, headers=_headers(api_key), timeout=timeout)
        r.raise_for_status()
        data = r.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        return extract_final_response(text)
    except Exception as e:
        print(f"LLM chat 错误 [{backend}]: {e}")
        return ""


class LLMClient:
    """
    可实例化的客户端：在构造时固定 base_url、api_key、model、backend，
    后续只传 prompt 或 messages，便于多组件复用同一配置。
    """

    def __init__(
        self,
        *,
        backend: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.backend = (backend or LLM_BACKEND).lower()
        self.base_url = base_url or LLM_BASE_URL
        self.api_key = api_key or LLM_API_KEY
        self.model = _model_name(model)

    def complete(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        timeout: int = 120,
    ) -> str:
        return complete(
            prompt,
            system_prompt=system_prompt,
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            backend=self.backend,
        )

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 2048,
        timeout: int = 120,
    ) -> str:
        return chat(
            messages,
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            backend=self.backend,
        )

    def run(self, system_prompt: str, text: str, **kwargs: Any) -> str:
        """
        兼容 UIE 等组件的 run(system_prompt, text) 接口。
        等价于 complete(text, system_prompt=system_prompt, ...)。
        """
        return self.complete(text, system_prompt=system_prompt, **kwargs)


class LLMRunAdapter:
    """
    统一接口：run(system_prompt, text) -> str。
    供 algorithm/uie、知识抽取等需要「系统提示 + 用户文本」的组件复用，
    内部委托给 LLMClient.complete。
    """

    def __init__(self, client: Optional[LLMClient] = None, **client_kw: Any):
        self._client = client or LLMClient(**client_kw)

    def run(self, system_prompt: str, text: str, **kwargs: Any) -> str:
        return self._client.run(system_prompt, text, **kwargs)


def create_run_adapter(
    *,
    backend: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> LLMRunAdapter:
    """
    创建满足 run(system_prompt, text) -> str 的适配器，供 UIE/LLMExtractor 等使用。
    配置与 LLMClient 一致，可从环境变量读取。
    """
    client = LLMClient(
        backend=backend,
        base_url=base_url,
        api_key=api_key,
        model=model,
    )
    return LLMRunAdapter(client)
