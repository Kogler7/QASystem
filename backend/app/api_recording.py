"""API 请求完整记录：普通 API 测试时把每次请求的输入、中间产物、耗时与日志落盘。

- 每次请求一个目录 ``<记录根目录>/<日期时间戳>_<request_id>/``：``uploads/``（上传
  文件原文）、``params.json``（endpoint/方法/时间/参数，不含文件内容）、
  ``ontology.txt``、``stage_parse/``、``stage_chunk/``、``stage_extract/`` 等中间
  产物、``response.json``、``timing.json``（各阶段耗时 + 总耗时）、``run.log``
  （请求处理期间的详细日志）。
- 配置：``API_RECORD_ENABLED``（默认 1 开）、固定目录
  ``backend/data/api_records``（容器内可写的 backend-data 卷）、
  ``API_RECORD_RETENTION_DAYS``（默认 7，<=0 不清理）。
- 清理：lifespan 启动时与每次新建记录前各执行一次，只删记录根目录下符合
  ``<8位日期>-<6位时间>_*`` 命名且时间戳（取目录名，取不到用 mtime）超过保留期
  的目录。
- 红线：只写显式传入的参数与产物，从不触碰请求头，Authorization 等鉴权内容
  通过敏感字段与Bearer脱敏；原始上传仍属于业务数据。记录失败只告警，不影响响应。
"""
from __future__ import annotations
from qasystem_config.configuration import getenv as config_getenv

import contextvars
import json
import logging
import os
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator
from uuid import uuid4

from backend.app import observability
from algorithm.extraction_trace import ExtractionTrace

logger = logging.getLogger(__name__)

#: 单文件分块写盘粒度：上传原文/响应体已是内存 bytes，分块写避免再拼大缓冲。
_CHUNK_SIZE = 1024 * 1024

_RECORD_DIR_RE = re.compile(r"^(\d{8}-\d{6})_")
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_FALSE_VALUES = {"0", "false", "no", "off"}
_PRIVATE_FIELDS = re.compile(r"authorization|api.?key|credential|password|secret|access.?token|refresh.?token|headers", re.I)


