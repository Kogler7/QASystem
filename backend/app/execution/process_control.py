"""Private supervisor control. Registered factories only; no shell or arbitrary PID."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import os
import re
from pathlib import Path
import socket
import threading
import time

CONTROL_SOCKET_PATH = Path('/tmp/qasystem-process-control/control.sock')


@dataclass
class ManagedProcess:
    name: str
    process: object
    draining: bool = False


class ProcessController:
    def __init__(self, *, authorize, drain_timeout=300, on_event=None):
        self.authorize = authorize
        self.drain_timeout = drain_timeout
        self.roles = {}
        self.commands = OrderedDict()
        self.lock = threading.RLock()
        self.closing = False
        self.on_event = on_event or (lambda event: None)

    def register(self, role, factory, count):
        with self.lock:
            if role in self.roles:
                raise ValueError('role already registered')
            self.roles[role] = dict(factory=factory, children=[], desired=count, busy=False,
                                   planned=count, planned_revision=None, applied_revision=None)
            self._grow(role, count)

    def _grow(self, role, count):
        record = self.roles[role]
        while len(record['children']) < count:
            if self.closing:
                raise RuntimeError('supervisor shutting down')
            used = {child.name for child in record['children']}
            index = next(i for i in range(32) if f'{role}-{i}' not in used)
            name = f'{role}-{index}'
            record['children'].append(ManagedProcess(name, record['factory'](name)))

    def status(self):
        with self.lock:
            if self.closing:
                raise ValueError('supervisor shutting down')
            return {role: dict(desired=record['desired'], busy=record['busy'],
                planned=record['planned'], planned_revision=record['planned_revision'],
                applied_revision=record['applied_revision'],
                processes=[dict(name=c.name, pid=c.process.pid,
                    state='failed' if c.process.poll() is not None else 'draining' if c.draining else 'running')
                    for c in record['children']]) for role, record in self.roles.items()}

    def request(self, message):
        if not isinstance(message, dict) or set(message) - {'action', 'role', 'request_id', 'revision'}:
            raise ValueError('invalid control request')
        action = message.get('action')
        if action == 'status':
            with self.lock:
                return dict(roles=self.status(), operations=[v['result'].copy() for v in self.commands.values()][-20:])
        if action not in {'drain', 'restart', 'apply'}:
            raise ValueError('unsupported control action')
        role = message.get('role')
        request_id = message.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request_id):
            raise ValueError('request_id required')
        if type(message.get('revision')) is not int or message['revision'] < 1:
            raise ValueError('approval revision required')
        fingerprint = json.dumps(message, sort_keys=True)
        with self.lock:
            if self.closing:
                raise ValueError('supervisor shutting down')
            if role not in self.roles:
                raise ValueError('unregistered role')
            # Even a cached command must refer to the currently approved version.
            target = self.authorize(role, message.get('revision'))
            if type(target) is not int or not 1 <= target <= 32:
                raise ValueError('approved worker count invalid')
            if request_id in self.commands:
                command = self.commands[request_id]
                if command['fingerprint'] != fingerprint:
                    raise ValueError('request_id reused for different operation')
                return dict(command=command['result'].copy())
            if self.roles[role]['busy']:
                raise ValueError('role operation already in progress')
            result = dict(request_id=request_id, role=role, action=action, state='accepted', revision=message.get('revision'), planned_workers=target)
            self.commands[request_id] = dict(fingerprint=fingerprint, result=result)
            while len(self.commands) > 256:
                key = next((k for k, v in self.commands.items() if v['result']['state'] not in {'accepted', 'running'}), None)
                if key is None:
                    break
                del self.commands[key]
            self.roles[role]['busy'] = True
            threading.Thread(target=self._operate, args=(role, action, target, result), daemon=True).start()
            return dict(command=result.copy())

    def _drain(self, child):
        child.draining = True
        if child.process.poll() is None:
            child.process.terminate()  # worker's SIGTERM stops consuming, completes current attempt
        child.process.wait(timeout=self.drain_timeout)

    def _operate(self, role, action, target, result):
        record = self.roles[role]
        result['state'] = 'running'
        try:
            self.on_event(result.copy())
            record['planned'] = target
            record['planned_revision'] = result['revision']
            record['desired'] = 0 if action == 'drain' else target
            if action in {'drain', 'restart'}:
                for child in list(record['children']):
                    self._drain(child)
                    with self.lock:
                        record['children'].remove(child)
                        if action == 'restart':
                            # Start replacement only after the old process actually exits.
                            if self.closing:
                                raise RuntimeError('supervisor shutting down')
                            record['children'].append(ManagedProcess(child.name, record['factory'](child.name)))
            if action != 'drain':
                for child in list(record['children'])[target:]:
                    self._drain(child)
                    with self.lock:
                        record['children'].remove(child)
                with self.lock:
                    for child in list(record['children']):
                        if child.process.poll() is not None:
                            record['children'].remove(child)
                    self._grow(role, target)
            result['state'] = 'applied'
            record['applied_revision'] = result['revision']
            self.on_event({**result, 'processes': self.status()[role]['processes']})
        except Exception:
            # Never kill in-flight work or spawn a second process after a drain timeout.
            result['state'] = 'failed'
            result['error'] = 'drain or replacement failed; inspect runtime status'
            try:
                self.on_event(result.copy())
            except Exception:
                result['error'] = 'process operation or audit failed; inspect runtime state and local records'
        finally:
            with self.lock:
                record['busy'] = False

    def begin_shutdown(self):
        """Freeze replacement creation before supervisor's container shutdown policy."""
        with self.lock:
            self.closing = True
            return [child.process for record in self.roles.values() for child in record['children']]


