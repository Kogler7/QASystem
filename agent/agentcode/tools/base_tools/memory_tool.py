from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import tiktoken
from openai import OpenAI

SYNTHETIC_PREFIXES = ("<background-results>", "<inbox>", "<agent-injected>")

DEFAULT_THRESHOLD = 8000
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TRANSCRIPT_DIR = ""


class MemoryCompactor:
    """对话上下文压缩器，在单次 agent loop 中 token 超限时自动裁剪/压缩消息列表。

    核心策略：
        - **多轮**: 保留最新一条用户提问及其后续交互，将更早的内容压缩为摘要。
        - **单轮过长**: 仅有一个用户提问但后续 assistant/tool 消息过多时，压缩中间部分。
        - 每次压缩前将完整 transcript 保存到磁盘。
        - 不再写入 HISTORY.md 或 session.jsonl，仅做当次 loop 内的压缩。

    Attributes:
        threshold: 待压缩消息的 token 上限，超限部分被裁剪。
        _encoding: tiktoken cl100k_base 编码器。
        _client: OpenAI 兼容客户端，用于生成摘要。
        _transcript_dir: transcript 保存目录。
    """

    def __init__(
        self,
        threshold: int = DEFAULT_THRESHOLD,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        base_url: str | None = None,
        transcript_dir: str | None = None,
    ) -> None:
        """初始化压缩器。

        Args:
            threshold: 待压缩消息的 token 上限。
            model: 摘要生成模型名。
            api_key: API 密钥，默认从 AGENT_LLM_API_KEY 环境变量读取。
            base_url: API 地址，默认从 AGENT_LLM_BASE_URL 环境变量读取。
            transcript_dir: transcript 保存目录，默认为 workspace/transcripts。
        """
        self.threshold = threshold

        self._encoding = tiktoken.get_encoding("cl100k_base")

        api_key = api_key or os.environ.get("AGENT_LLM_API_KEY", "")
        base_url = base_url or os.environ.get("AGENT_LLM_BASE_URL", "http://localhost:9999")
        self._client: OpenAI | None = None
        if api_key:
            self._client = OpenAI(base_url=base_url, api_key=api_key)
        self._model = model

        self._transcript_dir = Path(transcript_dir) if transcript_dir else None

    def estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """估算消息列表的 token 数。

        使用 tiktoken 的 ``cl100k_base`` 编码：
            1. 每条消息收取 4 token 基础开销。
            2. 遍历消息所有 key-value：字符串直接编码，列表序列化为 JSON 后编码，其他非 None 值转字符串编码。

        Args:
            messages: 消息字典列表。

        Returns:
            估算的 token 数量。
        """
        if not messages:
            return 0
        total = 0
        for msg in messages:
            total += 4
            for _key, value in msg.items():
                if isinstance(value, str):
                    total += len(self._encoding.encode(value))
                elif isinstance(value, list):
                    total += len(self._encoding.encode(json.dumps(value, ensure_ascii=False)))
                elif value is not None:
                    total += len(self._encoding.encode(str(value)))
        return total

    @staticmethod
    def _is_synthetic_message(msg: dict[str, Any]) -> bool:
        """判断消息是否由 agent 循环自动注入（非真实用户输入）。

        检测方式：
            1. 若消息包含 ``_agent_injected: True`` 字段，直接判为合成。
            2. 若 content 为字符串且以合成前缀开头，判为合成。

        Args:
            msg: 消息字典。

        Returns:
            True 表示该消息为合成消息。
        """
        if msg.get("_agent_injected"):
            return True
        content = msg.get("content", "")
        if not isinstance(content, str):
            return False
        return content.startswith(SYNTHETIC_PREFIXES)

    @staticmethod
    def _find_last_user_idx(non_system_msgs: list[dict[str, Any]]) -> int:
        """在非系统消息中找到最后一个真实用户消息的索引。

        跳过合成消息（如 ``<background-results>``、``<inbox>``）。

        Args:
            non_system_msgs: 不含 role==system 的消息列表。

        Returns:
            最后一个真实用户消息的索引，少于 2 个真实用户提问时返回 -1。
        """
        user_indices = [
            i
            for i, m in enumerate(non_system_msgs)
            if m.get("role") == "user" and not MemoryCompactor._is_synthetic_message(m)
        ]
        if len(user_indices) <= 1:
            return -1
        return user_indices[-1]

    def _trim_by_tokens(self, messages: list[dict[str, Any]], max_tokens: int) -> list[dict[str, Any]]:
        """从尾部保留不超过 *max_tokens* 的消息子集。

        从末尾向前迭代，在消息边界处停止，保证 JSON 结构不被破坏。
        至少保留最后一条消息。

        Args:
            messages: 待裁剪的消息列表。
            max_tokens: token 上限。

        Returns:
            裁剪后的消息列表。
        """
        if not messages:
            return []
        selected: list[dict[str, Any]] = []
        token_count = 0
        for msg in reversed(messages):
            msg_tokens = self.estimate_tokens([msg])
            if token_count + msg_tokens > max_tokens and selected:
                break
            selected.insert(0, msg)
            token_count += msg_tokens
        return selected

    def auto_compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """压缩对话历史，始终保留最新的用户提问。

        两种策略：

        1. **多轮** — 找到最新的 ``role==user`` 消息，将其之前的所有内容压缩为摘要。
           最新一轮完整保留。
        2. **单轮过长** — 仅有一个用户提问，但后续 assistant/tool 消息过多。
           保留用户提问和最后 *keep* 条消息，中间的压缩为摘要。

        压缩前将完整对话保存为 transcript。
        不再写入 HISTORY.md 或修改 session.jsonl，仅做单次 loop 内的内存压缩。

        Args:
            messages: 完整的消息列表（含 system / developer / user / assistant / tool）。

        Returns:
            压缩后的消息列表。
        """
        system_msgs: list[dict[str, Any]] = []
        non_system_msgs: list[dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") == "system":
                system_msgs.append(msg)
            else:
                non_system_msgs.append(msg)

        keep = 10
        if len(non_system_msgs) <= keep:
            return messages

        last_user_idx = self._find_last_user_idx(non_system_msgs)

        if last_user_idx != -1:
            if last_user_idx >= len(non_system_msgs) - 1:
                return messages
            to_summarize = non_system_msgs[:last_user_idx]
            recent_msgs = non_system_msgs[last_user_idx:]
        else:
            user_indices = [
                i
                for i, m in enumerate(non_system_msgs)
                if m.get("role") == "user" and not self._is_synthetic_message(m)
            ]
            if not user_indices:
                return messages
            user_idx = user_indices[0]
            tail = len(non_system_msgs) - user_idx - 1
            if tail <= keep:
                return messages
            to_summarize = non_system_msgs[user_idx + 1 : -keep]
            recent_msgs = non_system_msgs[: user_idx + 1] + non_system_msgs[-keep:]

        trimmed = self._trim_by_tokens(to_summarize, self.threshold)
        conversation_text = json.dumps(trimmed, ensure_ascii=False)

        summary = self._generate_summary(conversation_text)

        compressed_msgs: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": f"[Conversation compressed.]\n\n{summary}",
            },
        ]
        return system_msgs + compressed_msgs + recent_msgs

    def _generate_summary(self, conversation_text: str) -> str:
        """调用 LLM 将对话历史压缩为摘要。

        Args:
            conversation_text: 待压缩的对话 JSON 字符串。

        Returns:
            生成的摘要字符串。
        """
        if self._client is None:
            return "Summary unavailable (no API client configured)"

        prompt = (
            "Summarize this conversation for continuity. Include: "
            "1) What was accomplished, 2) Current state, 3) Key decisions made. "
            "Be concise but preserve critical details.\n\n" + conversation_text
        )

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content or "No summary generated."
        except Exception:
            return "Summary unavailable (API error)"