def sanitize_record(value: Any) -> Any:
    """Record data only; never persist authentication fields or SDK objects."""
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if _PRIVATE_FIELDS.search(str(k)) else sanitize_record(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_record(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)Bearer\s+[^\s"\']+', 'Bearer [REDACTED]', value)
        return re.sub(r'(?i)(authorization|api[_-]?key|password|secret)\s*[:=]\s*["\']?[^\s,"\'\]}]+',
                      r'\1=[REDACTED]', value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return f"<object:{type(value).__name__}>"


def recording_enabled() -> bool:
    """API_RECORD_ENABLED：默认开；仅显式 0/false/no/off 关闭。"""
    return config_getenv("API_RECORD_ENABLED", "1").strip().lower() not in _FALSE_VALUES


def record_root() -> Path:
    """固定 backend-data 卷路径；不接受独立环境变量目录。"""
    return Path(__file__).resolve().parents[1] / "data" / "api_records"


def record_retention_days() -> float:
    """API_RECORD_RETENTION_DAYS：默认 7；非法值按默认；<=0 表示不清理。"""
    raw = config_getenv("API_RECORD_RETENTION_DAYS", "7").strip()
    try:
        return float(raw)
    except ValueError:
        logger.warning("[API记录] API_RECORD_RETENTION_DAYS 无效，按 7 天处理")
        return 7.0


def cleanup_old_records(root: Path | None = None, retention_days: float | None = None) -> int:
    """删除记录根目录下超过保留期的记录目录，返回删除数；任何失败只告警返回 0。

    只碰符合 ``<8位日期>-<6位时间>_*`` 命名的目录，其余文件/目录一律不动。
    """
    try:
        retention = record_retention_days() if retention_days is None else float(retention_days)
        if retention <= 0:
            return 0
        root = root or record_root()
        cutoff = datetime.now() - timedelta(days=retention)
        removed = 0
        for child in root.iterdir():
            if not child.is_dir():
                continue
            match = _RECORD_DIR_RE.match(child.name)
            if not match:
                continue
            try:
                stamp = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S")
            except ValueError:
                try:
                    stamp = datetime.fromtimestamp(child.stat().st_mtime)
                except OSError:
                    continue
            if stamp >= cutoff:
                continue
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
        if removed:
            logger.info("[API记录] 清理过期请求记录 %d 个（保留 %.1f 天）: %s", removed, retention, root)
        return removed
    except Exception as exc:  # noqa: BLE001 - 清理失败绝不影响服务
        logger.error("[API记录] 请求记录清理失败: %s", exc)
        return 0


class _RequestIdFilter(logging.Filter):
    """只放行当前线程上下文 request_id 匹配的日志记录。"""

    def __init__(self, request_id: str):
        super().__init__()
        self._request_id = request_id

    def filter(self, record: logging.LogRecord) -> bool:
        return observability.get_request_id() == self._request_id


class _SafeFormatter(logging.Formatter):
    def format(self, record):
        return sanitize_record(super().format(record))


class RequestRecorder:
    """单次 API 请求的记录器；目录不可用时整体降级为 no-op。

    所有公开方法内部判 enabled 并吞掉异常：调用方（router/service）可以无条件
    调用，记录失败只会留下 ERROR 日志，绝不影响 API 响应。
    """

    def __init__(self, method: str, endpoint: str):
        self.method = method
        self.endpoint = endpoint
        self.request_id = observability.get_request_id() or observability.new_request_id()
        self.started_at = datetime.now()
        self._started_monotonic = time.monotonic()
        self._params: dict[str, Any] = {}
        self._stages: list[dict[str, Any]] = []
        self._status = "unknown"
        self._finalized = False
        self._log_handler: logging.Handler | None = None
        self._log_targets: list[logging.Logger] = []
        self._stage_listener: Any = None
        self.directory: Path | None = None
        self.enabled = False

    def start(self) -> "RequestRecorder":
        """按配置启用并创建记录目录；目录不可用只告警、本次请求不记录。"""
        if not recording_enabled():
            return self
        try:
            root = record_root()
            cleanup_old_records(root)
            stamp = self.started_at.strftime("%Y%m%d-%H%M%S")
            safe_id = _UNSAFE_CHARS.sub("_", self.request_id)[:64] or "unknown"
            suffix = f"_worker_{uuid4().hex[:12]}" if self.method == "WORKER" else ""
            directory = root / f"{stamp}_{safe_id}{suffix}"
            directory.mkdir(parents=True, exist_ok=True)
            self.directory = directory
            self.enabled = True
        except Exception as exc:  # noqa: BLE001 - 目录不可写时降级为不记录
            logger.error(
                "[API记录] 记录目录创建失败，本次请求不记录: %s %s error=%s",
                self.method,
                self.endpoint,
                exc,
            )
            self.directory = None
            self.enabled = False
            return self
        self._attach_run_log()
        self._attach_stage_listener()
        return self

    # ------------------------------------------------------------------
    # run.log：root + 当前已存在的 propagate=False logger（ensure_logging 的
    # 服务/算法 logger 不冒泡到 root，只能直挂）；请求中途新建的此类 logger
    # 与异步 worker 进程日志不在覆盖范围，已在文件首部注明。
    # ------------------------------------------------------------------

    def _attach_run_log(self) -> None:
        try:
            path = self.directory / "run.log"
            path.write_text(
                f"# request_id={self.request_id}\n"
                f"# {self.method} {self.endpoint}\n"
                f"# started_at={self.started_at.isoformat(timespec='seconds')}\n"
                "# 覆盖范围：本进程内、记录挂载时已存在的 logger 上 request_id 上下文匹配的日志；\n"
                "# 当前目录仅覆盖所在进程；worker有独立目录，按同一request_id关联。请求中途新建的 propagate=False logger 不覆盖。\n",
                encoding="utf-8",
            )
            handler = logging.FileHandler(path, mode="a", encoding="utf-8")
            handler.setFormatter(_SafeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
            handler.addFilter(_RequestIdFilter(self.request_id))
            targets = [logging.getLogger()]
            for existing in list(logging.Logger.manager.loggerDict.values()):
                if isinstance(existing, logging.Logger) and existing.propagate is False:
                    targets.append(existing)
            for target in targets:
                target.addHandler(handler)
            self._log_handler = handler
            self._log_targets = targets
        except Exception as exc:  # noqa: BLE001 - 日志挂载失败不影响记录其余产物
            logger.error("[API记录] run.log 挂载失败: %s", exc)
            self._log_handler = None
            self._log_targets = []

    def _attach_stage_listener(self) -> None:
        def _listener(stage: str, duration: float) -> None:
            if observability.get_request_id() == self.request_id:
                self.add_stage_timing(stage, duration)

        observability.add_stage_duration_listener(_listener)
        self._stage_listener = _listener

    def _detach(self) -> None:
        if self._log_handler is not None:
            for target in self._log_targets:
                try:
                    target.removeHandler(self._log_handler)
                except Exception:  # noqa: BLE001 - 摘挂失败忽略
                    pass
            try:
                self._log_handler.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_handler = None
            self._log_targets = []
        if self._stage_listener is not None:
            observability.remove_stage_duration_listener(self._stage_listener)
            self._stage_listener = None

    # ------------------------------------------------------------------
    # 落盘内容
    # ------------------------------------------------------------------

    def _resolve(self, relative: str) -> Path | None:
        """把相对路径解析进记录目录：逐段清洗非法字符、拒绝 .. 逃逸。"""
        if not self.enabled or self.directory is None:
            return None
        parts = []
        for part in Path(str(relative)).parts:
            if part in ("", ".", ".."):
                continue
            parts.append(_UNSAFE_CHARS.sub("_", part))
        if not parts:
            return None
        return self.directory.joinpath(*parts)

    def _write_bytes(self, target: Path, content: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        view = memoryview(content)
        with target.open("wb") as handle:
            for offset in range(0, len(view), _CHUNK_SIZE):
                handle.write(view[offset : offset + _CHUNK_SIZE])

    def write_text(self, relative: str, content: str) -> str | None:
        target = self._resolve(relative)
        if target is None:
            return None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return str(target)
        except Exception as exc:  # noqa: BLE001 - 单文件落盘失败不影响请求
            logger.error("[API记录] 文本写入失败 %s: %s", relative, exc)
            return None

    def write_json(self, relative: str, value: Any) -> str | None:
        if not self.enabled:
            return None
        try:
            text = json.dumps(sanitize_record(value), ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001 - 序列化失败只告警
            logger.error("[API记录] JSON 序列化失败 %s: %s", relative, exc)
            return None
        return self.write_text(relative, text)

    def save_uploads(self, uploads: Iterable[tuple[str, bytes]]) -> None:
        """保存上传文件原文到 uploads/；同名文件追加序号避免覆盖。"""
        if not self.enabled:
            return
        used: set[str] = set()
        for name, content in uploads or []:
            if content is None:
                continue
            base = _UNSAFE_CHARS.sub("_", Path(name or "upload.bin").name) or "upload.bin"
            candidate = base
            index = 2
            while candidate in used:
                candidate = f"{Path(base).stem}__{index}{Path(base).suffix}"
                index += 1
            used.add(candidate)
            target = self._resolve(f"uploads/{candidate}")
            if target is None:
                continue
            try:
                self._write_bytes(target, bytes(content))
            except Exception as exc:  # noqa: BLE001 - 单个上传落盘失败跳过
                logger.error("[API记录] 上传文件写入失败 %s: %s", name, exc)

    def set_params(self, fields: dict[str, Any] | None = None, **kwargs: Any) -> None:
        """合并请求参数（params.json 内容）；bytes 一律不落参数，只记长度占位。"""
        if not self.enabled:
            return
        try:
            for key, value in {**dict(fields or {}), **kwargs}.items():
                if isinstance(value, (bytes, bytearray, memoryview)):
                    value = f"<bytes:{len(value)}>"
                self._params[key] = value
        except Exception as exc:  # noqa: BLE001
            logger.error("[API记录] 参数记录失败: %s", exc)

    def save_ontology(self, ontology: str) -> None:
        if (ontology or "").strip():
            self.write_text("ontology.txt", ontology)

    def add_stage_timing(self, stage: str, duration_seconds: float) -> None:
        if not self.enabled:
            return
        try:
            self._stages.append({"stage": str(stage), "duration_seconds": round(float(duration_seconds), 3)})
        except Exception:  # noqa: BLE001
            pass

    def capture_response(self, response: Any) -> None:
        """保存最终响应 payload（response.json）并按 HTTP 状态归类成功/失败。"""
        if not self.enabled:
            return
        try:
            status_code = getattr(response, "status_code", None)
            if status_code is not None:
                self._status = "succeeded" if int(status_code) < 400 else "failed"
            body = getattr(response, "body", None)
            target = self._resolve("response.json")
            if target is not None and isinstance(body, (bytes, bytearray)):
                try:
                    self.write_json("response.json", json.loads(body))
                except (ValueError, TypeError):
                    self._write_bytes(target, sanitize_record(bytes(body).decode("utf-8", errors="replace")).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.error("[API记录] 响应落盘失败: %s", exc)

    def finalize(self, *, error: str | None = None) -> None:
        """收尾：摘除日志/耗时挂载，写 params.json 与 timing.json；幂等。"""
        if self._finalized:
            return
        self._finalized = True
        self._detach()
        if not self.enabled:
            return
        try:
            finished_at = datetime.now()
            self.write_json(
                "params.json",
                {
                    "method": self.method,
                    "endpoint": self.endpoint,
                    "request_id": self.request_id,
                    "started_at": self.started_at.isoformat(timespec="seconds"),
                    "params": self._params,
                },
            )
            timing: dict[str, Any] = {
                "request_id": self.request_id,
                "method": self.method,
                "endpoint": self.endpoint,
                "started_at": self.started_at.isoformat(timespec="seconds"),
                "finished_at": finished_at.isoformat(timespec="seconds"),
                "total_seconds": round(time.monotonic() - self._started_monotonic, 3),
                "status": "failed" if error else self._status,
                "stages": list(self._stages),
            }
            if error:
                timing["error"] = error
            self.write_json("timing.json", timing)
        except Exception as exc:  # noqa: BLE001
            logger.error("[API记录] 收尾落盘失败: %s", exc)


#: 无激活记录时 current_recorder() 的返回值：全部方法 no-op。
_NULL_RECORDER = RequestRecorder("GET", "-")

_active_recorder: contextvars.ContextVar[RequestRecorder | None] = contextvars.ContextVar(
    "qasystem_api_recorder", default=None
)


def current_recorder() -> RequestRecorder:
    """当前请求上下文的记录器；不在记录上下文中时返回 no-op 记录器。"""
    return _active_recorder.get() or _NULL_RECORDER


def create_extraction_trace(*, config=None, secrets=()):
    """Raw model calls share the request's enable switch and retention directory."""
    recorder = _active_recorder.get()
    if recorder is None:
        # Direct algorithm calls have no HTTP context but still receive an ID
        # and the same writable data-volume retention policy.
        recorder = RequestRecorder("INTERNAL", "extraction").start()
        recorder.finalize()
    if not recorder.enabled or recorder.directory is None:
        return None
    return ExtractionTrace(recorder.directory / "model_calls", config=config, secrets=secrets,
                           request_id=recorder.request_id, flat=True)


@contextmanager
def record_operation(operation: str, payload: dict[str, Any]):
    """Worker recording boundary; inline handlers reuse their HTTP recorder."""
    if _active_recorder.get() is not None:
        yield current_recorder()
        return
    execution = payload.get("__execution") or {}
    request_id = payload.get("__api_record_request_id") or execution.get("job_id")
    with observability.request_id_scope(str(request_id) if request_id else None):
        with record_request("WORKER", operation) as recorder:
            recorder.set_params(operation=operation, execution=execution, payload=payload)
            yield recorder
            recorder._status = "succeeded"


@contextmanager
def record_request(method: str, endpoint: str) -> Iterator[RequestRecorder]:
    """HTTP 入口的记录上下文：进入建目录挂日志，退出写 params/timing 并摘挂。

    contextvars 传播覆盖 inline 执行（anyio 线程池带上下文）；异步队列模式下
    worker通过 record_operation 单独挂载执行记录，按入队的 request_id 与网关记录关联。
    """
    recorder = RequestRecorder(method, endpoint).start()
    token = _active_recorder.set(recorder)
    try:
        yield recorder
    except Exception as exc:
        recorder.finalize(error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        _active_recorder.reset(token)
        recorder.finalize()


__all__ = [
    "RequestRecorder",
    "cleanup_old_records",
    "current_recorder",
    "record_request",
    "record_retention_days",
    "record_root",
    "recording_enabled",
]
