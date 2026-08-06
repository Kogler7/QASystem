from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import time
import uuid

from .manager import ExecutionManager
from .operations import run_operation
from .queue import RedisTaskQueue
from .store import RedisExecutionStore


LOGGER = logging.getLogger("qasystem.worker")


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
        if os.getenv("EXECUTION_WORKER_ID_UNIQUE", "false").lower() in {"1", "true", "yes", "on"}:
            configured_worker_id = f"{configured_worker_id}-{socket.gethostname()}"
        self.worker_id = configured_worker_id
        os.environ["EXECUTION_WORKER_ID"] = self.worker_id
        os.environ["EXECUTION_WORKER_INSTANCE_ID"] = uuid.uuid4().hex
        self.manager = ExecutionManager()
        if not isinstance(self.manager.store, RedisExecutionStore):
            raise RuntimeError("Worker requires EXECUTION_REDIS_URL and Redis; memory execution is test-only")
        self.queue: RedisTaskQueue = self.manager.queue
        self.stopped = False
        self._last_heartbeat = 0.0

    def _heartbeat(self, *, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_heartbeat < float(os.getenv("EXECUTION_WORKER_HEARTBEAT_SECONDS", "5")):
            return
        devices = [self.gpu_device] if self.gpu_device else []
        snapshots = self.manager.scheduler.inventory.snapshots(force=True) if devices else []
        self.manager.scheduler.register_worker(
            worker_id=self.worker_id,
            resource_classes=self.resource_classes,
            gpu_devices=devices,
            snapshots=snapshots,
            endpoint=os.getenv("MINERU_VLM_SERVER_URL"),
        )
        self._last_heartbeat = time.monotonic()

    def _retry_envelope(self, resource_class: str, envelope: dict) -> dict:
        next_envelope = {**envelope, "attempt": int(envelope.get("attempt", 1)) + 1}
        next_envelope.pop("reserved_allocation", None)
        next_envelope.pop("target_worker", None)
        if resource_class.startswith("gpu"):
            reservation = self.manager.scheduler.reserve_worker(resource_class)
            if reservation:
                next_envelope["target_worker"] = (reservation.get("worker") or {}).get("worker_id")
                next_envelope["reserved_allocation"] = reservation.get("allocation")
        return next_envelope

    def stop(self, *_args) -> None:
        self.stopped = True

    def run(self) -> None:
        LOGGER.info("worker ready: id=%s resource_class=%s gpu=%s", self.worker_id, self.resource_class, os.getenv("CUDA_VISIBLE_DEVICES", ""))
        self._heartbeat(force=True)
        while not self.stopped:
            self._heartbeat()
            handled = False
            for resource_class in self.resource_classes:
                target = self.worker_id
                message = self.queue.consume(resource_class, self.worker_id, block_ms=10, target_worker=target)
                if message is None:
                    target = None
                    message = self.queue.consume(resource_class, self.worker_id, block_ms=50)
                if message is None:
                    continue
                handled = True
                message_id, envelope = message
                job_id = envelope.get("job_id")
                try:
                    job = self.manager.store.get_job(job_id)
                    if not job:
                        self.manager.release_allocation(envelope.get("reserved_allocation"))
                        continue
                    if job.get("cancel_requested"):
                        self.manager.release_allocation(envelope.get("reserved_allocation"))
                        self.manager.store.update_job(job_id, status="cancelled", stage="cancel")
                        self.manager.hub.emit(job_id, "job.cancelled", operation=job.get("operation"), stage="cancel")
                    else:
                        self.manager.run_attempt(
                            job_id,
                            envelope,
                            lambda payload: run_operation(envelope["operation"], payload),
                            mark_job_failed=False,
                        )
                except Exception as exc:
                    attempt = int(envelope.get("attempt", 1))
                    max_attempts = int(os.getenv("EXECUTION_MAX_ATTEMPTS", "3"))
                    if attempt < max_attempts:
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
                    else:
                        self.manager.store.update_job(job_id, status="failed", stage="failed", error={"type": type(exc).__name__, "message": str(exc)})
                        self.manager.hub.emit(job_id, "job.failed", level="ERROR", operation=envelope.get("operation"), stage="failed", details={"type": type(exc).__name__, "message": str(exc)})
                        LOGGER.exception("task failed permanently: job_id=%s", job_id)
                finally:
                    self.queue.ack(resource_class, message_id, target_worker=target)
                break
            if not handled:
                time.sleep(0.05)
        self.manager.scheduler.unregister_worker(self.worker_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="QASystem durable execution worker")
    parser.add_argument("--resource-class", default=os.getenv("EXECUTION_RESOURCE_CLASS", "cpu.default"))
    parser.add_argument("--worker-id", default=os.getenv("EXECUTION_WORKER_ID"))
    parser.add_argument("--gpu-device", default=os.getenv("EXECUTION_GPU_DEVICE"))
    args = parser.parse_args()
    runtime = WorkerRuntime(args.resource_class, args.worker_id, args.gpu_device)
    signal.signal(signal.SIGTERM, runtime.stop)
    signal.signal(signal.SIGINT, runtime.stop)
    runtime.run()


if __name__ == "__main__":
    main()
