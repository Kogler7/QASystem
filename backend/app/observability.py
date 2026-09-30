"""进程内可观测性支撑：request/job 上下文、告警 webhook 与阶段耗时指标。

- request/job 上下文用 contextvars 承载：HTTP 入口为无执行上下文的同步调用
  生成 request_id；执行层提交/运行时可改绑 job_id。日志通过
  ``context_prefix(request_id=get_request_id())`` 在记录时附带，未绑定时不输出。
- 告警走 ``QASYSTEM_ALERT_WEBHOOK``（钉钉/企业微信 text 格式）。未配置则静默
  跳过（DEBUG 日志）；同 event+request_id 1 分钟内去重；发送失败只记 ERROR
  日志，绝不影响主流程。
- 阶段耗时保存在进程内环形缓冲（每阶段最近 512 次），供指标端点输出分位数；
  超过环境变量预算时发送 slow_stage 告警（只告警，不中断任务）。
"""
from __future__ import annotations

import contextvars
import json
import logging
import math
import os
import threading
import time
import uuid
from collections import Counter, deque
from contextlib import contextmanager
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "qasystem_request_id", default=None
)


def get_request_id() -> str | None:
    """当前 request/job 上下文 id；未绑定时返回 None。"""
    return _request_id_var.get()


def set_request_id(request_id: str | None) -> contextvars.Token:
    """绑定 request/job 上下文，返回供 reset_request_id 使用的 token。"""
    return _request_id_var.set(request_id)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id_var.reset(token)


def new_request_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def request_id_scope(request_id: str | None = None) -> Iterator[str]:
    """确保当前上下文有 request_id：已绑定时沿用，否则生成（或用给定值）并绑定。

    HTTP 入口用本上下文管理器为无执行上下文的同步调用生成 request_id；
    执行层已在上下文里绑定 job_id 时不会覆盖。
    """
    existing = get_request_id()
    if existing is not None:
        yield existing
        return
    resolved = request_id or new_request_id()
    token = set_request_id(resolved)
    try:
        yield resolved
    finally:
        reset_request_id(token)


def context_prefix(**fields: Any) -> str:
    """把非空字段拼成日志前缀片段，如 ``request_id=xxx document=yyy ``；全空返回 ""。

    服务层日志记录时调用 ``context_prefix(request_id=get_request_id())``，
    没有 request_id 时不附加任何内容。
    """
    parts = [f"{key}={value}" for key, value in fields.items() if value is not None and value != ""]
    return (" ".join(parts) + " ") if parts else ""


# ---------------------------------------------------------------------------
# 告警 webhook
# ---------------------------------------------------------------------------

#: 同 event+request_id 的告警去重窗口（秒）。
ALERT_DEDUP_SECONDS = 60.0

_alert_last_sent: dict[tuple[str, str], float] = {}
_alert_lock = threading.Lock()


def _format_alert_content(event: str, request_id: str, payload: dict[str, Any]) -> str:
    lines = [f"【QASystem】{event}"]
    if request_id:
        lines.append(f"request_id: {request_id}")
    for key, value in payload.items():
        if key == "request_id" or value is None or value == "":
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, default=str)
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _post_webhook(webhook_url: str, content: str) -> None:
    """POST 钉钉/企业微信兼容的 text 消息；5 秒超时。失败由调用方捕获记录。"""
    import urllib.request

    body = json.dumps({"msgtype": "text", "text": {"content": content}}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5):
        pass


def send_alert(event: str, payload: dict[str, Any] | None = None) -> bool:
    """发送告警；同 event+request_id 在 60 秒内去重。返回是否真正发送。

    未配置 ``QASYSTEM_ALERT_WEBHOOK`` 时静默跳过（DEBUG 日志记录 skipped）；
    发送失败只记 ERROR 日志后返回 False，绝不影响主流程。
    """
    fields = dict(payload or {})
    request_id = str(fields.get("request_id") or get_request_id() or "")
    key = (event, request_id)
    now = time.monotonic()
    with _alert_lock:
        last = _alert_last_sent.get(key)
        if last is not None and now - last < ALERT_DEDUP_SECONDS:
            logger.debug("告警去重跳过: event=%s request_id=%s", event, request_id)
            return False
        _alert_last_sent[key] = now
    webhook = os.environ.get("QASYSTEM_ALERT_WEBHOOK", "").strip()
    if not webhook:
        logger.debug(
            "告警 skipped（未配置 QASYSTEM_ALERT_WEBHOOK）: event=%s request_id=%s",
            event,
            request_id,
        )
        return False
    content = _format_alert_content(event, request_id, fields)
    try:
        _post_webhook(webhook, content)
    except Exception as exc:  # noqa: BLE001 - 告警失败绝不影响主流程
        logger.error("告警发送失败: event=%s request_id=%s error=%s", event, request_id, exc)
        return False
    logger.info("告警已发送: event=%s request_id=%s", event, request_id)
    return True


