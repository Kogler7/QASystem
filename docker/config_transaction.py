"""Validated configuration transactions shared by automation and the terminal UI.

Patch files are private input, never command-line credential values. Planning
creates the same private backups as opening the editor, but publishes nothing.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path

from docker import config_guard as guard
from docker.config_database import database, private_write


def read_patch(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raise ValueError('无法读取配置 patch JSON；未输出输入内容') from None
    if not isinstance(data, dict) or set(data) - {
        'settings', 'models', 'services', 'accounts', 'routes', 'resolve_conflicts'}:
        raise ValueError('配置 patch 顶层字段无效')
    return data


def patch_configuration(config, patch):
    """Merge named records; null explicitly deletes registry records, never settings."""
    if not isinstance(patch, dict) or set(patch) - {'settings', 'models', 'services', 'accounts', 'routes', 'resolve_conflicts'}:
        raise ValueError('配置 patch 顶层字段无效')
    allowed = {field['key'] for field in config.fields}
    settings = patch.get('settings', {})
    if not isinstance(settings, dict) or any(key not in allowed or not isinstance(value, str)
                                             for key, value in settings.items()):
        raise ValueError('settings 只能使用当前模板标准配置名和字符串值')
    config.values.update(settings)
    provided = set(settings)
    for section in ('models', 'services', 'accounts', 'routes'):
        changes = patch.get(section, {})
        if not isinstance(changes, dict):
            raise ValueError('配置登记变更必须为对象')
        target = getattr(config, section)
        for name, value in changes.items():
            if not isinstance(name, str):
                raise ValueError('配置记录名称必须为字符串')
            if value is None:
                target.pop(name, None)
            elif section == 'routes':
                if not isinstance(value, str): raise ValueError('路由必须引用服务名称')
                target[name] = value
            else:
                if not isinstance(value, dict): raise ValueError('登记记录必须为对象')
                provided.add(section + '.' + name)
                provided.update(name + '.' + key for key in value)
                provided.update(section + '.' + name + '.' + key for key in value)
                if section == 'services': provided.add(section + '.' + name + '.legacy_api')
                defaults = {}
                if section == 'models':
                    from docker.runtime_config import MODEL_DEFAULTS
                    defaults = deepcopy(config.model_defaults.get(name, MODEL_DEFAULTS))
                target[name] = {**defaults, **deepcopy(target.get(name, {})), **deepcopy(value)}
    resolutions = patch.get('resolve_conflicts', {})
    if not isinstance(resolutions, dict): raise ValueError('冲突解决必须为对象')
    for key, choice in resolutions.items():
        if key not in config.migration_conflicts or choice not in {'keep', 'patched', 'retire'}:
            raise ValueError('冲突名称或解决策略无效')
        if choice == 'patched' and key not in provided:
            raise ValueError('patched 冲突解决必须显式提供标准配置取值')
        config.resolved_migrations.add(key)


def plan(config):
    for record in config.models.values():
        if any(key.lower() in {'api_key', 'secret', 'password', 'token', 'authorization'}
               for key in record.get('engine_options', {})):
            raise ValueError('模型参数不得包含原始凭据，只能登记凭据引用')
    errors = config.validate(probe=False)
    if errors: raise ValueError('；'.join(errors))
    current = guard.snapshot(config.root, config.directory, payloads=config.pending_payloads())
    previous = guard.approved(config.approval_directory)
    old, new = (previous or {}).get('snapshot', {}).get('values', {}), current['values']
    rows = []
    for key in sorted(set(old) | set(new)):
        if old.get(key) == new.get(key): continue
        action = 'added' if key not in old else 'removed' if key not in new else 'changed'
        mode = 'hot' if key.startswith(('settings.', 'services.', 'routes.', 'accounts.')) else 'container'
        if key.startswith('settings.'):
            from docker.configuration import declarations
            if declarations()['items'].get(key.split('.', 1)[1], {}).get('mode') in {'process', 'processes'}: mode = 'process'
        if key.startswith(('declarations.', 'catalog.', 'release.')): mode = 'review'
        rows.append({'key': key, 'action': action, 'before': old.get(key), 'after': new.get(key), 'effect': mode})
    from docker.config_migrations import ALIASES
    from docker.configuration import declarations
    from docker.setup_llm import read_env
    retired = set(declarations().get('retired', []))
    migrations = [{'source': key, 'target': ALIASES.get(key),
                   'action': 'migrated' if key in ALIASES else 'retired',
                   'previous': guard.safe_value(key, value)}
                  for key, value in read_env(config.original).items() if key in retired or key in ALIASES]
    return {'schema': 1, 'base_revision': (previous or {}).get('revision', 0),
            'source_commit': new['release.source_commit']['value'],
            'declaration_version': new['declarations.version']['value'],
            'changes': rows, 'migrations': migrations, 'migration_conflicts': sorted(config.migration_conflicts),
            'resolved_conflicts': sorted(config.resolved_migrations), 'deployed': False}, current


@contextmanager
def transaction_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / '.config-transaction.lock'
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError('另一个配置事务正在执行；若进程已退出，请确认后删除遗留事务锁') from None
    try:
        with os.fdopen(fd, 'w') as stream: stream.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def apply(config, *, reason, actor='agent', base_revision, expected_snapshot=None, reviewer=None):
    """Publish only validated/CAS-matched state; restore approval and files on failure."""
    if actor not in {'agent', 'human', 'automation'} or not isinstance(reason, str) or not reason.strip():
        raise ValueError('必须提供有效操作者类型和非空变更原因')
    if any(value and value in reason for key, value in config.values.items() if guard.sensitive(key)):
        raise ValueError('变更原因不得包含凭据内容')
    with transaction_lock(config.directory):
        report, proposed = plan(config)
        if report['base_revision'] != base_revision:
            raise ValueError('配置批准版本已变化；请重新生成计划')
        if expected_snapshot is not None and proposed != expected_snapshot:
            raise ValueError('配置候选与审核计划不一致')
        if base_revision:
            guard.verified_record(config.approval_directory)
        # Back up only publication files; unrelated application read events are untouched.
        tracked = set(config.expected) | set(config.pending_payloads())
        tracked |= {config.directory / 'approved-config.json', config.directory / 'database.json'}
        prior_files = {path: path.read_bytes() if path.exists() else None for path in tracked}
        editor_state = {name: deepcopy(getattr(config, name)) for name in
                        ('expected', 'persisted_hot', 'initial_hot', 'input_directory',
                         'approval_directory', 'legacy_expected')}
        prior_artifacts = {path for sub in ('history', 'versions', 'events', 'changes')
                           for path in (config.directory / sub).rglob('*') if path.is_file()}
        try:
            config.save()
            actual = guard.snapshot(config.root, config.directory)
            if actual != proposed: raise ValueError('保存期间配置变化；事务已回滚')
            audit = {'base_revision': base_revision, 'resolved_conflicts': sorted(config.resolved_migrations),
                     'source_commit': report['source_commit'], 'declaration_version': report['declaration_version'],
                     'migrations': report['migrations']}
            revision = guard.record(config.directory, actual, reason=reason.strip(), reviewer=reviewer or actor,
                                    actor=actor, audit=audit, root=config.root)
            report.update(revision=revision, actor=actor, reason=reason.strip(), state='approved')
            report_dir = config.directory / 'changes' / f'r{revision:06d}'
            private_write(report_dir / 'changes.json', json.dumps(report, ensure_ascii=False, indent=2).encode())
            lines = [f'配置变更 r{revision}', f'操作者：{actor}', '状态：配置已保存并审核，尚未部署。', '']
            def display(value):
                if value is None: return '(未配置)'
                return str(value.get('value', '(无)')).replace('\n', ' ')[:240]
            lines += [f"- {row['action']} {row['key']} [{row['effect']}]："
                      f"{display(row['before'])} → {display(row['after'])}" for row in report['changes']]
            private_write(report_dir / 'changes.md', ('\n'.join(lines) + '\n').encode())
            database(config.directory)
            guard.require(config.root, config.directory)
            return report
        except Exception:
            for path, raw in prior_files.items():
                if raw is None: path.unlink(missing_ok=True)
                else: private_write(path, raw)
            for sub in ('history', 'versions', 'events', 'changes'):
                for path in (config.directory / sub).rglob('*'):
                    if path.is_file() and path not in prior_artifacts:
                        if sub == 'events':
                            try: event = json.loads(path.read_text())
                            except (OSError, ValueError): continue
                            if event.get('event') != 'approved' or event.get('revision') != base_revision + 1: continue
                        path.unlink()
                for path in sorted((config.directory / sub).rglob('*'), reverse=True):
                    if path.is_dir() and not any(path.iterdir()): path.rmdir()
            for name, value in editor_state.items(): setattr(config, name, value)
            raise


def run(root, *, operation, patch_path=None, reason=None, actor='agent', base_revision=None):
    from docker.configure import Configuration
    config = Configuration(Path(root))
    if patch_path is not None: patch_configuration(config, read_patch(patch_path))
    if operation in {'plan', 'diff'}: return plan(config)[0]
    if base_revision is None: raise ValueError('apply 必须提供 --base-revision，首次为 0')
    return apply(config, reason=reason, actor=actor, base_revision=base_revision)
