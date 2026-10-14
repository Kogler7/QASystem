"""Local configuration versions and lifecycle events; no network or approvals."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import threading
import uuid
import sys


def private_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def append_event(directory, event):
    directory = Path(directory)
    events = directory / 'events'
    events.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = {'schema': 1, 'time': datetime.now(timezone.utc).isoformat(), **event}
    # One immutable JSON record per event avoids concurrent read/modify/write loss.
    target = events / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '-' + uuid.uuid4().hex + '.json')
    private_write(target, json.dumps(data, ensure_ascii=False, indent=2).encode())
    return target


def record_approval(directory, approved, *, root=None):
    directory = Path(directory)
    version = directory / 'versions' / f"r{approved['revision']:06d}"
    # Existing revisions cannot be overwritten, including after a failed publication.
    version.mkdir(parents=True, mode=0o700)
    source = (Path(root) if root is not None else directory.parent) / '.env'
    if source.is_file():
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != approved['snapshot']['files']['.env']:
            raise ValueError('配置归档时环境文件变化；请重新审核')
        private_write(version / '.env', raw)
    metadata = {'schema': 1, 'revision': approved['revision'], 'state': 'approved',
                'release': approved['snapshot']['values'].get('declarations.version', {}).get('value'),
                'source_commit': approved['snapshot']['values'].get('release.source_commit', {}).get('value'),
                'time': approved['time'], 'files': approved['snapshot']['files'],
                'values': approved['snapshot']['values'], 'actor': approved.get('actor', 'human'),
                'reviewer': approved.get('reviewer'), 'reason': approved.get('reason'), 'audit': approved.get('audit', {})}
    private_write(version / 'environment.json', json.dumps(metadata, ensure_ascii=False, indent=2).encode())
    for name in ('config.json', 'models.compose.json'):
        source = directory / name
        if source.is_file():
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != approved['snapshot']['files'][name]:
                raise ValueError('配置归档时文件变化；请重新审核')
            private_write(version / name, raw)
    (directory / 'observed' / 'events').mkdir(parents=True, exist_ok=True, mode=0o700)
    append_event(directory, {'event': 'approved', 'revision': approved['revision'], 'version': version.name})


_seen = set()
_lock = threading.Lock()


def observe_read(key, value, *, directory, revision, source):
    from docker.config_guard import safe_value
    encoded = {'value': None} if value is None else safe_value(key, str(value))
    identity = (str(directory), revision, key, json.dumps(encoded, sort_keys=True), source)
    with _lock:
        if identity in _seen:
            return
        event = append_event(directory, {'event': 'read', 'approved_revision': revision, 'pid': os.getpid(),
                                 'service_instance': os.getenv('HOSTNAME', ''),
                                 'actor': 'ops-probe' if '_probe' in sys.argv else 'application',
                                 'key': key, 'source': source, **encoded})
        # The host owns the 0700 parent created at approval. Docker may run as
        # root; redacted event files must remain readable to that host owner.
        event.chmod(0o644)
        _seen.add(identity)


def database(directory):
    directory = Path(directory)
    versions = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((directory / 'versions').glob('r*/environment.json'))]
    by_revision = {version['revision']: version for version in versions}
    for path in sorted((directory / 'history').glob('*.json')):
        record = json.loads(path.read_text(encoding='utf-8'))
        fingerprint = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:12]
        if path.name != f"{record['revision']:06d}-{fingerprint}.json":
            raise ValueError('历史配置记录指纹不一致；拒绝生成数据库视图')
        revision = record['revision']
        if revision in by_revision:
            version = by_revision[revision]
            if version['files'] != record['snapshot']['files'] or version['values'] != record['snapshot']['values']:
                raise ValueError('版本归档与审核历史不一致')
            for name in ('.env', 'config.json', 'models.compose.json'):
                archived = directory / 'versions' / f'r{revision:06d}' / name
                if archived.is_file() and hashlib.sha256(archived.read_bytes()).hexdigest() != version['files'][name]:
                    raise ValueError('历史配置归档文件指纹不一致')
        else:
            values = record['snapshot']['values']
            by_revision[revision] = {'schema': 1, 'revision': revision, 'state': 'approved',
                'time': record['time'], 'release': values.get('declarations.version', {}).get('value'),
                'source_commit': values.get('release.source_commit', {}).get('value'),
                'files': record['snapshot']['files'], 'values': values, 'archive_available': False}
    versions = [by_revision[key] for key in sorted(by_revision)]
    events = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((directory / 'events').glob('*.json'))]
    observations = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((directory / 'observed/events').glob('*.json'))]
    data = {'schema': 1, 'versions': versions, 'events': events, 'observations': observations}
    # A disposable database view; immutable rows above remain authoritative.
    private_write(directory / 'database.json', json.dumps(data, ensure_ascii=False, indent=2).encode())
    return data
