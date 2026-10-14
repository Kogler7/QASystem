from __future__ import annotations
from docker.configuration import getenv as config_getenv
from docker.startup_contract import EXECUTION_LIVENESS, EXECUTION_TOPOLOGY

import argparse
import logging
import os
import signal
import socket
import time
import threading
import uuid

from backend.app.responses import ErrorCode, is_retryable

from .manager import ExecutionCancelled, ExecutionLeaseLost, ExecutionManager
from .operations import run_operation
from .queue import RedisTaskQueue
from .store import RedisExecutionStore
from .models import TERMINAL_STATUSES
from external_services.application import bind_user_headers


LOGGER = logging.getLogger("qasystem.worker")


def _error_retryable(exc: Exception) -> bool:
    """按错误码协议（docs/接口/错误码协议.md）判断任务失败是否值得重投。

    协议 retryable 集合的权威常量在 ``backend.app.responses``：业务异常
    携带 ``code``（ErrorCode，如 CapabilityError / PipelineError /
    PageShardError）时以协议为准——参数类 1xxx、解析/切片结果类 2xxx/3xxx、
    本体无效 4006 等确定性失败不再白重试；未知异常（网络/依赖抖动）保持
    可重试。取消不是失败，调用方先判 ``ExecutionCancelled``。
    """
    if isinstance(exc, ExecutionLeaseLost):
        return False
    code = getattr(exc, "code", None)
    if isinstance(code, ErrorCode):
        return is_retryable(code)
    return True


def _bind_gpu(device: str | None) -> None:
    if device is None:
        return
    # This function runs before importing operation handlers, which is the
    # boundary that may import Torch/MinerU.
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    os.environ["CUDA_VISIBLE_DEVICES"] = str(device)