class ControlSocket:
    def __init__(self, path: Path, controller):
        self.path, self.controller = path, controller
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.closed = threading.Event()

    def start(self):
        import fcntl
        if self.path.parent.is_symlink():
            raise ValueError('control directory cannot be a symlink')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.parent.stat().st_uid != os.geteuid():
            raise ValueError('control directory belongs to another user')
        self.path.parent.chmod(0o700)
        if self.path.with_suffix('.lock').is_symlink() or self.path.is_symlink():
            raise ValueError('control paths cannot be symlinks')
        self.owner_lock = self.path.with_suffix('.lock').open('a+b')
        try:
            # Lock ownership survives threads and is released by the OS after a crash.
            fcntl.flock(self.owner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self.owner_lock.close()
            raise
        try:
            # A crash may leave a stale socket; only the unique lock holder removes it.
            self.path.unlink(missing_ok=True)
            self.socket.bind(str(self.path))
            self.path.chmod(0o600)
            self.socket.listen(8)
            self.socket.settimeout(0.2)
        except Exception:
            self.owner_lock.close()
            raise
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                connection.settimeout(2)
                try:
                    data = b''
                    while not data.endswith(b'\n'):
                        part = connection.recv(4097 - len(data))
                        if not part or len(data) + len(part) > 4096:
                            raise ValueError('invalid request size')
                        data += part
                    response = dict(ok=True, **self.controller.request(json.loads(data)))
                except Exception:
                    response = dict(ok=False, error='control request rejected')
                try:
                    connection.sendall(json.dumps(response).encode() + b'\n')
                except OSError:
                    pass

    def close(self):
        self.closed.set()
        self.socket.close()
        self.thread.join(timeout=3)
        self.path.unlink(missing_ok=True)
        self.owner_lock.close()


def control_request(path: Path, message):
    payload = json.dumps(message).encode() + b'\n'
    if len(payload) > 4096:
        raise ValueError('control request too large')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        connection.sendall(payload)
        data = b''
        while not data.endswith(b'\n'):
            part = connection.recv(65536)
            if not part or len(data) + len(part) > 1024 * 1024:
                raise ValueError('invalid supervisor response')
            data += part
    return json.loads(data)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Private Backend CPU worker control')
    parser.add_argument('action', choices=['status', 'drain', 'restart', 'apply'])
    parser.add_argument('--role', choices=['cpu'], default='cpu')
    parser.add_argument('--revision', type=int)
    parser.add_argument('--request-id')
    args = parser.parse_args(argv)
    message = dict(action=args.action)
    if args.action != 'status':
        if not args.revision or not args.request_id:
            parser.error('mutations require approval revision and request-id')
        message.update(role=args.role, revision=args.revision, request_id=args.request_id)
    try:
        response = control_request(CONTROL_SOCKET_PATH, message)
        print(json.dumps(response, ensure_ascii=False))
        return 0 if response.get('ok') and response.get('command', {}).get('state') != 'failed' else 2
    except (ValueError, OSError):
        print(json.dumps(dict(ok=False, error='supervisor control unavailable')))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
