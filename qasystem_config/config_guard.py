"""Deployment guard and local configuration revision records (stdlib only)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from functools import lru_cache
import subprocess

from qasystem_config.runtime_config import HOT_KEYS, read_document
from qasystem_config.io import read_env, atomic_write


def sensitive(key):
    return bool(re.search(r'KEY|SECRET|PASSWORD|TOKEN|AUTH|CREDENTIAL|HEADER|DATABASE_URL|REDIS_URL', key, re.I)) and not re.search(
        r'(MAX_(?:INPUT_|OUTPUT_)?TOKENS|BATCHED_TOKENS)$', key)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def safe_value(key, value):
    if sensitive(key) or re.search(r'[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s]+@', value):
        return {'value': '[凭据已隐藏]', 'fingerprint': digest(value.encode())}
    return {'value': value}


def source_version():
    source = Path(__file__).resolve().parents[1]
    marker = source / 'SOURCE_COMMIT'
    if marker.is_file():
        return marker.read_text(encoding='utf-8-sig').strip()
    if (source / '.git').exists():
        result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=source, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout.strip()
    return 'unknown'


def snapshot(root, directory, *, payloads=None):
    root, directory = Path(root), Path(directory)
    payloads = payloads or {}
    def content(path):
        if path in payloads:
            return payloads[path].encode('utf-8')
        return path.read_bytes() if path.is_file() else b''
    env_path = root / '.env'
    config_path = directory / 'config.json'
    template = root / 'deployment.env.example' if (root / 'deployment.env.example').is_file() else root / '.env.example'
    # The image mounts source at /app; the host UI can use its bundled catalog.
    if not template.is_file() and template not in payloads:
        template = Path(__file__).resolve().parents[1] / '.env.example'
    release = Path(__file__).resolve().parents[1] / '.env.example'
    if not release.is_file():
        release = template
    env = read_env(content(env_path).decode('utf-8-sig'))
    if config_path in payloads:
        document = json.loads(payloads[config_path])
    else:
        document = read_document(config_path)
    values = {'env.' + k: safe_value(k, v) for k, v in env.items()}
    try:
        displayed_directory = './' + directory.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        displayed_directory = str(directory.resolve())
    values['deployment.runtime_directory'] = {'value': displayed_directory}
    for key, value in document['settings'].items():
        values['settings.' + key] = {'value': value}
    for repo, model in document['models'].items():
        for key, value in model.items():
            values[f'models.{repo}.{key}'] = {'value': str(value)}
    for name, service in document.get('services', {}).items():
        for key, value in service.items():
            values[f'services.{name}.{key}'] = {'value': str(value)} if key == 'api_key_env' else safe_value(key, str(value))
    for name, limits in document.get('accounts', {}).items():
        for key, value in limits.items():
            values[f'accounts.{name}.{key}'] = {'value': str(value)}
    for purpose, service_id in document.get('routes', {}).items():
        values[f'routes.{purpose}'] = {'value': service_id}
    # Exact file hashes catch comments, quoting, duplicate assignments and catalog changes.
    files = {'.env': digest(content(env_path)), 'config.json': digest(content(config_path)),
             '.env.example': digest(content(template)),
             'release.env.example': digest(content(release)),
             'models.compose.json': digest(content(directory / 'models.compose.json'))}
    declaration = Path(__file__).with_name('config-declarations.json')
    files['config-declarations.json'] = digest(declaration.read_bytes()) if declaration.exists() else digest(b'')
    values['release.source_commit'] = {'value': source_version()}
    if declaration.exists():
        registry = json.loads(declaration.read_text(encoding='utf-8'))
        values['declarations.version'] = {'value': registry['version']}
        for key in ('aliases', 'model_defaults', 'model_descriptions', 'model_migrations', 'retired', 'fixed_contracts'):
            values['declarations.' + key] = {'value': json.dumps(registry.get(key), ensure_ascii=False, sort_keys=True)}
        for key, item in registry['items'].items():
            values['declarations.' + key] = {**safe_value(key, str(item['default'])),
                'description': item['desc'], 'mode': item['mode'], 'consumers': item['consumers'],
                'fallbacks': item.get('fallbacks', [])}
    catalog = read_env(content(template).decode('utf-8-sig'))
    comments, descriptions = [], {}
    for line in content(template).decode('utf-8-sig').splitlines():
        if line.startswith('#'):
            comments.append(line[1:].strip())
        elif '=' in line and line.split('=', 1)[0] in catalog:
            descriptions[line.split('=', 1)[0]] = ' / '.join(comments)
            comments = []
        elif not line.strip():
            comments = []
    for key, value in catalog.items():
        values['catalog.' + key] = {**safe_value(key, value),
                                  'description': descriptions.get(key, ''),
                                  'mode': 'hot' if key in HOT_KEYS else 'startup'}
    return {'files': files, 'values': values}


def approved(directory):
    path = Path(directory) / 'approved-config.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or data.get('schema') != 1
                or not isinstance(data.get('revision'), int) or data['revision'] < 1
                or not isinstance(data.get('snapshot'), dict)
                or not isinstance(data['snapshot'].get('values'), dict)
                or not isinstance(data['snapshot'].get('files'), dict)):
            raise ValueError
        return data
    except FileNotFoundError:
        return None
    except (OSError, ValueError, AttributeError):
        raise ValueError('配置批准记录损坏；请在 CLI UI 中重新审核') from None


def changes(previous, current):
    old = previous['snapshot']['values'] if previous else {}
    new = current['values']
    result = []
    for key in sorted(set(old) | set(new)):
        if old.get(key) == new.get(key):
            continue
        action = '新增' if key not in old else '删除' if key not in new else '修改'
        before = old.get(key, {}).get('value', '(无)')
        after = new.get(key, {}).get('value', '(无)')
        mode = '新请求热更' if key.startswith('settings.') else '配置清单升级' if key.startswith('catalog.') else '重新创建容器'
        result.append(f'[{action}] {key}: {before} → {after}  [{mode}]')
        if key.startswith(('catalog.', 'declarations.')) and old.get(key, {}).get('description') != new.get(key, {}).get('description'):
            result.append(f"  说明: {old.get(key, {}).get('description', '(无)')} → {new.get(key, {}).get('description', '(无)')}")
        if key.startswith(('catalog.', 'declarations.')) and old.get(key, {}).get('mode') != new.get(key, {}).get('mode'):
            result.append(f"  生效方式: {old.get(key, {}).get('mode', '(无)')} → {new.get(key, {}).get('mode', '(无)')}")
        if key.startswith('declarations.') and any(old.get(key, {}).get(field) != new.get(key, {}).get(field) for field in ('consumers', 'fallbacks')):
            result.append('  消费者或兼容回退发生变化，请核对当前版本声明')
    if previous:
        for name, value in current['files'].items():
            if previous['snapshot']['files'].get(name) != value:
                result.append(f'[文件变更] {name}（包括格式、注释和说明）')
    return result


def record(directory, current, *, reason, reviewer, root=None, actor='human', audit=None):
    """Publish history after the shared validated UI/automation transaction."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    previous = approved(directory)
    revision = int(previous.get('revision', 0)) + 1 if previous else 1
    data = {'schema': 1, 'revision': revision, 'time': datetime.now(timezone.utc).isoformat(),
            'reviewer': reviewer, 'reason': reason, 'previous': digest(json.dumps(previous, sort_keys=True).encode()) if previous else None,
            'changes': changes(previous, current), 'snapshot': current, 'actor': actor, 'audit': audit or {}}
    from qasystem_config.config_database import record_approval
    record_approval(directory, data, root=root)
    history = directory / 'history'
    history.mkdir(mode=0o700, exist_ok=True)
    target = history / f'{revision:06d}-{digest(json.dumps(data, sort_keys=True).encode())[:12]}.json'
    atomic_write(target, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    atomic_write(directory / 'approved-config.json', json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    return revision


@lru_cache(maxsize=8)
def _runtime_record(raw):
    try:
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get('schema') != 1 or
                not isinstance(data.get('revision'), int) or data['revision'] < 1 or
                not isinstance(data.get('snapshot'), dict) or
                not isinstance(data['snapshot'].get('files'), dict) or
                not isinstance(data['snapshot'].get('values'), dict)):
            raise ValueError
        return data
    except (ValueError, TypeError):
        raise ValueError('配置批准记录损坏；请在 CLI UI 中重新审核') from None


@lru_cache(maxsize=8)
def _history_name(raw):
    previous = _runtime_record(raw)
    return f"{int(previous['revision']):06d}-{digest(json.dumps(previous, sort_keys=True).encode())[:12]}.json"


@lru_cache(maxsize=8)
def _verified_bytes(raw, history_raw):
    previous = _runtime_record(raw)
    if json.loads(history_raw) != previous:
        raise ValueError('配置批准记录与历史记录不一致；禁止部署，请重新人工审核')
    return previous


def verified_record(directory):
    path = Path(directory) / 'approved-config.json'
    if not path.is_file():
        raise ValueError('配置尚未人工确认；请打开 CLI UI，审核差异并确认保存')
    raw = path.read_bytes()
    history = Path(directory) / 'history'
    filename = _history_name(raw)
    try:
        return _verified_bytes(raw, (history / filename).read_bytes())
    except (OSError, ValueError):
        raise ValueError('配置批准记录与历史记录不一致；禁止部署，请重新人工审核') from None


def require_runtime(directory, *, config_bytes=None):
    previous = verified_record(directory)
    declaration = Path(__file__).with_name('config-declarations.json')
    if declaration.exists() and digest(declaration.read_bytes()) != previous['snapshot']['files'].get('config-declarations.json'):
        raise ValueError('配置声明版本变化；请在 CLI UI 审核升级')
    for name in ('config.json', 'models.compose.json'):
        path = Path(directory) / name
        raw = config_bytes if name == 'config.json' and config_bytes is not None else path.read_bytes() if path.is_file() else None
        if raw is None or digest(raw) != previous['snapshot']['files'].get(name):
            raise ValueError('热配置与批准版本不一致；拒绝使用，请在 CLI UI 审核')
    return previous['revision']


def require(root, directory):
    from qasystem_config.config_contract import require_contract
    # Validate current source, including an external packaged source directory.
    source = Path(__file__).resolve().parents[1]
    errors = require_contract(source)
    if errors:
        raise ValueError('配置声明门禁失败：' + '；'.join(errors))
    previous = verified_record(directory)
    current = snapshot(root, directory)
    from qasystem_config.configuration import declarations
    retired = set(declarations().get('retired', []))
    if any('env.' + key in current['values'] for key in retired):
        raise ValueError('配置仍含已删除或迁移的参数；请通过 ops config 审核迁移后部署')
    if current != previous['snapshot']:
        raise ValueError('配置或版本清单与批准记录不一致；禁止部署，请回 CLI UI 审核变更')
    return previous['revision']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-dir', type=Path, default=Path.cwd())
    parser.add_argument('--runtime-dir', type=Path)
    parser.add_argument('--exec', dest='execute', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        root = args.project_dir.resolve()
        from qasystem_config.startup_contract import runtime_directory
        directory = args.runtime_dir or runtime_directory(root)
        revision = require(root, directory)
        print(f'配置 guard 通过，批准版本 r{revision}', flush=True)
        if args.execute:
            os.execvp(args.execute[0], args.execute)
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc) if isinstance(exc, ValueError) else '配置 guard 无法读取部署文件', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
