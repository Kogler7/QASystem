from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any


class ResourceLeaseUnavailable(RuntimeError):
    """Raised when a resource class has no capacity for the requested task."""


@dataclass(frozen=True, slots=True)
class GpuSnapshot:
    physical_index: str
    device_uuid: str | None
    memory_total_mb: int | None
    memory_used_mb: int | None
    utilization_gpu: int | None
    source: str

    @property
    def memory_free_mb(self) -> int | None:
        if self.memory_total_mb is None or self.memory_used_mb is None:
            return None
        return max(self.memory_total_mb - self.memory_used_mb, 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "physical_index": self.physical_index,
            "device_uuid": self.device_uuid,
            "memory_total_mb": self.memory_total_mb,
            "memory_used_mb": self.memory_used_mb,
            "memory_free_mb": self.memory_free_mb,
            "utilization_gpu": self.utilization_gpu,
            "snapshot_source": self.source,
        }


@dataclass(frozen=True, slots=True)
class GpuLease:
    lease_id: str
    device: GpuSnapshot
    memory_budget_mb: int
    owner: str
    expires_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "physical_index": self.device.physical_index,
            "logical_index": None,
            "device_uuid": self.device.device_uuid,
            "lease_id": self.lease_id,
            "memory_budget_mb": self.memory_budget_mb,
            "memory_total_mb": self.device.memory_total_mb,
            "memory_used_mb": self.device.memory_used_mb,
            "memory_free_mb": self.device.memory_free_mb,
            "utilization_gpu": self.device.utilization_gpu,
            "snapshot_source": self.device.source,
        }


def parse_device_ids(value: str | None) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def configured_gpu_ids() -> list[str]:
    for name in ("EXECUTION_GPU_DEVICE_IDS", "GPU_DEVICE_IDS", "CUDA_VISIBLE_DEVICES"):
        ids = parse_device_ids(os.getenv(name))
        if ids:
            return ids
    return []


