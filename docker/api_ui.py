# Keyboard forms; registry validation lives in qasystem_config.
from qasystem_config.api_services import (LIMIT_FIELDS, CAPABILITY_FIELDS, CONNECTION_FIELDS,
    LIMIT_DEFAULTS, FIELD_LABELS, SERVICE_DESCRIPTIONS)


def ui_rows(config):
    """Flat keyboard rows, with one field per row; no user-authored JSON."""
    rows = []
    def row(kind, name, field, value, path=()):
        return dict(kind='api_registry', registry_kind=kind, record_id=name,
                    key=field, path=path, display_name=f'[{kind}] {name} · {FIELD_LABELS.get(field, field)}',
                    display_value=str(value) if value is not None else '(未设置)',
                    label=f'{name} {field}', description=SERVICE_DESCRIPTIONS.get(field, field))
    rows.append(row('新增服务', '', 'create', 'Enter 添加 LLM API'))
    rows.append(row('新增账号', '', 'create', 'Enter 添加共享账号额度'))
    for name, service in config.services.items():
        for key in ('enabled', 'model', 'base_url', 'api_key_env', 'account_id'):
            if key == 'base_url' and service.get('kind') != 'api':
                continue
            rows.append(row('服务', name, key, service.get(key, True if key=='enabled' else '')))
        for group, fields in (('limits', LIMIT_FIELDS), ('capabilities', CAPABILITY_FIELDS), ('connection', CONNECTION_FIELDS)):
            for key in fields:
                rows.append(row('服务', name, key, service.get(group, {}).get(key), (group,)))
        rows.append(row('服务', name, 'delete', 'Enter 删除（检查引用）'))
    for name, limits in getattr(config, 'accounts', {}).items():
        for key in LIMIT_FIELDS:
            rows.append(row('账号', name, key, limits.get(key)))
        rows.append(row('账号', name, 'delete', 'Enter 删除（检查引用）'))
    for purpose in ('extract', 'graph', 'agent', 'entity_count'):
        rows.append(row('用途', purpose, 'service', config.routes.get(purpose, '')))
    return rows


def edit_field(config, row, screen=None, *, prompt=input):
    """Edit plans only. Existing save/review guard remains the sole approval."""
    kind, name, field = row['registry_kind'], row['record_id'], row['key']
    if kind in {'新增服务', '新增账号'}:
        name = prompt('稳定 ID: ').strip()
        target = config.services if kind == '新增服务' else config.accounts
        if not name or name in target:
            raise ValueError('名称为空或已存在')
        target[name] = {'kind': 'api', 'adapter': 'chat', 'model': '', 'base_url': '',
                        'api_key_env': '', 'limits': dict(LIMIT_DEFAULTS),
                        'capabilities': {}} if kind == '新增服务' else dict(LIMIT_DEFAULTS)
        return
    if field == 'delete':
        if kind == '服务' and name in config.routes.values():
            raise ValueError('请先解除功能对服务的引用')
        if kind == '账号' and any(s.get('account_id') == name for s in config.services.values()):
            raise ValueError('请先解除服务对账号的引用')
        if prompt('输入 DELETE 确认从计划删除: ').strip() == 'DELETE':
            (config.services if kind == '服务' else config.accounts).pop(name)
        return
    value = prompt(row['display_name'] + ' 新值: ').strip()
    if kind == '用途':
        if value and value not in config.services:
            raise ValueError('请选择已登记服务：' + ', '.join(config.services))
        if value:
            config.routes[name] = value
        else:
            config.routes.pop(name, None)
        return
    if field == 'account_id' and value and value not in config.accounts:
        raise ValueError('请选择已登记账号')
    target = config.services[name] if kind == '服务' else config.accounts[name]
    for part in row.get('path', ()):
        target = target.setdefault(part, {})
    numeric = field in LIMIT_FIELDS or field in {'context_tokens', 'max_output_tokens'}
    if field in {'verify_ssl', 'enabled'}:
        if value.lower() not in {'true','false','1','0'}:
            raise ValueError('TLS验证请选择true/false')
        target[field] = value.lower() in {'true','1'}
        return
    if field == 'timeout_seconds':
        target[field] = float(value)
        return
    if not value and row.get('path') == ('capabilities',):
        target.pop(field, None)
    else:
        target[field] = int(value) if numeric else value
