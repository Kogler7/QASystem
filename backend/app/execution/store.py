from __future__ import annotations
from docker.configuration import getenv as config_getenv

import json
import os
import threading
import uuid
from copy import deepcopy
from typing import Any

from .models import iso_now


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode(value: str | bytes | None) -> Any:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        # Redis records created by older builds used Python's scalar string
        # representation, so keep boolean/null cancellation flags typed.
        if value == "True":
            return True
        if value == "False":
            return False
        if value == "None":
            return None
        return value


class MemoryExecutionStore:
    """Thread-safe store for explicit inline tests and local diagnostics."""

    def __init__(self):
        self._lock = threading.RLock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.executions: dict[str, list[dict[str, Any]]] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.settings: dict[str, dict[str, Any]] = {}
        self.auth_contexts: dict[str, dict[str, Any]] = {}

    def create_job(self, **fields: Any) -> dict[str, Any]:
        with self._lock:
            job_id = fields.pop("job_id", None) or uuid.uuid4().hex
            now = iso_now()
            job = {
                "job_id": job_id,
                "status": "accepted",
                "stage": "accepted",
                "created_at": now,
                "updated_at": now,
                "latest_event_id": None,
                **deepcopy(fields),
            }
            self.jobs[job_id] = job
            self.executions[job_id] = []
            self.events[job_id] = []
            return deepcopy(job)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self.jobs.get(job_id)
            return deepcopy(value) if value else None

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            job.update(deepcopy(fields))
            job["updated_at"] = iso_now()
            return deepcopy(job)

    def create_execution(self, job_id: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            record = {"execution_id": uuid.uuid4().hex, **deepcopy(fields)}
            self.executions.setdefault(job_id, []).append(record)
            return deepcopy(record)

    def update_execution(self, job_id: str, execution_id: str, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            for record in self.executions.get(job_id, []):
                if record.get("execution_id") == execution_id:
                    record.update(deepcopy(fields))
                    return deepcopy(record)
        return None

    def list_executions(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self.executions.get(job_id, []))

    def append_event(self, job_id: str, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            sequence = len(self.events.setdefault(job_id, [])) + 1
            result = {**deepcopy(event), "event_id": f"{job_id}:{sequence}", "sequence": sequence}
            self.events[job_id].append(result)
            job = self.jobs.get(job_id)
            if job:
                job["latest_event_id"] = result["event_id"]
                job["updated_at"] = result.get("timestamp") or iso_now()
            return deepcopy(result)

    def list_events(self, job_id: str, after_sequence: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(
                [
                    item
                    for item in self.events.get(job_id, [])
                    if int(item.get("sequence", 0)) > after_sequence
                ][:limit]
            )

    def set_setting(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self.settings[key] = deepcopy(value)

    def get_settings(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return deepcopy(self.settings)

    def save_auth_context(self, job_id: str, headers: dict[str, str]) -> str:
        auth_context_id = uuid.uuid4().hex
        with self._lock:
            self.auth_contexts[auth_context_id] = {
                "job_id": job_id,
                "headers": deepcopy(headers),
            }
        return auth_context_id

    def load_auth_context(self, auth_context_id: str, job_id: str) -> dict[str, str]:
        with self._lock:
            context = self.auth_contexts.get(auth_context_id) or {}
            if context.get("job_id") != job_id:
                return {}
            return deepcopy(context.get("headers") or {})

    def delete_auth_context(self, auth_context_id: str | None) -> None:
        if not auth_context_id:
            return
        with self._lock:
            self.auth_contexts.pop(auth_context_id, None)


class RedisExecutionStore:
    def __init__(self, url: str, prefix: str | None = None):
        import redis

        self.redis = redis.Redis.from_url(
            url,
            decode_responses=True,
            health_check_interval=30,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
        self.prefix = prefix or config_getenv("EXECUTION_KEY_PREFIX", "qasystem:execution")
        self.event_maxlen = int(config_getenv("EXECUTION_EVENT_MAXLEN", "10000"))

    def _job_key(self, job_id: str) -> str:
        return f"{self.prefix}:job:{job_id}"

    def _event_key(self, job_id: str) -> str:
        return f"{self.prefix}:events:{job_id}"

    def _execution_key(self, job_id: str) -> str:
        return f"{self.prefix}:executions:{job_id}"

    def _setting_key(self, key: str) -> str:
        return f"{self.prefix}:setting:{key}"

    def _auth_context_key(self, auth_context_id: str) -> str:
        return f"{self.prefix}:auth:{auth_context_id}"

    def create_job(self, **fields: Any) -> dict[str, Any]:
        job_id = fields.pop("job_id", None) or uuid.uuid4().hex
        now = iso_now()
        job = {
            "job_id": job_id,
            "status": "accepted",
            "stage": "accepted",
            "created_at": now,
            "updated_at": now,
            "latest_event_id": None,
            **fields,
        }
        encoded = {key: _json(value) for key, value in job.items()}
        self.redis.hset(self._job_key(job_id), mapping=encoded)
        self.redis.expire(self._job_key(job_id), int(config_getenv("EXECUTION_JOB_TTL", "604800")))
        return job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        raw = self.redis.hgetall(self._job_key(job_id))
        if not raw:
            return None
        result: dict[str, Any] = {}
        for key, value in raw.items():
            decoded = _decode(value)
            result[key] = decoded
        return result

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        if not self.redis.exists(self._job_key(job_id)):
            return None
        fields["updated_at"] = iso_now()
        encoded = {key: _json(value) for key, value in fields.items()}
        self.redis.hset(self._job_key(job_id), mapping=encoded)
        return self.get_job(job_id)

    def create_execution(self, job_id: str, **fields: Any) -> dict[str, Any]:
        record = {"execution_id": uuid.uuid4().hex, **fields}
        self.redis.rpush(self._execution_key(job_id), _json(record))
        self.redis.expire(self._execution_key(job_id), int(config_getenv("EXECUTION_JOB_TTL", "604800")))
        return record

    def update_execution(self, job_id: str, execution_id: str, **fields: Any) -> dict[str, Any] | None:
        key = self._execution_key(job_id)
        records = self.redis.lrange(key, 0, -1)
        for index, raw in enumerate(records):
            record = _decode(raw)
            if record.get("execution_id") != execution_id:
                continue
            record.update(fields)
            self.redis.lset(key, index, _json(record))
            return record
        return None

    def list_executions(self, job_id: str) -> list[dict[str, Any]]:
        return [_decode(raw) for raw in self.redis.lrange(self._execution_key(job_id), 0, -1)]

    def append_event(self, job_id: str, event: dict[str, Any]) -> dict[str, Any]:
        sequence = int(self.redis.incr(f"{self.prefix}:sequence:{job_id}"))
        result = {**event, "event_id": f"{job_id}:{sequence}", "sequence": sequence}
        event_id = self.redis.xadd(
            self._event_key(job_id),
            {"payload": _json(result)},
            maxlen=self.event_maxlen,
            approximate=True,
        )
        result["stream_id"] = event_id
        self.redis.hset(self._job_key(job_id), mapping={"latest_event_id": result["event_id"], "updated_at": result.get("timestamp", iso_now())})
        return result

    def list_events(self, job_id: str, after_sequence: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        entries = self.redis.xrange(self._event_key(job_id), count=max(limit * 4, limit))
        result = []
        for _stream_id, values in entries:
            event = _decode(values.get("payload"))
            if int(event.get("sequence", 0)) > after_sequence:
                result.append(event)
                if len(result) >= limit:
                    break
        return result

    def set_setting(self, key: str, value: dict[str, Any]) -> None:
        self.redis.set(self._setting_key(key), _json(value), ex=int(value.get("ttl_seconds", 3600)))

    def get_settings(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for key in self.redis.scan_iter(match=f"{self.prefix}:setting:*"):
            name = key.rsplit(":", 1)[-1]
            value = _decode(self.redis.get(key))
            if isinstance(value, dict):
                result[name] = value
        return result

    def save_auth_context(self, job_id: str, headers: dict[str, str]) -> str:
        auth_context_id = uuid.uuid4().hex
        self.redis.set(
            self._auth_context_key(auth_context_id),
            _json({"job_id": job_id, "headers": headers}),
            ex=int(config_getenv("EXECUTION_AUTH_CONTEXT_TTL_SECONDS", "3600")),
        )
        return auth_context_id

    def load_auth_context(self, auth_context_id: str, job_id: str) -> dict[str, str]:
        context = _decode(self.redis.get(self._auth_context_key(auth_context_id))) or {}
        if context.get("job_id") != job_id:
            return {}
        return context.get("headers") or {}

    def delete_auth_context(self, auth_context_id: str | None) -> None:
        if auth_context_id:
            self.redis.delete(self._auth_context_key(auth_context_id))


_MEMORY_STORE = MemoryExecutionStore()
_STORE_LOCK = threading.Lock()
_STORE: MemoryExecutionStore | RedisExecutionStore | None = None
_STORE_KEY: str | None = None


def get_execution_store() -> MemoryExecutionStore | RedisExecutionStore:
    global _STORE, _STORE_KEY
    url = config_getenv("EXECUTION_REDIS_URL", "").strip()
    key = url or "memory"
    with _STORE_LOCK:
        if _STORE is not None and _STORE_KEY == key:
            return _STORE
        if url:
            _STORE = RedisExecutionStore(url)
        else:
            _STORE = _MEMORY_STORE
        _STORE_KEY = key
        return _STORE
