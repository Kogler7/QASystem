"""工作线程核心函数 — 供 api.py 的 lifespan 内嵌使用。

每个线程独立从 Redis 队列拉取任务 → 借 ToolManager → 执行 Agent → 归还。
多进程由 uvicorn workers 参数控制，此模块不再处理进程管理。

环境变量:
    AGENT_WORKER_CONCURRENCY   每进程工作线程数，默认 4
    AGENT_WORKER_POOL_SIZE     ToolManager 连接池大小，默认 = concurrency
    TASK_STORE_REDIS_URL       Redis 连接 URL
"""

from __future__ import annotations

import logging
import threading

from . import task_store
from .adapter import (
    _load_env,
    _chat_completions_url,
    _messages_to_openai,
    _mcp_tools_to_openai,
    _default_mcp_path,
)
from .delivery import answer_v1_delivery
from .models import BudgetConfig, SessionConfig, SkillRef, ToolPolicy
from .pool import ToolManagerPool
from .runtime import AgentRuntime
from .skillsets.loader import SkillLoader
from .tools.mcp_tools.mcp_loader import build_config_from_mcp_json
from .trace import TaskEventTraceSink
from .cancellation import AgentCancelled, CancellationToken

logger = logging.getLogger("agentcode.worker")


# ═══════════════════════════════════════════════════════════════════════
# LLM 后端（线程级 HTTP 连接复用）
# ═══════════════════════════════════════════════════════════════════════

class _BlockingLLM:
    """同步 LLMBackend，每个线程持有一个实例以复用 HTTP 连接。

    与旧版区别：httpx.Client 在实例生命周期内复用（而非每次 complete() 新建），
    利用 HTTP keep-alive 避免重复 TCP 握手开销。
    """

    def __init__(self):
        import httpx
        self._cfg = _load_env()
        self._url = _chat_completions_url(self._cfg["base_url"])
        self._client = httpx.Client(
            timeout=self._cfg["timeout_s"],
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=20),
            transport=httpx.HTTPTransport(retries=1),
        )

    def complete(self, messages, tools, cancel_token=None):
        import json as json_mod

        oa_msgs = _messages_to_openai(messages)
        oa_tools = _mcp_tools_to_openai(tools) if tools else []

        payload = {
            "model": self._cfg["model"],
            "messages": oa_msgs,
            "temperature": self._cfg["temperature"],
            "max_tokens": self._cfg["max_tokens"],
            "stream": False,
        }
        if oa_tools:
            payload["tools"] = oa_tools

        headers = {"Content-Type": "application/json"}
        if self._cfg["api_key"]:
            headers["Authorization"] = f"Bearer {self._cfg['api_key']}"

        if cancel_token is not None and cancel_token.is_cancelled():
            raise AgentCancelled("Task cancelled")
        timeout = self._cfg["timeout_s"]
        if cancel_token is not None and cancel_token.remaining_seconds() is not None:
            timeout = max(0.1, min(timeout, cancel_token.remaining_seconds()))
        resp = self._client.post(self._url, json=payload, headers=headers, timeout=timeout)
        if cancel_token is not None and cancel_token.is_cancelled():
            raise AgentCancelled("Task cancelled")
        resp.raise_for_status()
        body = resp.json()

        choice = (body.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content_text = msg.get("content") or ""
        reasoning = msg.get("reasoning_content", "")

        blocks = []
        if content_text.strip():
            blocks.append({"type": "text", "text": content_text.strip()})

        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            try:
                args = json_mod.loads(fn.get("arguments", "{}"))
            except json_mod.JSONDecodeError:
                args = {}
            blocks.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "input": args,
            })

        result = {"role": "assistant", "content": blocks}
        if reasoning:
            result["reasoning_content"] = reasoning
        return result

    def close(self):
        self._client.close()


# ═══════════════════════════════════════════════════════════════════════
# 流式 LLM 后端（逐 token 推送至 StreamBroker）
# ═══════════════════════════════════════════════════════════════════════