# ---------------------------------------------------------------------------
# 阶段耗时样本与计数
# ---------------------------------------------------------------------------

#: 每个阶段保留的近期耗时样本上限。
STAGE_SAMPLE_LIMIT = 512

#: 各阶段慢预算对应的环境变量；未配置（或 <=0/非法）表示不告警。
_STAGE_BUDGET_ENV = {
    "parse": "QASYSTEM_ALERT_STAGE_SLOW_PARSE_SECONDS",
    "chunk": "QASYSTEM_ALERT_STAGE_SLOW_CHUNK_SECONDS",
    "extract": "QASYSTEM_ALERT_STAGE_SLOW_EXTRACT_SECONDS",
}

_stage_samples: dict[str, deque[float]] = {}
_stage_samples_lock = threading.Lock()
_counters: Counter[str] = Counter()
_counters_lock = threading.Lock()

#: 阶段耗时监听器：API 请求记录（api_recording）借此收集单次请求内各阶段耗时。
_stage_duration_listeners: list[Callable[[str, float], None]] = []
_stage_duration_listeners_lock = threading.Lock()


def add_stage_duration_listener(listener: Callable[[str, float], None]) -> None:
    """注册阶段耗时监听器；监听器抛错只记 DEBUG 日志，不影响主流程。"""
    with _stage_duration_listeners_lock:
        if listener not in _stage_duration_listeners:
            _stage_duration_listeners.append(listener)


def remove_stage_duration_listener(listener: Callable[[str, float], None]) -> None:
    with _stage_duration_listeners_lock:
        if listener in _stage_duration_listeners:
            _stage_duration_listeners.remove(listener)


def _stage_budget_seconds(stage: str) -> float | None:
    env_name = _STAGE_BUDGET_ENV.get(stage)
    if not env_name:
        return None
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.debug("慢阶段预算无效 %s=%s，按未配置处理", env_name, raw)
        return None
    return value if value > 0 else None


def increment_counter(name: str, amount: int = 1) -> None:
    with _counters_lock:
        _counters[name] += amount