class WorkerRuntime:
    def __init__(self, resource_class: str, worker_id: str | None = None, gpu_device: str | None = None):
        self.gpu_device = str(gpu_device).strip() if gpu_device else None
        _bind_gpu(self.gpu_device)
        self.resource_classes = tuple(item.strip() for item in resource_class.split(",") if item.strip()) or ("cpu.default",)
        self.resource_class = ",".join(self.resource_classes)
        configured_worker_id = worker_id or f"{resource_class}-{socket.gethostname()}-{os.getpid()}"
        if config_getenv("EXECUTION_WORKER_ID_UNIQUE", "false").lower() in {"1", "true", "yes", "on"}:
            configured_worker_id = f"{configured_worker_id}-{socket.gethostname()}"
        self.worker_id = configured_worker_id
        os.environ["EXECUTION_WORKER_ID"] = self.worker_id
        instance_id = uuid.uuid4().hex
        os.environ["EXECUTION_WORKER_INSTANCE_ID"] = instance_id
        self.consumer_id = self.worker_id + '@' + instance_id
        self.manager = ExecutionManager()
        if not isinstance(self.manager.store, RedisExecutionStore):
            raise RuntimeError("Worker requires EXECUTION_REDIS_URL and Redis; memory execution is test-only")
        self.queue: RedisTaskQueue = self.manager.queue
        self.stopped = False
        self._last_heartbeat = 0.0
        # PEL 滞留消息重投（崩溃恢复）：开关与阈值见 _reap。
        self._reaper_enabled = EXECUTION_TOPOLOGY['reaper_enabled']
        self._reaper_min_idle_seconds = float(config_getenv("EXECUTION_REAPER_MIN_IDLE_SECONDS", "600"))
        self._reaper_interval_seconds = EXECUTION_TOPOLOGY['reaper_interval_seconds']
        self._last_reap = 0.0

    def _heartbeat(self, *, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_heartbeat < EXECUTION_LIVENESS['heartbeat_seconds']:
            return
        devices = [self.gpu_device] if self.gpu_device else []
        snapshots = self.manager.scheduler.inventory.snapshots(force=True) if devices else []
        self.manager.scheduler.register_worker(
            worker_id=self.worker_id,
            resource_classes=self.resource_classes,
            gpu_devices=devices,
            snapshots=snapshots,
            endpoint=config_getenv("MINERU_VLM_SERVER_URL"),
            queue_consumer_id=getattr(self, 'consumer_id', None),
        )
        if getattr(self, 'consumer_id', None):
            self.queue.heartbeat_consumer(self.consumer_id)
        self._last_heartbeat = time.monotonic()

    def _retry_envelope(self, resource_class: str, envelope: dict) -> dict:
        next_envelope = {**envelope, "attempt": int(envelope.get("attempt", 1)) + 1}
        next_envelope.pop("reserved_allocation", None)
        next_envelope.pop("target_worker", None)
        return next_envelope

    def _reserve_gpu_capacity(self, resource_class: str, envelope: dict, job: dict) -> bool:
        if not resource_class.startswith("gpu.") or envelope.get("reserved_allocation"):
            return True
        reservation = self.manager.scheduler.reserve_worker(
            resource_class,
            required_worker_id=self.worker_id,
        )
        if reservation is not None:
            envelope["target_worker"] = self.worker_id
            envelope["reserved_allocation"] = reservation.get("allocation")
            return True

        if job.get("stage") != "waiting_for_capacity":
            self.manager.store.update_job(job["job_id"], status="queued", stage="waiting_for_capacity")
            self.manager.hub.emit(
                job["job_id"],
                "task.waiting_for_capacity",
                operation=job.get("operation"),
                stage="waiting_for_capacity",
                details={"resource_class": resource_class, "worker_id": self.worker_id},
            )
        self.queue.enqueue(resource_class, envelope)
        return False

    def _handle_failure(self, resource_class: str, envelope: dict, exc: Exception) -> None:
        """任务失败出口：取消优先，其次按协议 retryable 决定重投或终败。"""
        job_id = envelope.get("job_id")
        if isinstance(exc, ExecutionCancelled):
            self.manager.store.delete_auth_context(envelope.get("auth_context_id"))
            self.manager.store.update_job(
                job_id,
                status="cancelled",
                stage="cancel",
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            self.manager.hub.emit(job_id, "job.cancelled", operation=envelope.get("operation"), stage="cancel")
            LOGGER.info("task cancelled: job_id=%s", job_id)
            return
        attempt = int(envelope.get("attempt", 1))
        max_attempts = int(config_getenv("EXECUTION_MAX_ATTEMPTS", "3"))
        retryable = _error_retryable(exc)
        if attempt < max_attempts and retryable:
            next_envelope = self._retry_envelope(resource_class, envelope)
            self.manager.store.update_job(job_id, status="retry_wait", stage="retry", error={"type": type(exc).__name__, "message": str(exc)})
            self.manager.hub.emit(job_id, "task.retry_scheduled", level="WARN", operation=envelope.get("operation"), stage="retry", details={"attempt": attempt + 1, "max_attempts": max_attempts, "error": str(exc)})
            time.sleep(min(2**attempt, 10))
            try:
                self.queue.enqueue(resource_class, next_envelope, target_worker=next_envelope.get("target_worker"))
            except Exception:
                self.manager.release_allocation(next_envelope.get("reserved_allocation"))
                raise
            self.manager.store.update_job(job_id, status="queued", stage="queued")
            return
        error = {"type": type(exc).__name__, "message": str(exc)}
        if not retryable:
            error["retryable"] = False
            error["reason"] = "non_retryable_error_code"
        self.manager.store.delete_auth_context(envelope.get("auth_context_id"))
        self.manager.store.update_job(job_id, status="failed", stage="failed", error=error)
        self.manager.hub.emit(job_id, "job.failed", level="ERROR", operation=envelope.get("operation"), stage="failed", details=error)
        if retryable:
            LOGGER.exception("task failed permanently after %s attempts: job_id=%s", attempt, job_id)
        else:
            LOGGER.error("task failed with non-retryable error, no retry attempted: job_id=%s error=%s", job_id, error)

    def _maybe_reap(self) -> None:
        """周期性收割本 worker 名下滞留的 PEL 消息（worker 崩溃恢复）。"""
        if not self._reaper_enabled:
            return
        now = time.monotonic()
        if now - self._last_reap < self._reaper_interval_seconds:
            return
        self._last_reap = now
        for resource_class in self.resource_classes:
            try:
                self._reap(resource_class)
            except Exception:
                LOGGER.exception("reap pending messages failed: resource_class=%s", resource_class)

    def _reap(self, resource_class: str) -> None:
        """收回并代投本 worker 名下 idle 超阈值的滞留消息。

        不做超时强杀：min-idle 默认 600 秒（EXECUTION_REAPER_MIN_IDLE_SECONDS）。
        队列同时检查实际消费者实例的心跳；正常长任务不会因 idle 超阈值被抢走。共享队列与
        指向本 worker 的定向队列都收割；指向其他 worker 的定向消息不在此处理。
        """
        for target in (None, self.worker_id):
            for message_id, envelope in self.queue.reclaim(
                resource_class,
                getattr(self, 'consumer_id', self.worker_id),
                min_idle_seconds=self._reaper_min_idle_seconds,
                target_worker=target,
            ):
                self._handle_reclaimed(resource_class, message_id, envelope, target_worker=target)

    def _ack_reclaimed(self, resource_class, message_id, target_worker):
        if target_worker is None:
            self.queue.ack(resource_class, message_id)
        else:
            self.queue.ack(resource_class, message_id, target_worker=target_worker)

    def _handle_reclaimed(self, resource_class: str, message_id: str, envelope: dict, *, target_worker=None) -> None:
        """处理一条收回的滞留消息：重投（attempt+1）或按上限/取消态终态化。"""
        job_id = envelope.get("job_id")
        attempt = int(envelope.get("attempt", 1))
        max_attempts = int(config_getenv("EXECUTION_MAX_ATTEMPTS", "3"))
        job = self.manager.store.get_job(job_id) if job_id else None
        if not job:
            # job 记录已过期：消息没有可归属的任务，清掉鉴权上下文后丢弃。
            self.manager.store.delete_auth_context(envelope.get("auth_context_id"))
            self.manager.release_allocation(envelope.get('reserved_allocation'))
            self._ack_reclaimed(resource_class, message_id, target_worker)
            LOGGER.warning("reaped orphaned message without job, dropped: message_id=%s job_id=%s", message_id, job_id)
            return
        if job.get('status') in TERMINAL_STATUSES:
            # Completion can be durable even when the consumer crashes before ACK.
            self.manager.store.delete_auth_context(envelope.get('auth_context_id'))
            self.manager.release_allocation(envelope.get('reserved_allocation'))
            self._ack_reclaimed(resource_class, message_id, target_worker)
            return
        if job.get("cancel_requested"):
            self.manager.store.delete_auth_context(envelope.get("auth_context_id"))
            self.manager.release_allocation(envelope.get('reserved_allocation'))
            self.manager.store.update_job(
                job_id,
                status="cancelled",
                stage="reap",
                error={"type": "ExecutionCancelled", "message": "任务在滞留期间被取消，不再重投"},
            )
            self.manager.hub.emit(job_id, "job.cancelled", operation=envelope.get("operation"), stage="reap")
            self._ack_reclaimed(resource_class, message_id, target_worker)
            return
        if attempt >= max_attempts:
            error = {
                "type": "ExecutionReapLimitExceeded",
                "message": f"任务重投达到上限 {max_attempts}（含崩溃重投），不再重投",
            }
            self.manager.store.delete_auth_context(envelope.get("auth_context_id"))
            self.manager.release_allocation(envelope.get('reserved_allocation'))
            self.manager.store.update_job(job_id, status="failed", stage="reap", error=error)
            self.manager.hub.emit(job_id, "job.failed", level="ERROR", operation=envelope.get("operation"), stage="reap", details={**error, "attempt": attempt})
            self._ack_reclaimed(resource_class, message_id, target_worker)
            LOGGER.error("reaped message exceeded max attempts, job failed: job_id=%s attempt=%s", job_id, attempt)
            return
        next_envelope = self._retry_envelope(resource_class, envelope)
        # 旧 envelope 上的 GPU 租约属于已崩溃的 worker，重投前释放，
        # 避免容量被挂到租约 TTL（EXECUTION_GPU_LEASE_TTL_SECONDS）自然过期。
        self.manager.release_allocation(envelope.get("reserved_allocation"))
        try:
            self.queue.enqueue(resource_class, next_envelope, target_worker=next_envelope.get("target_worker"))
        except Exception:
            self.manager.release_allocation(next_envelope.get("reserved_allocation"))
            # 消息留在 PEL 里，下个收割周期再试。
            LOGGER.exception("re-enqueue reclaimed message failed, left pending: job_id=%s", job_id)
            return
        self.manager.store.update_job(
            job_id,
            status="queued",
            stage="requeued",
            error={"type": "ExecutionReaped", "message": f"worker 崩溃后由重投回收（attempt {attempt}→{attempt + 1}）"},
        )
        self.manager.hub.emit(
            job_id,
            "task.requeued",
            level="WARN",
            operation=envelope.get("operation"),
            stage="reap",
            details={"attempt": attempt + 1, "max_attempts": max_attempts, "reaped_message_id": str(message_id)},
        )
        self._ack_reclaimed(resource_class, message_id, target_worker)

    def stop(self, *_args) -> None:
        self.stopped = True

    def _keep_alive(self, stop_event) -> None:
        # A synchronous model call must not suppress process liveness reporting.
        while not stop_event.wait(EXECUTION_LIVENESS['heartbeat_seconds']):
            try:
                self._heartbeat(force=True)
            except Exception:
                LOGGER.exception('worker heartbeat failed: id=%s', self.worker_id)

    def run(self) -> None:
        stop_event = threading.Event()
        heartbeat = threading.Thread(target=self._keep_alive, args=(stop_event,), daemon=True)
        self._heartbeat(force=True)
        heartbeat.start()
        try:
            self._run_messages()
        finally:
            stop_event.set()
            heartbeat.join(timeout=6)
            self.manager.scheduler.unregister_worker(self.worker_id, queue_consumer_id=getattr(self, 'consumer_id', None))
            if getattr(self, 'consumer_id', None):
                self.queue.remove_consumer_heartbeat(self.consumer_id)

    def _run_messages(self) -> None:
        LOGGER.info("worker ready: id=%s resource_class=%s gpu=%s", self.worker_id, self.resource_class, config_getenv("CUDA_VISIBLE_DEVICES", ""))
        self._heartbeat(force=True)
        while not self.stopped:
            self._heartbeat()
            self._maybe_reap()
            handled = False
            for resource_class in self.resource_classes:
                if self.stopped:
                    break
                target = self.worker_id
                message = self.queue.consume(resource_class, getattr(self, 'consumer_id', self.worker_id), block_ms=10, target_worker=target)
                if message is None:
                    if self.stopped:
                        break
                    target = None
                    message = self.queue.consume(resource_class, getattr(self, 'consumer_id', self.worker_id), block_ms=50)
                if message is None:
                    continue
                handled = True
                message_id, envelope = message
                job_id = envelope.get("job_id")
                try:
                    job = self.manager.store.get_job(job_id)
                    if not job:
                        self.manager.store.delete_auth_context(envelope.get("auth_context_id"))
                        self.manager.release_allocation(envelope.get("reserved_allocation"))
                        continue
                    if job.get("cancel_requested"):
                        self.manager.store.delete_auth_context(job.get("auth_context_id"))
                        self.manager.release_allocation(envelope.get("reserved_allocation"))
                        self.manager.store.update_job(job_id, status="cancelled", stage="cancel")
                        self.manager.hub.emit(job_id, "job.cancelled", operation=job.get("operation"), stage="cancel")
                    else:
                        if not self._reserve_gpu_capacity(resource_class, envelope, job):
                            time.sleep(0.05)
                            continue
                        auth_context_id = envelope.get("auth_context_id") or job.get("auth_context_id")
                        user_headers = (
                            self.manager.store.load_auth_context(auth_context_id, job_id)
                            if auth_context_id
                            else {}
                        )
                        with bind_user_headers(user_headers):
                            self.manager.run_attempt(
                                job_id,
                                envelope,
                                lambda payload: run_operation(envelope["operation"], payload),
                                mark_job_failed=False,
                            )
                        self.manager.store.delete_auth_context(auth_context_id)
                except Exception as exc:
                    self._handle_failure(resource_class, envelope, exc)
                finally:
                    self.queue.ack(resource_class, message_id, target_worker=target)
                break
            if not handled:
                time.sleep(0.05)


def main() -> None:
    parser = argparse.ArgumentParser(description="QASystem durable execution worker")
    parser.add_argument("--resource-class", default=config_getenv("EXECUTION_RESOURCE_CLASS", "cpu.default"))
    parser.add_argument("--worker-id", default=config_getenv("EXECUTION_WORKER_ID"))
    parser.add_argument("--gpu-device", default=config_getenv("EXECUTION_GPU_DEVICE"))
    args = parser.parse_args()
    runtime = WorkerRuntime(args.resource_class, args.worker_id, args.gpu_device)
    signal.signal(signal.SIGTERM, runtime.stop)
    signal.signal(signal.SIGINT, runtime.stop)
    runtime.run()


if __name__ == "__main__":
    main()
