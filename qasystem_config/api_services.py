"""Non-secret LLM service records shared by ops forms and runtime validation."""
from __future__ import annotations

import math
import json
from copy import deepcopy

BASE_LIMIT_FIELDS = ('max_concurrency', 'qps', 'qpm')
LIMIT_FIELDS = BASE_LIMIT_FIELDS + ('tpm', 'quota_tiers', 'adaptive_initial_input_tokens')
CAPABILITY_FIELDS = ('context_tokens', 'max_output_tokens', 'thinking_parameter', 'api_mode', 'response_format',
                     'tokenizer_dir', 'tokenizer_revision', 'max_input_bytes')
CONNECTION_FIELDS = ('verify_ssl', 'ca_bundle', 'timeout_seconds')
LIMIT_DEFAULTS = {'max_concurrency': 4, 'qps': 0, 'qpm': 0}
LEGACY_CREDENTIAL_REFERENCES = {'extract':'KG_EXTRACT_LLM_API_KEY',
    'graph':'GRAPH_LLM_API_KEY', 'agent':'AGENT_LLM_API_KEY'}
CREDENTIAL_REFERENCE_KEYS = tuple(LEGACY_CREDENTIAL_REFERENCES.values()) + ('SILICONFLOW_API_KEY', 'DEEPSEEK_API_KEY')
API_REGISTRY_CONTRACT = {
    'service_fields': ['kind', 'model', 'base_url', 'adapter', 'api_key_env', 'capabilities', 'limits', 'account_id', 'connection', 'enabled'],
    'connection_fields': list(CONNECTION_FIELDS), 'connection_defaults': {'verify_ssl': True},
    'account_fields': list(LIMIT_FIELDS), 'capability_fields': list(CAPABILITY_FIELDS),
    'limit_defaults': LIMIT_DEFAULTS, 'routes': ['extract', 'graph', 'agent', 'entity_count'],
    'capability_defaults': {}, 'credential_storage': 'reference-only',
    'scheduler': 'internal-redis-fifo-service-and-account', 'hot_reload': 'new-request-snapshot',
    'credential_references': list(CREDENTIAL_REFERENCE_KEYS),
}
SERVICE_DESCRIPTIONS = {
    'enabled': '是否允许新请求使用服务；关闭仍保留记录，已引用用途必须先调整。',
    'verify_ssl': '验证远程服务TLS证书，默认开启；自签证书建议填写可信CA。',
    'ca_bundle': '可信CA证书文件路径，需要在调用进程所在环境中可读；不存证书私钥。',
    'timeout_seconds': '一次HTTP通信最长等待秒数，与算法任务总预算分开。',
    'base_url': 'OpenAI兼容API地址，不允许URL携带用户名、密码、查询参数或片段。',
    'model': '供应商提供的模型名称；本地服务引用已登记的模型记录。',
    'api_key_env': '已声明的凭据变量名；这里只保存引用，不填写密钥。',
    'context_tokens': '服务实际上下文上限，输入与输出合计；不是算法切片大小。',
    'max_output_tokens': '服务允许的一次输出上限；算法可请求更少。',
    'max_concurrency': '所有 Backend / Agent 进程共同允许的在途请求数。',
    'qps': '每秒最多开始多少请求；0 表示本系统不额外限速。',
    'qpm': '每分钟最多开始多少请求；0 表示本系统不额外限速。',
    'tpm': '每分钟输入与输出 token 合计预算；0 不额外限制，未知 usage 保守保留预留量。',
    'quota_tiers': '可选输入长度档位，每档登记输入上限、RPM、TPM；空列表不施加供应商分档限制。',
    'adaptive_initial_input_tokens': '429 后恢复时使用的初始输入档位下限；0 不强制更高档。',
    'tokenizer_dir': '调用进程可读取的离线 tokenizer 资产目录，通常位于只读 /models；不自动下载。',
    'tokenizer_revision': '离线资产对应的固定模型提交版本（40位十六进制），必须与 manifest 一致。',
    'max_input_bytes': '可选 API 输入字节硬上限，与算法自己的窗口策略分开。',
    'account_id': '选择共享账号额度；关联的所有服务同时受账号总限制。',
}
API_REGISTRY_CONTRACT['descriptions'] = SERVICE_DESCRIPTIONS
FIELD_LABELS = {
    'enabled': '允许调用',
    'base_url': 'API 地址', 'model': '模型名称', 'api_key_env': '凭据变量引用',
    'account_id': '共享账号', 'context_tokens': '上下文上限', 'max_output_tokens': '输出上限',
    'max_concurrency': '同时请求上限', 'qps': '每秒请求上限', 'qpm': '每分钟请求上限',
    'thinking_parameter': '思考参数协议', 'api_mode': '调用协议', 'response_format': '格式协议',
    'service': '选择服务', 'delete': '删除记录', 'create': '添加记录',
    'verify_ssl': '验证TLS证书', 'ca_bundle': '可信CA文件', 'timeout_seconds': '通信等待上限（秒）',
    'tpm': '每分钟 token 上限', 'quota_tiers': '输入档位额度',
    'adaptive_initial_input_tokens': '恢复初始输入档位', 'tokenizer_dir': '离线 tokenizer 目录',
    'tokenizer_revision': 'tokenizer 固定版本', 'max_input_bytes': 'API 输入字节上限',
    'max_input_tokens': '档位输入上限', 'rpm': '档位每分钟请求数', 'tier_create': '添加配额档位', 'tier_delete': '删除配额档位',
}