def record_stage_duration(
    stage: str,
    duration_seconds: float,
    *,
    document: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """记录一次阶段耗时样本；超出对应环境变量预算时发 slow_stage 告警。

    只告警不强杀：预算仅触发告警与计数，任务照常完成。
    """
    duration = float(duration_seconds)
    with _stage_samples_lock:
        samples = _stage_samples.setdefault(stage, deque(maxlen=STAGE_SAMPLE_LIMIT))
        samples.append(duration)
    with _stage_duration_listeners_lock:
        listeners = list(_stage_duration_listeners)
    for listener in listeners:
        try:
            listener(stage, duration)
        except Exception:  # noqa: BLE001 - 监听器异常不影响主流程
            logger.debug("阶段耗时监听器异常: stage=%s", stage, exc_info=True)
    budget = _stage_budget_seconds(stage)
    if budget is None or duration <= budget:
        return
    increment_counter("slow_stage_total")
    payload = {
        "request_id": get_request_id(),
        "document": document,
        "stage": stage,
        "duration_seconds": round(duration, 3),
        "budget_seconds": budget,
    }
    if extra:
        payload.update(extra)
    send_alert("slow_stage", payload)


def _quantile(ordered: list[float], q: float) -> float | None:
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def stage_duration_summary() -> dict[str, Any]:
    """各阶段近期耗时样本的分位数（进程内环形缓冲，不引外部依赖）。"""
    with _stage_samples_lock:
        snapshot = {stage: list(samples) for stage, samples in _stage_samples.items()}
    summary: dict[str, Any] = {}
    for stage, values in snapshot.items():
        ordered = sorted(values)
        summary[stage] = {
            "count": len(ordered),
            "p50": _quantile(ordered, 0.5),
            "p90": _quantile(ordered, 0.9),
            "p99": _quantile(ordered, 0.99),
            "max": ordered[-1] if ordered else None,
        }
    return summary


# ---------------------------------------------------------------------------
# 执行层指标（队列深度 / job 状态计数，能读多少读多少）
# ---------------------------------------------------------------------------


def _read_execution_manager() -> Any | None:
    try:
        from backend.app.execution.manager import get_execution_manager

        return get_execution_manager()
    except Exception as exc:  # noqa: BLE001 - 执行层不可用不应让指标端点失败
        logger.debug("读取执行 manager 失败: %s", exc)
        return None


def _queue_depth(manager: Any) -> int | None:
    """当前队列深度：内存队列精确统计；Redis 队列按各资源流未确认消息数估算。"""
    queue = getattr(manager, "queue", None)
    if queue is None:
        return None
    queues = getattr(queue, "_queues", None)
    if isinstance(queues, dict):
        try:
            return sum(target.qsize() for target in queues.values())
        except Exception as exc:  # noqa: BLE001 - 指标尽力而为
            logger.debug("读取内存队列深度失败: %s", exc)
            return None
    redis_client = getattr(queue, "redis", None)
    prefix = getattr(queue, "prefix", None)
    if redis_client is not None and prefix:
        try:
            group = getattr(queue, "consumer_group", "workers")
            total = 0
            for key in redis_client.scan_iter(match=f"{prefix}:*", count=200):
                try:
                    pending = redis_client.xpending(key, group)
                    total += int(pending.get("pending", 0) if isinstance(pending, dict) else pending)
                except Exception:  # noqa: BLE001 - 单条流不可读时跳过
                    continue
            return total
        except Exception as exc:  # noqa: BLE001 - 指标尽力而为
            logger.debug("读取 Redis 队列深度失败: %s", exc)
            return None
    return None


def _plain_text(value: Any) -> str:
    """把 Redis 里 JSON 编码的标量（如带引号的字符串）还原为纯文本。"""
    if value is None:
        return "unknown"
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, str):
            return parsed
    except (ValueError, TypeError):
        pass
    return text


def _job_status_counts(store: Any) -> dict[str, int]:
    jobs = getattr(store, "jobs", None)
    if isinstance(jobs, dict):
        counts: Counter[str] = Counter(
            str((job or {}).get("status") or "unknown") for job in jobs.values()
        )
        return dict(sorted(counts.items()))
    redis_client = getattr(store, "redis", None)
    prefix = getattr(store, "prefix", None)
    if redis_client is not None and prefix:
        counts = Counter()
        try:
            scanned = 0
            for key in redis_client.scan_iter(match=f"{prefix}:job:*", count=200):
                status = redis_client.hget(key, "status")
                counts[_plain_text(status)] += 1
                scanned += 1
                if scanned >= 10000:
                    break
        except Exception as exc:  # noqa: BLE001 - 指标尽力而为
            logger.debug("读取 job 状态计数失败: %s", exc)
            return {}
        return dict(sorted(counts.items()))
    return {}


def metrics_payload(manager: Any | None = None) -> dict[str, Any]:
    """指标端点数据：队列深度、job 状态计数、告警计数、各阶段耗时分位数。"""
    if manager is None:
        manager = _read_execution_manager()
    store = getattr(manager, "store", None)
    with _counters_lock:
        counters = dict(_counters)
    return {
        "queue_depth": _queue_depth(manager) if manager is not None else None,
        "job_status_counts": _job_status_counts(store) if store is not None else {},
        "counters": counters,
        "stage_duration_samples": stage_duration_summary(),
    }


async def metrics_endpoint() -> dict[str, Any]:
    """GET /api/v1/observability/metrics：暴露方式与 /health 一致（不鉴权）。"""
    return metrics_payload()


__all__ = [
    "ALERT_DEDUP_SECONDS",
    "STAGE_SAMPLE_LIMIT",
    "add_stage_duration_listener",
    "context_prefix",
    "get_request_id",
    "increment_counter",
    "metrics_endpoint",
    "metrics_payload",
    "new_request_id",
    "record_stage_duration",
    "remove_stage_duration_listener",
    "request_id_scope",
    "reset_request_id",
    "send_alert",
    "set_request_id",
    "stage_duration_summary",
]
