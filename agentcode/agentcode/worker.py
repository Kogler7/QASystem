"""agent-worker — 多进程 + 多线程并发任务执行器。

核心架构:
    main() 主进程
      ├── 单进程模式 (--workers 1): 直接在当前进程启动 M 个线程
      └── 多进程模式 (--workers N): fork N 个子进程，每个启动 M 个线程

每个线程独立从 Redis 队列拉取任务 → 借 ToolManager → 执行 Agent → 归还

所有组件（task_store / ToolManagerPool / AgentRuntime）均天然线程安全，
Redis BLPOP 在多进程/多线程间自动分发任务，无需额外协调。

启动:
    agentcode-worker --workers 2 --concurrency 8 --pool-size 8

环境变量:
    AGENT_WORKER_WORKERS       Worker 进程数，默认 1
    AGENT_WORKER_CONCURRENCY   每进程工作线程数，默认 4
    AGENT_WORKER_POOL_SIZE     ToolManager 连接池大小，默认 = concurrency
    TASK_STORE_REDIS_URL       Redis 连接 URL
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import sys
import threading
import time
from pathlib import Path

from . import task_store
from .adapter import (
    _load_env,
    _chat_completions_url,
    _messages_to_openai,
    _mcp_tools_to_openai,
    _default_mcp_path,
)
from .models import BudgetConfig, SessionConfig, SkillRef, ToolPolicy
from .pool import ToolManagerPool
from .runtime import AgentRuntime
from .skillsets.loader import SkillLoader
from .tools.mcp_tools.mcp_loader import build_config_from_mcp_json
from .trace import NoopTraceSink

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

        resp = self._client.post(self._url, json=payload, headers=headers)
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
            with self._client.stream("POST", self._url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json_mod.loads(data_str)
                    except json_mod.JSONDecodeError:
                        continue

                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    finish_reason = choice.get("finish_reason") or finish_reason

                    if "reasoning_content" in delta and delta["reasoning_content"]:
                        accumulated_reasoning += delta["reasoning_content"]

                    if "content" in delta and delta["content"]:
                        text = delta["content"]
                        accumulated_text += text
                        self._publish("token", {"text": text})

                    if "tool_calls" in delta:
                        for tc in delta["tool_calls"]:
                            idx = tc.get("index", 0)
                            if idx not in accumulated_tool_calls:
                                accumulated_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                            if "id" in tc and tc["id"]:
                                accumulated_tool_calls[idx]["id"] = tc["id"]
                            if "function" in tc:
                                fn = tc["function"]
                                if "name" in fn and fn["name"]:
                                    accumulated_tool_calls[idx]["name"] += fn["name"]
                                if "arguments" in fn and fn["arguments"]:
                                    accumulated_tool_calls[idx]["arguments"] += fn["arguments"]
        except Exception as exc:
            self._publish("error", {"error": str(exc)})
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
    避免每个请求重复扫描 skillset 目录。未传入时走旧兼容路径。
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

    if skill_loader is not None and discovered_refs is not None:
        config.skills = list(config.skills) + discovered_refs
    elif skill_loader is not None:
        config.skills = list(config.skills) + skill_loader.discover_skills()
    else:
        skillset_dir = Path(__file__).resolve().parent / "skillsets"
        skill_loader = SkillLoader(skillset_dir=skillset_dir)
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

            try:
                config, sk_loader = _build_config(
                    request_data, mcp_path, skill_loader, discovered_refs,
                )

                def _on_tool_results(results, elapsed_ms):
                    for r in results:
                        broker.publish(task_id, {
                            "type": "tool_result",
                            "name": r.get("name", ""),
                            "is_error": r.get("is_error", False),
                            "content": r.get("content", "") if not r.get("is_error") else None,
                            "error": r.get("error") if r.get("is_error") else None,
                        })

                with pool.borrow(timeout=120.0) as tm:
                    runtime = AgentRuntime(
                        llm_backend=llm,
                        tool_manager=tm,
                        trace_sink=NoopTraceSink(),
                        skill_loader=sk_loader,
                        on_tool_results=_on_tool_results,
                    )
                    result = runtime.run_session(config, request_data["user_message"])

                if result.status == "ok":
                    task_store.update(task_id, "completed", result={
                        "payload": result.payload,
                        "spent": result.spent,
                    })
                    broker.publish(task_id, {
                        "type": "status", "status": "completed",
                        "payload": result.payload, "spent": result.spent,
                    })
                    logger.info(
                        "[%s] thread-%d 完成: turns=%s, tool_calls=%s",
                        task_id, thread_id,
                        result.spent.get("turns"), result.spent.get("tool_calls"),
                    )
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
                broker.publish(task_id, {"type": "done"})

    finally:
        llm.close()
        logger.info("worker-thread-%d: 已退出", thread_id)


# ═══════════════════════════════════════════════════════════════════════
# Worker 进程（单进程内管理池 + 线程）
# ═══════════════════════════════════════════════════════════════════════

def _run_worker_process(
    concurrency: int,
    pool_size: int,
    mcp_path: str,
    stop_event: threading.Event | multiprocessing.Event,
) -> None:
    """启动一个 Worker 进程：创建 ToolManagerPool → 预加载 Skills → 启动线程。

    在 stop_event 被设置前一直运行。收到停止信号后等待所有线程完成当前任务再退出。
    """
    # ── 创建 ToolManager 连接池 ──
    base_config = build_config_from_mcp_json(mcp_path)
    pool = ToolManagerPool(size=pool_size, config=base_config)
    pool.start()
    logger.info("worker 进程 pid=%d: pool %d/%d 就绪",
                os.getpid(), pool.stats()["total"], pool_size)

    # ── 预加载 Skills（只做一次，避免每个请求重复扫描）──
    skillset_dir = Path(__file__).resolve().parent / "skillsets"
    skill_loader = SkillLoader(skillset_dir=skillset_dir)
    discovered_refs = skill_loader.discover_skills()
    logger.info("worker 进程 pid=%d: 发现 %d 个 skill", os.getpid(), len(discovered_refs))

    # ── 启动工作线程 ──
    threads: list[threading.Thread] = []
    for i in range(concurrency):
        t = threading.Thread(
            target=_process_one,
            args=(pool, skill_loader, discovered_refs, mcp_path, stop_event, i),
            daemon=False,
            name=f"worker-{i}",
        )
        t.start()
        threads.append(t)

    logger.info("worker 进程 pid=%d: %d 个线程已启动, pool_size=%d",
                os.getpid(), concurrency, pool.stats()["total"])

    # ── 等待停止信号 ──
    stop_event.wait()

    logger.info("worker 进程 pid=%d: 收到停止信号，等待线程完成当前任务...", os.getpid())

    # 等待所有线程完成当前任务（最多等 60s）
    deadline = time.time() + 60
    for t in threads:
        remaining = max(1.0, deadline - time.time())
        t.join(timeout=remaining)

    # ── 关闭连接池 ──
    pool.shutdown()
    logger.info("worker 进程 pid=%d: 已退出", os.getpid())


# ═══════════════════════════════════════════════════════════════════════
# 旧兼容接口
# ═══════════════════════════════════════════════════════════════════════

def run(
    pool: ToolManagerPool,
    skill_loader: SkillLoader,
    mcp_path: str,
    concurrency: int = 1,
) -> None:
    """旧兼容接口：使用外部传入的 pool + skill_loader 在当前进程内运行。

    新代码推荐使用 main() → _run_worker_process() 路径以支持多进程。
    """
    stop_event = threading.Event()

    # 预加载 Skills
    discovered_refs = skill_loader.discover_skills()

    def _sig_handler(signum, frame):
        logger.info("收到信号 %d，开始优雅关闭...", signum)
        stop_event.set()

    prev_int = signal.signal(signal.SIGINT, _sig_handler)
    prev_term = signal.signal(signal.SIGTERM, _sig_handler)

    try:
        threads: list[threading.Thread] = []
        for i in range(concurrency):
            t = threading.Thread(
                target=_process_one,
                args=(pool, skill_loader, discovered_refs, mcp_path, stop_event, i),
                daemon=False,
                name=f"worker-{i}",
            )
            t.start()
            threads.append(t)

        logger.info("worker (旧接口): %d 个线程已启动", concurrency)

        for t in threads:
            t.join()

        logger.info("worker (旧接口): 所有线程已退出")
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)


# ═══════════════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════════════

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="agent-worker — 多进程 + 多线程并发任务执行器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  agentcode-worker                                     # 1 进程 × 4 线程
  agentcode-worker --concurrency 8                     # 1 进程 × 8 线程
  agentcode-worker --workers 2 --concurrency 8         # 2 进程 × 8 线程 = 16 并发
  agentcode-worker --workers 4 --concurrency 16        # 4 进程 × 16 线程 = 64 并发

环境变量:
  AGENT_WORKER_WORKERS       同 --workers
  AGENT_WORKER_CONCURRENCY   同 --concurrency
  AGENT_WORKER_POOL_SIZE     同 --pool-size
  TASK_STORE_REDIS_URL       Redis 连接 URL（多进程时必需）
""",
    )
    parser.add_argument("--workers", type=int, default=0,
                        help="Worker 进程数 (默认: env AGENT_WORKER_WORKERS or 1)")
    parser.add_argument("--concurrency", type=int, default=0,
                        help="每进程工作线程数 (默认: env AGENT_WORKER_CONCURRENCY or 4)")
    parser.add_argument("--pool-size", type=int, default=0,
                        help="ToolManager 连接池大小 (默认: =concurrency)")
    parser.add_argument("--port", type=int, default=0, help="（保留，兼容旧脚本）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    workers = args.workers or int(os.getenv("AGENT_WORKER_WORKERS", "1"))
    concurrency = args.concurrency or int(os.getenv("AGENT_WORKER_CONCURRENCY", "4"))
    pool_size = args.pool_size or int(os.getenv("AGENT_WORKER_POOL_SIZE", str(concurrency)))
    mcp_path = str(_default_mcp_path())

    if workers < 1:
        workers = 1
    if concurrency < 1:
        concurrency = 1
    if pool_size < concurrency:
        logger.warning("pool_size (%d) < concurrency (%d)，可能引发借连接超时", pool_size, concurrency)

    logger.info("启动 agent-worker: workers=%d, concurrency=%d, pool_size=%d, mcp=%s",
                workers, concurrency, pool_size, mcp_path)

    # ── 单进程模式（不涉及 multiprocessing）──
    if workers == 1:
        stop_event = threading.Event()

        def _single_sighandler(signum, frame):
            logger.info("收到信号 %d，开始优雅关闭...", signum)
            stop_event.set()

        signal.signal(signal.SIGINT, _single_sighandler)
        signal.signal(signal.SIGTERM, _single_sighandler)

        try:
            _run_worker_process(concurrency, pool_size, mcp_path, stop_event)
        except KeyboardInterrupt:
            logger.info("收到 KeyboardInterrupt")
        return

    # ── 多进程模式 ──
    # 注意：多进程模式下父进程不接触 task_store，确保 fork 安全
    stop_event = multiprocessing.Event()
    processes: list[multiprocessing.Process] = []

    for pid in range(workers):
        p = multiprocessing.Process(
            target=_run_worker_process,
            args=(concurrency, pool_size, mcp_path, stop_event),
            name=f"worker-proc-{pid}",
            daemon=False,
        )
        p.start()
        processes.append(p)
        logger.info("子进程 %d 已启动 (pid=%d)", pid, p.pid)

    # 父进程信号 → 广播给所有子进程
    def _parent_sighandler(signum, frame):
        logger.info("主进程收到信号 %d，通知 %d 个子进程退出...", signum, len(processes))
        stop_event.set()

    signal.signal(signal.SIGINT, _parent_sighandler)
    signal.signal(signal.SIGTERM, _parent_sighandler)

    # 等待所有子进程退出
    for p in processes:
        p.join()

    logger.info("主进程: 所有子进程已退出, 共 %d 个", len(processes))


if __name__ == "__main__":
    main()