def migrate_api_services(env, services, accounts, routes):
    """Produce an editable migration plan. Never save or approve it here.

    Old values are carried into records rather than silently resetting limits.
    Conflicts are returned for the existing per-field review mechanism.
    """
    services, accounts, routes = deepcopy(services), deepcopy(accounts), deepcopy(routes)
    conflicts = {}
    def merge(name, proposal):
        existing = services.setdefault(name, {})
        if existing.get('kind') == 'local':
            conflicts[f'services.{name}.legacy_api'] = {'current': 'local', 'legacy': proposal}
            return
        for key, value in proposal.items():
            if key not in existing:
                existing[key] = value
            elif existing[key] != value:
                conflicts[f'services.{name}.{key}'] = {'current': existing[key], 'legacy': value}
    profiles = {}
    if env.get('KG_EXTRACT_PROVIDERS_JSON'):
        try:
            profiles = json.loads(env['KG_EXTRACT_PROVIDERS_JSON'])
            if not isinstance(profiles, dict) or any(not isinstance(v, dict) for v in profiles.values()):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('旧供应商配置无法迁移；请在界面核对原配置') from None
    for name, profile in profiles.items():
        proposal = {'kind': 'api', 'adapter': 'chat', 'base_url': profile.get('base_url', ''),
                    'model': profile.get('model', ''), 'api_key_env': profile.get('api_key_env', ''),
                    'limits': {k: int(profile.get(k, 4 if k == 'max_concurrency' else 0)) for k in BASE_LIMIT_FIELDS},
                    'capabilities': {}}
        # Legacy token budgets describe algorithm requests, not verified API
        # capabilities. Do not turn them into a guessed physical service cap.
        for old, new in (('thinking_parameter', 'thinking_parameter'),
                         ('api_mode', 'api_mode'), ('response_format', 'response_format')):
            if old in profile:
                proposal['capabilities'][new] = profile[old]
        group = profile.get('quota_group')
        if group:
            proposal['account_id'] = group
            old = accounts.setdefault(group, deepcopy(proposal['limits']))
            if old != proposal['limits']:
                conflicts['accounts.'+group] = {'current': old, 'legacy': proposal['limits']}
        merge(name, proposal)
    for purpose, prefix in (('extract', 'KG_EXTRACT_LLM_'), ('graph', 'GRAPH_LLM_'), ('agent', 'AGENT_LLM_')):
        if not env.get(prefix+'BASE_URL'):
            continue
        selected = routes.get(purpose)
        name = selected or 'migrated-'+purpose
        proposal = {'kind': 'api', 'adapter': 'chat', 'base_url': env.get(prefix+'BASE_URL', ''),
                    'model': env.get(prefix+'MODEL', ''), 'api_key_env': LEGACY_CREDENTIAL_REFERENCES[purpose]}
        if purpose == 'extract':
            proposal['limits'] = {k: int(env.get(prefix+{'max_concurrency':'MAX_CONCURRENCY','qps':'QPS','qpm':'QPM'}[k], 4 if k == 'max_concurrency' else 0)) for k in BASE_LIMIT_FIELDS}
            group = env.get(prefix+'QUOTA_GROUP')
            if group:
                proposal['account_id'] = group
                accounts.setdefault(group, deepcopy(proposal['limits']))
        if purpose == 'graph':
            proposal['connection'] = {}
            if 'GRAPH_LLM_VERIFY_SSL' in env:
                proposal['connection']['verify_ssl'] = env['GRAPH_LLM_VERIFY_SSL'].lower() not in {'false','0','no','off'}
            if env.get('GRAPH_LLM_TIMEOUT_SECONDS'):
                proposal['connection']['timeout_seconds'] = float(env['GRAPH_LLM_TIMEOUT_SECONDS'])
        # Registered local routing is authoritative; show the old conflicting
        # endpoint for review, never convert a local model into a remote API.
        merge(name, proposal)
        routes.setdefault(purpose, name)
    legacy = {
        'KG_EXTRACT_LLM_QUOTA_TIERS_JSON': ('limits', 'quota_tiers', json.loads),
        'KG_EXTRACT_LLM_ADAPTIVE_INITIAL_INPUT_TOKENS': ('limits', 'adaptive_initial_input_tokens', int),
        'KG_EXTRACT_LLM_TOKENIZER_DIR': ('capabilities', 'tokenizer_dir', str),
        'KG_EXTRACT_LLM_TOKENIZER_REVISION': ('capabilities', 'tokenizer_revision', str),
        'KG_EXTRACT_LLM_MAX_INPUT_BYTES': ('capabilities', 'max_input_bytes', int),
    }
    for old, (group, key, cast) in legacy.items():
        if old not in env or env[old] == '': continue
        name = routes.get('extract')
        if name not in services:
            raise ValueError('旧 LLM 容量配置需先登记抽取服务，不能静默丢弃')
        try: value = cast(env[old])
        except (TypeError, ValueError): raise ValueError('旧 LLM 容量配置无效') from None
        target = services[name].setdefault(group, {})
        if key not in target: target[key] = value
        elif target[key] != value:
            conflicts[f'services.{name}.{group}'] = {'current': target.copy(), 'legacy': {**target, key: value}}
    return services, accounts, routes, conflicts