class _StreamingWorkerLLM:
    """流式 LLMBackend：逐 token 推送到 StreamBroker，同时返回完整 assistant 消息。

    逻辑与 cli.py 的 _StreamingLLM 相同，但 stdout 打印替换为 StreamBroker.publish()，
    使得 SSE 客户端能实时收到 LLM 生成的每个 token。
    """

    def __init__(self, broker=None):
        import httpx
        self._cfg = _load_env()
        self._url = _chat_completions_url(self._cfg["base_url"])
        self._broker = broker
        self._task_id: str | None = None
        self._client = httpx.Client(
            timeout=self._cfg["timeout_s"],
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=20),
        )

    def set_task_id(self, task_id: str) -> None:
        self._task_id = task_id

    def complete(self, messages, tools, cancel_token=None):
        import json as json_mod

        oa_msgs = _messages_to_openai(messages)
        oa_tools = _mcp_tools_to_openai(tools) if tools else []

        payload = {
            "model": self._cfg["model"],
            "messages": oa_msgs,
            "temperature": self._cfg["temperature"],
            "max_tokens": self._cfg["max_tokens"],
            "stream": True,
        }
        if oa_tools:
            payload["tools"] = oa_tools

        headers = {"Content-Type": "application/json"}
        if self._cfg["api_key"]:
            headers["Authorization"] = f"Bearer {self._cfg['api_key']}"

        accumulated_text = ""
        accumulated_reasoning = ""
        accumulated_tool_calls: dict[int, dict] = {}
        finish_reason = ""

        try:
            timeout = self._cfg["timeout_s"]
            if cancel_token is not None and cancel_token.remaining_seconds() is not None:
                timeout = max(0.1, min(timeout, cancel_token.remaining_seconds()))
            with self._client.stream(
                "POST", self._url, json=payload, headers=headers, timeout=timeout
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if cancel_token is not None and cancel_token.is_cancelled():
                        raise AgentCancelled("Task cancelled")
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json_mod.loads(data_str)
                    except json_mod.JSONDecodeError:
                        continue
                    # 网关可能下发非对象 chunk（如 `data: null`），跳过而不是让它炸在 .get 上。
                    if not isinstance(chunk, dict):
                        continue

                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    finish_reason = choice.get("finish_reason") or finish_reason

                    if "reasoning_content" in delta and delta["reasoning_content"]:
                        accumulated_reasoning += delta["reasoning_content"]
                        self._publish("reasoning", {"text": delta["reasoning_content"]})

                    if "content" in delta and delta["content"]:
                        text = delta["content"]
                        accumulated_text += text
                        self._publish("token", {"text": text})

                    # 兼容"显式下发 null"的网关（MSE 网关无工具调用时会给出
                    # "tool_calls": null，键存在、值为 null）；只判断键是否存在会让
                    # for 遍历 None 抛 'NoneType' object is not iterable，整轮会话失败。
                    for tc in (delta.get("tool_calls") or []):
                        idx = tc.get("index", 0)
                        if idx not in accumulated_tool_calls:
                            accumulated_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                        if "id" in tc and tc["id"]:
                            accumulated_tool_calls[idx]["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            accumulated_tool_calls[idx]["name"] += fn["name"]
                        if fn.get("arguments"):
                            accumulated_tool_calls[idx]["arguments"] += fn["arguments"]
        except Exception as exc:
            # 网关返回 4xx/5xx 时把响应体带出来，否则线上只剩一句 "401 Unauthorized"，
            # 完全看不出对方说了什么（httpx 异常才有 .response）。
            detail = ""
            response = getattr(exc, "response", None)
            if response is not None:
                try:
                    detail = response.read().decode("utf-8", "replace")[:500]
                except Exception:
                    detail = "(unable to read response body)"
            message = f"{type(exc).__name__}: {exc}"
            if detail:
                message = f"{message} | body={detail}"
            self._publish("error", {"error": message})
            raise

        blocks = []
        published_calls = []

        if accumulated_text.strip():
            blocks.append({"type": "text", "text": accumulated_text.strip()})

        for idx in sorted(accumulated_tool_calls.keys()):
            tc = accumulated_tool_calls[idx]
            fn_name = tc["name"]
            try:
                fn_args = json_mod.loads(tc["arguments"]) if tc["arguments"] else {}
            except json_mod.JSONDecodeError:
                fn_args = {}
            blocks.append({
                "type": "tool_use",
                "id": tc["id"] or f"call_{idx}",
                "name": fn_name,
                "input": fn_args,
            })
            published_calls.append({"name": fn_name, "input": fn_args})

        if published_calls:
            self._publish("tool_calls", {"calls": published_calls})

        result = {"role": "assistant", "content": blocks}
        if accumulated_reasoning:
            result["reasoning_content"] = accumulated_reasoning
        return result

    def _publish(self, event_type: str, data: dict) -> None:
        if self._broker and self._task_id:
            self._broker.publish(self._task_id, {"type": event_type, **data})

    def close(self):
        self._client.close()


# ═══════════════════════════════════════════════════════════════════════
# 配置构建
# ═══════════════════════════════════════════════════════════════════════

def _build_config(
    data: dict,
    mcp_path: str,
    skill_loader: SkillLoader | None = None,
    discovered_refs: list[SkillRef] | None = None,
) -> tuple[SessionConfig, SkillLoader]:
    """从请求数据构建 SessionConfig。

    skill_loader 与 discovered_refs 通常由 _run_worker_process 预创建并传入，
    避免每个请求重复扫描 skills 目录。未传入时走旧兼容路径。
    """
    config: SessionConfig = build_config_from_mcp_json(mcp_path)
    config.session_id = data.get("session_id", "")
    config.trace_id = data.get("trace_id", "")
    config.tool_policy = ToolPolicy(allow=data.get("tool_allowlist", ["*"]))
    config.budget = BudgetConfig(
        max_turns=data.get("max_turns", 50),
        max_tool_calls=data.get("max_tool_calls", 70),
        max_time_s=data.get("max_time_s", 1200),
    )
    config.delivery = answer_v1_delivery()

    if skill_loader is not None and discovered_refs is not None:
        config.skills = list(config.skills) + discovered_refs
    elif skill_loader is not None:
        config.skills = list(config.skills) + skill_loader.discover_skills()
    else:
        skill_loader = SkillLoader()
        config.skills = list(config.skills) + skill_loader.discover_skills()

    skill_ids = data.get("skill_ids", [])
    if skill_ids:
        config.skills = [s for s in config.skills if s.id in skill_ids]
    return config, skill_loader


# ═══════════════════════════════════════════════════════════════════════
# 线程工作循环
# ═══════════════════════════════════════════════════════════════════════

def _process_one(
    pool: ToolManagerPool,
    skill_loader: SkillLoader,
    discovered_refs: list[SkillRef],
    mcp_path: str,
    stop_event: threading.Event,
    thread_id: int,
) -> None:
    """单个工作线程的主循环。

    循环: dequeue → build_config → pool.borrow → execute → update → 重复
    使用流式 LLM，逐 token 通过 StreamBroker 推送到 SSE 客户端。
    """
    from .stream_broker import get_broker
    broker = get_broker()
    llm = _StreamingWorkerLLM(broker=broker)
    logger.info("worker-thread-%d: 就绪（流式）", thread_id)

    try:
        while not stop_event.is_set():
            item = task_store.dequeue(timeout=5.0)
            if item is None:
                continue

            task_id, request_data = item
            llm.set_task_id(task_id)

            msg_preview = request_data.get("user_message", "")[:80]
            logger.info("[%s] thread-%d 开始: %s", task_id, thread_id, msg_preview)
            task_store.update(task_id, "running")
            broker.publish(task_id, {"type": "status", "status": "running"})
            lease_stop = threading.Event()

            def _renew_lease(current_id=task_id):
                while not lease_stop.wait(30):
                    task_store.touch(current_id)

            lease_thread = threading.Thread(
                target=_renew_lease,
                daemon=True,
                name=f"task-lease-{task_id[:8]}",
            )
            lease_thread.start()

            try:
                cancel_token = CancellationToken(
                    remote_check=lambda current_id=task_id: task_store.is_cancel_requested(current_id)
                )
                config, sk_loader = _build_config(
                    request_data, mcp_path, skill_loader, discovered_refs,
                )
                user_headers = task_store.load_auth_context(
                    request_data.get("auth_context_id"), task_id
                )

                def _on_tool_results(results, elapsed_ms):
                    for r in results:
                        name = r.get("name", "")
                        content = r.get("content", "") if not r.get("is_error") else None
                        broker.publish(task_id, {
                            "type": "tool_result",
                            "name": name,
                            "is_error": r.get("is_error", False),
                            "content": content,
                            "error": r.get("error") if r.get("is_error") else None,
                        })
                        if (
                            not r.get("is_error")
                            and "problem-resolve-path-display" in name
                            and content
                        ):
                            try:
                                import json as json_mod
                                path_payload = json_mod.loads(content) if isinstance(content, str) else content
                            except (TypeError, ValueError):
                                path_payload = content
                            broker.publish(task_id, {
                                "type": "solution_path",
                                "path": path_payload,
                            })

                history = request_data.get("history") or []
                with pool.borrow(timeout=120.0) as tm:
                    with tm.bind_user_headers(user_headers):
                        runtime = AgentRuntime(
                            llm_backend=llm,
                            tool_manager=tm,
                            trace_sink=TaskEventTraceSink(task_id),
                            skill_loader=sk_loader,
                            on_tool_results=_on_tool_results,
                        )
                        result = runtime.run_session(
                            config,
                            request_data["user_message"],
                            cancel_token=cancel_token,
                            history=history,
                        )

                if result.status == "ok":
                    task_store.update(task_id, "completed", result={
                        "payload": result.payload,
                        "spent": result.spent,
                        "trace_ref": result.trace_ref,
                    })
                    broker.publish(task_id, {
                        "type": "status", "status": "completed",
                        "payload": result.payload, "spent": result.spent,
                        "trace_ref": result.trace_ref,
                    })
                    logger.info(
                        "[%s] thread-%d 完成: turns=%s, tool_calls=%s",
                        task_id, thread_id,
                        result.spent.get("turns"), result.spent.get("tool_calls"),
                    )
                elif result.status == "cancelled":
                    task_store.update(task_id, "cancelled", error="Task cancelled")
                    broker.publish(task_id, {
                        "type": "status", "status": "cancelled",
                    })
                    logger.info("[%s] thread-%d 已取消", task_id, thread_id)
                else:
                    err = result.error
                    error_msg = f"{err.code.value}: {err.message}" if err else "unknown"
                    task_store.update(task_id, "failed", error=error_msg)
                    broker.publish(task_id, {
                        "type": "status", "status": "failed", "error": error_msg,
                    })
                    logger.warning("[%s] thread-%d 失败: %s", task_id, thread_id, error_msg)

            except Exception as exc:
                task_store.update(task_id, "failed", error=str(exc))
                broker.publish(task_id, {
                    "type": "status", "status": "failed", "error": str(exc),
                })
                logger.exception("[%s] thread-%d 异常: %s", task_id, thread_id, exc)
            finally:
                lease_stop.set()
                lease_thread.join(timeout=1)
                broker.publish(task_id, {"type": "done"})
                info = task_store.get(task_id)
                if info and info.get("status") in ("completed", "failed", "cancelled"):
                    task_store.delete_auth_context(request_data.get("auth_context_id"))

    finally:
        llm.close()
        logger.info("worker-thread-%d: 已退出", thread_id)