def _parse_number(value: str) -> int | None:
    if value is None:
        return None
    value = str(value).strip().replace("[N/A]", "")
    if not value or value.upper() in {"N/A", "NOT SUPPORTED"}:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _query_nvidia_smi(device_ids: list[str]) -> list[GpuSnapshot]:
    command = [
        "nvidia-smi",
        "--query-gpu=index,uuid,memory.total,memory.used,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    if device_ids:
        command.insert(1, f"--id={','.join(device_ids)}")
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=float(os.getenv("EXECUTION_GPU_PROBE_TIMEOUT_SECONDS", "2")),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    snapshots: list[GpuSnapshot] = []
    for line in result.stdout.splitlines():
        fields = [item.strip() for item in line.split(",")]
        if len(fields) < 5:
            continue
        physical_index = fields[0]
        if device_ids and physical_index not in device_ids:
            continue
        snapshots.append(
            GpuSnapshot(
                physical_index=physical_index,
                device_uuid=fields[1] or None,
                memory_total_mb=_parse_number(fields[2]),
                memory_used_mb=_parse_number(fields[3]),
                utilization_gpu=_parse_number(fields[4]),
                source="nvidia-smi",
            )
        )
    return snapshots


class GpuInventory:
    """Low-dependency GPU probe with a short cache to avoid per-task shell calls."""

    def __init__(self, cache_seconds: float | None = None):
        self.cache_seconds = max(
            0.0,
            float(cache_seconds if cache_seconds is not None else os.getenv("EXECUTION_GPU_PROBE_CACHE_SECONDS", "1")),
        )
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached: list[GpuSnapshot] = []

    def snapshots(self, *, force: bool = False) -> list[GpuSnapshot]:
        now = time.monotonic()
        with self._lock:
            if not force and self._cached and now - self._cached_at < self.cache_seconds:
                return list(self._cached)
            ids = configured_gpu_ids()
            snapshots = _query_nvidia_smi(ids)
            seen = {item.physical_index for item in snapshots}
            # A configured device list still produces a useful allocation record
            # in development containers without nvidia-smi or NVML.
            for physical_index in ids:
                if physical_index in seen:
                    continue
                snapshots.append(
                    GpuSnapshot(
                        physical_index=physical_index,
                        device_uuid=None,
                        memory_total_mb=_parse_number(os.getenv("EXECUTION_GPU_MEMORY_TOTAL_MB")),
                        memory_used_mb=0,
                        utilization_gpu=0,
                        source="configuration",
                    )
                )
            self._cached = snapshots
            self._cached_at = now
            return list(snapshots)


class ResourceScheduler:
    """Small GPU capacity controller used by workers until a cluster controller is deployed.

    Redis leases are advisory capacity reservations. NVML/nvidia-smi remains the
    runtime health signal, while the configured memory budget prevents several
    tasks from overcommitting a GPU between two probes.
    """

    _local_lock = threading.Lock()
    _local_leases: dict[str, dict[str, tuple[int, float]]] = {}
    _local_workers: dict[str, dict[str, Any]] = {}

    def __init__(self, store: Any = None, *, worker_id: str | None = None, inventory: GpuInventory | None = None):
        self.store = store
        self.worker_id = worker_id or os.getenv("EXECUTION_WORKER_ID", "gateway-inline")
        self.inventory = inventory or GpuInventory()
        self.lease_ttl_seconds = max(5, int(os.getenv("EXECUTION_GPU_LEASE_TTL_SECONDS", "900")))
        self.prefix = getattr(store, "prefix", os.getenv("EXECUTION_KEY_PREFIX", "qasystem:execution"))

    @property
    def redis(self):
        return getattr(self.store, "redis", None)

    def enabled_for(self, resource_class: str) -> bool:
        if not str(resource_class).startswith("gpu"):
            return False
        return os.getenv("EXECUTION_GPU_SCHEDULING", "true").strip().lower() not in {"0", "false", "off", "no"}

    @staticmethod
    def _memory_request(resource_class: str) -> int:
        suffix = str(resource_class).replace(".", "_").replace("-", "_").upper()
        value = os.getenv(f"EXECUTION_GPU_MEMORY_REQUEST_MB_{suffix}")
        value = value or os.getenv("EXECUTION_GPU_MEMORY_REQUEST_MB", "0")
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    def _lease_key(self, device: GpuSnapshot) -> str:
        identity = device.device_uuid or device.physical_index
        return f"{self.prefix}:gpu-leases:{identity}"

    def _worker_key(self, worker_id: str) -> str:
        return f"{self.prefix}:workers:{worker_id}"

    def register_worker(
        self,
        *,
        worker_id: str,
        resource_classes: tuple[str, ...] | list[str],
        gpu_devices: list[str],
        snapshots: list[GpuSnapshot] | None = None,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "worker_id": worker_id,
            "worker_instance_id": os.getenv("EXECUTION_WORKER_INSTANCE_ID"),
            "runtime": os.getenv("HOSTNAME", socket.gethostname()),
            "resource_classes": list(resource_classes),
            "gpu_devices": list(gpu_devices),
            "snapshots": [snapshot.as_dict() for snapshot in (snapshots or [])],
            "endpoint": endpoint,
            "host_id": socket.gethostname(),
            "updated_at": time.time(),
        }
        ttl = max(self.lease_ttl_seconds, int(os.getenv("EXECUTION_WORKER_HEARTBEAT_TTL_SECONDS", "30")))
        if self.redis is not None:
            self.redis.set(self._worker_key(worker_id), json.dumps(record, ensure_ascii=False, separators=(",", ":")), ex=ttl)
        else:
            with self._local_lock:
                self._local_workers[worker_id] = record
        return record

    def unregister_worker(self, worker_id: str) -> None:
        if self.redis is not None:
            self.redis.delete(self._worker_key(worker_id))
        else:
            with self._local_lock:
                self._local_workers.pop(worker_id, None)

    def list_workers(self, resource_class: str | None = None) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if self.redis is not None:
            for key in self.redis.scan_iter(match=f"{self.prefix}:workers:*"):
                raw = self.redis.get(key)
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                records.append(record)
        else:
            with self._local_lock:
                now = time.time()
                ttl = max(5, int(os.getenv("EXECUTION_WORKER_HEARTBEAT_TTL_SECONDS", "30")))
                for worker_id, item in list(self._local_workers.items()):
                    if now - float(item.get("updated_at", 0)) > ttl:
                        self._local_workers.pop(worker_id, None)
                        continue
                    records.append(dict(item))
        if resource_class:
            records = [item for item in records if resource_class in (item.get("resource_classes") or [])]
        return records

    @staticmethod
    def _snapshot_from_worker(worker: dict[str, Any], physical_index: str) -> GpuSnapshot:
        snapshots = worker.get("snapshots") or []
        selected = snapshots[0] if snapshots else {}
        for item in snapshots:
            if str(item.get("physical_index")) == physical_index:
                selected = item
                break
        return GpuSnapshot(
            physical_index=physical_index,
            device_uuid=selected.get("device_uuid"),
            memory_total_mb=selected.get("memory_total_mb"),
            memory_used_mb=selected.get("memory_used_mb"),
            utilization_gpu=selected.get("utilization_gpu"),
            source="worker-heartbeat",
        )

    def _active_lease_stats(self, device: GpuSnapshot) -> tuple[int, int]:
        now = time.time()
        if self.redis is not None:
            try:
                values = self.redis.hvals(self._lease_key(device))
            except Exception:
                return 0, 0
            active: list[int] = []
            for raw in values:
                try:
                    budget, expiry = str(raw).split("|", 1)
                    if float(expiry) > now:
                        active.append(max(0, int(budget)))
                except (TypeError, ValueError):
                    continue
            return len(active), sum(active)
        with self._local_lock:
            leases = self._local_leases.get(self._lease_key(device), {})
            active = [budget for budget, expiry in leases.values() if expiry > now]
        return len(active), sum(active)

    def reserve_worker(
        self,
        resource_class: str,
        *,
        memory_request_mb: int | None = None,
        exclude_workers: set[str] | None = None,
        exclude_devices: set[str] | None = None,
    ) -> dict[str, Any] | None:
        excluded = exclude_workers or set()
        excluded_devices = exclude_devices or set()
        workers = [item for item in self.list_workers(resource_class) if item.get("worker_id") not in excluded]
        candidates: list[tuple[tuple[int, int, int, str], dict[str, Any], GpuSnapshot]] = []
        for worker in workers:
            devices = [str(item) for item in (worker.get("gpu_devices") or [])]
            if not devices:
                continue
            for physical_index in devices:
                if physical_index in excluded_devices:
                    continue
                snapshot = self._snapshot_from_worker(worker, physical_index)
                lease_count, leased_mb = self._active_lease_stats(snapshot)
                effective_free_mb = max(0, (snapshot.memory_free_mb or 0) - leased_mb)
                candidates.append(
                    (
                        (
                            -effective_free_mb,
                            snapshot.utilization_gpu if snapshot.utilization_gpu is not None else 0,
                            lease_count,
                            str(worker.get("worker_id")),
                        ),
                        worker,
                        snapshot,
                    )
                )
        candidates.sort(key=lambda item: item[0])
        request_mb = self._memory_request(resource_class) if memory_request_mb is None else max(0, memory_request_mb)
        for _score, worker, snapshot in candidates:
            if snapshot.memory_free_mb is not None and snapshot.memory_free_mb < request_mb:
                continue
            lease_id = uuid.uuid4().hex
            if not self._reserve(snapshot, lease_id, request_mb):
                continue
            lease = GpuLease(
                lease_id=lease_id,
                device=snapshot,
                memory_budget_mb=request_mb,
                owner=f"{self.worker_id}:{os.getpid()}",
                expires_at=time.time() + self.lease_ttl_seconds,
            )
            allocation = {
                "scheduler_id": os.getenv("EXECUTION_SCHEDULER_ID", "gpu-capacity-local"),
                "worker_id": worker.get("worker_id"),
                "worker_instance_id": worker.get("worker_instance_id"),
                "runtime": worker.get("runtime"),
                "host_id": worker.get("host_id"),
                "model": "mineru-vlm" if resource_class == "gpu.vlm" else None,
                "resource_class": resource_class,
                "endpoint": worker.get("endpoint"),
                "gpu_allocations": [lease.as_dict()],
            }
            return {"worker": worker, "lease": lease, "allocation": allocation}
        return None

    def _reserve(self, device: GpuSnapshot, lease_id: str, budget_mb: int) -> bool:
        capacity_mb = device.memory_free_mb if device.memory_free_mb is not None else (device.memory_total_mb or 0)
        try:
            capacity_mb = max(0, capacity_mb - int(os.getenv("EXECUTION_GPU_SAFETY_MARGIN_MB", "512")))
        except (TypeError, ValueError):
            capacity_mb = max(0, capacity_mb)
        try:
            default_max = "16" if budget_mb > 0 else "1"
            max_leases = max(1, int(os.getenv("EXECUTION_GPU_MAX_LEASES_PER_DEVICE", default_max)))
        except (TypeError, ValueError):
            max_leases = 1
        redis = self.redis
        if redis is not None:
            script = """
            local entries = redis.call('hgetall', KEYS[1])
            local active = 0
            local count = 0
            local now = tonumber(ARGV[1])
            for index = 1, #entries, 2 do
                local field = entries[index]
                local raw = entries[index + 1]
                local expiry = tonumber(string.match(raw, '^[^|]+|([^|]+)')) or 0
                if expiry <= now then
                    redis.call('hdel', KEYS[1], field)
                else
                    active = active + (tonumber(string.match(raw, '^([^|]+)')) or 0)
                    count = count + 1
                end
            end
            local budget = tonumber(ARGV[2]) or 0
            local capacity = tonumber(ARGV[3]) or 0
            local max_leases = tonumber(ARGV[4]) or 1
            if count >= max_leases or (capacity > 0 and active + budget > capacity) then
                return 0
            end
            redis.call('hset', KEYS[1], ARGV[5], tostring(budget) .. '|' .. tostring(now + tonumber(ARGV[6])))
            redis.call('expire', KEYS[1], tonumber(ARGV[6]))
            return 1
            """
            return bool(
                redis.eval(
                    script,
                    1,
                    self._lease_key(device),
                    time.time(),
                    budget_mb,
                    capacity_mb,
                    max_leases,
                    lease_id,
                    self.lease_ttl_seconds,
                )
            )
        with self._local_lock:
            key = self._lease_key(device)
            leases = self._local_leases.setdefault(key, {})
            now = time.time()
            for old_id, (_old_budget, expiry) in list(leases.items()):
                if expiry <= now:
                    leases.pop(old_id, None)
            active = sum(item[0] for item in leases.values())
            if len(leases) >= max_leases or (capacity_mb > 0 and active + budget_mb > capacity_mb):
                if not leases:
                    self._local_leases.pop(key, None)
                return False
            leases[lease_id] = (budget_mb, now + self.lease_ttl_seconds)
            return True

    def release(self, lease: GpuLease | dict[str, Any] | None) -> bool:
        if not lease:
            return False
        lease_id = lease.lease_id if isinstance(lease, GpuLease) else lease.get("lease_id")
        physical_index = lease.device.physical_index if isinstance(lease, GpuLease) else str(lease.get("physical_index", ""))
        device_uuid = lease.device.device_uuid if isinstance(lease, GpuLease) else lease.get("device_uuid")
        if not lease_id or not physical_index:
            return False
        device = GpuSnapshot(physical_index, device_uuid, None, None, None, "release")
        redis = self.redis
        if redis is not None:
            script = "if redis.call('hexists', KEYS[1], ARGV[1]) == 1 then return redis.call('hdel', KEYS[1], ARGV[1]) else return 0 end"
            try:
                return bool(redis.eval(script, 1, self._lease_key(device), lease_id))
            except Exception:
                return False
        with self._local_lock:
            key = self._lease_key(device)
            leases = self._local_leases.get(key) or {}
            if lease_id not in leases:
                return False
            del leases[lease_id]
            if not leases:
                self._local_leases.pop(key, None)
            return True

    def acquire(
        self,
        resource_class: str,
        *,
        gpu_count: int = 1,
        memory_request_mb: int | None = None,
        preferred_device: str | None = None,
    ) -> list[GpuLease]:
        if not self.enabled_for(resource_class):
            return []
        request_mb = self._memory_request(resource_class) if memory_request_mb is None else max(0, memory_request_mb)
        candidates = self.inventory.snapshots(force=True)
        candidates.sort(
            key=lambda item: (
                0 if preferred_device and item.physical_index == preferred_device else 1,
                item.memory_free_mb is not None and item.memory_free_mb < request_mb,
                -(item.memory_free_mb if item.memory_free_mb is not None else 0),
                item.utilization_gpu if item.utilization_gpu is not None else 0,
            )
        )
        owner = f"{self.worker_id}:{os.getpid()}"
        acquired: list[GpuLease] = []
        for device in candidates:
            if len(acquired) >= max(1, gpu_count):
                break
            if device.memory_free_mb is not None and device.memory_free_mb < request_mb:
                continue
            lease_id = uuid.uuid4().hex
            if not self._reserve(device, lease_id, request_mb):
                continue
            acquired.append(
                GpuLease(
                    lease_id=lease_id,
                    device=device,
                    memory_budget_mb=request_mb,
                    owner=owner,
                    expires_at=time.time() + self.lease_ttl_seconds,
                )
            )
        if len(acquired) != max(1, gpu_count):
            for lease in acquired:
                self.release(lease)
            raise ResourceLeaseUnavailable(
                f"GPU capacity unavailable for {resource_class}: requested={gpu_count}, acquired={len(acquired)}"
            )
        return acquired

    def describe(self) -> dict[str, Any]:
        snapshots = self.inventory.snapshots(force=True)
        devices: list[dict[str, Any]] = []
        redis = self.redis
        for snapshot in snapshots:
            lease_key = self._lease_key(snapshot)
            ttl_seconds = None
            leases: list[dict[str, Any]] = []
            if redis is not None:
                try:
                    raw_leases = redis.hgetall(lease_key)
                    ttl_seconds = redis.ttl(lease_key)
                    leases = [
                        {
                            "lease_id": str(key),
                            "memory_budget_mb": _parse_number(str(value).split("|", 1)[0]),
                        }
                        for key, value in raw_leases.items()
                    ]
                except Exception:
                    leases = []
            else:
                with self._local_lock:
                    leases = [
                        {"lease_id": key, "memory_budget_mb": value[0]}
                        for key, value in self._local_leases.get(lease_key, {}).items()
                    ]
            devices.append(
                {
                    "physical_index": snapshot.physical_index,
                    "device_uuid": snapshot.device_uuid,
                    "memory_total_mb": snapshot.memory_total_mb,
                    "memory_used_mb": snapshot.memory_used_mb,
                    "memory_free_mb": snapshot.memory_free_mb,
                    "utilization_gpu": snapshot.utilization_gpu,
                    "snapshot_source": snapshot.source,
                    "lease_count": len(leases),
                    "leases": leases,
                    "lease_ttl_seconds": ttl_seconds,
                }
            )
        return {
            "scheduler_id": os.getenv("EXECUTION_SCHEDULER_ID", "gpu-capacity-local"),
            "devices": devices,
            "workers": self.list_workers(),
            "observed_at": time.time(),
        }


__all__ = [
    "GpuInventory",
    "GpuLease",
    "GpuSnapshot",
    "ResourceLeaseUnavailable",
    "ResourceScheduler",
    "configured_gpu_ids",
    "parse_device_ids",
]