def validate_limits(limits, *, required=False):
    if not isinstance(limits, dict) or set(limits) - set(LIMIT_FIELDS):
        raise ValueError('无效的服务或账号限额')
    if required and 'max_concurrency' not in limits:
        raise ValueError('服务或账号必须登记并发上限')
    for key, value in limits.items():
        if key == 'quota_tiers':
            from qasystem_config.quota_schema import parse_quota_tiers
            if not isinstance(value, list): raise ValueError('配额档位必须为结构化列表')
            parse_quota_tiers(value)
            continue
        if type(value) is not int or value < (1 if key == 'max_concurrency' else 0):
            raise ValueError('无效的服务或账号限额')


def validate_api_registry(services, accounts, routes):
    if not isinstance(accounts, dict):
        raise ValueError('无效的账号额度登记')
    for name, limits in accounts.items():
        if not isinstance(name, str) or not name:
            raise ValueError('账号必须有稳定名称')
        validate_limits(limits, required=True)
    for service in services.values():
        if 'enabled' in service and type(service['enabled']) is not bool:
            raise ValueError('服务调用开关必须是布尔值')
        connection = service.get('connection', {})
        if not isinstance(connection, dict) or set(connection) - set(CONNECTION_FIELDS):
            raise ValueError('无效的服务连接参数')
        if 'verify_ssl' in connection and type(connection['verify_ssl']) is not bool:
            raise ValueError('TLS证书验证必须是开关')
        if 'ca_bundle' in connection and not isinstance(connection['ca_bundle'], str):
            raise ValueError('可信CA文件必须是路径')
        timeout = connection.get('timeout_seconds')
        if timeout is not None and (type(timeout) not in {int,float} or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError('通信等待上限必须是正数')
        if 'limits' in service:
            validate_limits(service['limits'], required=True)
        account = service.get('account_id')
        if account and account not in accounts:
            raise ValueError('服务引用了未登记账号')
        caps = service.get('capabilities', {})
        if not isinstance(caps, dict) or set(caps) - set(CAPABILITY_FIELDS):
            raise ValueError('无效的服务能力登记')
        for key in ('context_tokens', 'max_output_tokens', 'max_input_bytes'):
            if key in caps and (type(caps[key]) is not int or caps[key] < 1):
                raise ValueError('服务 token 上限必须为正整数')
        for key in ('tokenizer_dir', 'tokenizer_revision'):
            if key in caps and not isinstance(caps[key], str): raise ValueError('tokenizer 配置必须为字符串')
        if caps.get('tokenizer_dir'):
            import re
            if not re.fullmatch('[a-f0-9]{40}', caps.get('tokenizer_revision', '')):
                raise ValueError('离线 tokenizer 必须登记固定版本')
        if caps.get('max_output_tokens', 0) > caps.get('context_tokens', math.inf):
            raise ValueError('输出上限不能超过上下文上限')
        for key, allowed in {'thinking_parameter': {'thinking', 'enable_thinking', 'none'},
                             'api_mode': {'auto', 'chat'},
                             'response_format': {'auto', 'json_object'}}.items():
            if key in caps and caps[key] not in allowed:
                raise ValueError('无效的服务协议能力')
    for purpose, selected in routes.items():
        if purpose not in {'extract', 'graph', 'agent', 'entity_count'}:
            raise ValueError('无效的模型用途')
        if selected and selected not in services:
            raise ValueError('用途引用了未登记服务')
