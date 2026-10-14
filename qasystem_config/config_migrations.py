"""Explicit migrations into the current configuration vocabulary."""
ALIASES = {
    'MCP_DATABASE_URL': 'DATABASE_URL',
    "KG_API_RECORD_ENABLED": "API_RECORD_ENABLED",
    "KG_API_RECORD_RETENTION_DAYS": "API_RECORD_RETENTION_DAYS",
    'KG_EXTRACT_API_URL': 'KG_EXTRACT_LLM_BASE_URL',
    'KG_EXTRACT_V3_BASE_URL': 'KG_EXTRACT_LLM_BASE_URL',
    'KG_EXTRACT_API_KEY': 'KG_EXTRACT_LLM_API_KEY',
    'KG_EXTRACT_V3_API_KEY': 'KG_EXTRACT_LLM_API_KEY',
    'KG_EXTRACT_MODEL': 'KG_EXTRACT_LLM_MODEL',
    'KG_EXTRACT_V3_MODEL': 'KG_EXTRACT_LLM_MODEL',
    'KG_EXTRACT_MAX_TOKENS': 'KG_EXTRACT_LLM_MAX_TOKENS',
    'KG_EXTRACT_JSON_RETRIES': 'KG_EXTRACT_LLM_JSON_RETRIES',
    'KG_EXTRACT_JEV_NOUL_ACCEPT': 'KG_EXTRACT_JEV_ACCEPT',
    'KG_EXTRACT_JEV_NOUL_REJECT': 'KG_EXTRACT_JEV_REJECT',
    'SOFFICE_PATH': 'LIBREOFFICE_PATH',
    'FILE_SERVICE_URL': 'APPLICATION_SERVICE_URL',
}

MODEL_KEYS = {
    'LLM_MODEL_DIR': 'model_dir', 'LLM_GPU_DEVICE_IDS': 'device',
    'LLM_BIND': 'bind', 'LLM_HOST_PORT': 'port',
    'LLM_GPU_MEMORY_UTILIZATION': 'utilization', 'LLM_MAX_MODEL_LEN': 'context',
    'LLM_MAX_NUM_SEQS': 'max_num_seqs', 'LLM_MAX_NUM_BATCHED_TOKENS': 'max_num_batched_tokens',
    'LLM_DTYPE': 'dtype',
}


def migrate_models(values, models, catalog):
    """Preserve old deployment parameters; never implicitly enable a model."""
    proposed = {repo: dict(state) for repo, state in models.items()}
    conflicts = {}
    target = values.get('LLM_SERVED_MODEL_NAME') or 'Qwen/Qwen3.5-4B'
    repos = {model['repo'] for model in catalog}
    if any(values.get(key) for key in MODEL_KEYS) and target not in repos:
        raise ValueError('旧模型名称未登记；请先登记模型再通过 ops config 迁移')
    for old, field in MODEL_KEYS.items():
        if not values.get(old):
            continue
        targets = repos if field in {'max_num_seqs', 'max_num_batched_tokens', 'dtype'} else {target}
        for repo in targets:
            state = proposed.setdefault(repo, {})
            if field in state and state[field] != ('gpu:' + values[old] if field == 'device' else values[old]):
                conflicts[f'{repo}.{field}'] = [old]
            else:
                state[field] = 'gpu:' + values[old] if field == 'device' else values[old]
    return proposed, conflicts


def migrate(values):
    """Return a proposal and conflicts, without writing files or exposing values."""
    proposed = dict(values)
    conflicts = {}
    for old, new in ALIASES.items():
        value = proposed.pop(old, None)
        if not value:
            continue
        if proposed.get(new) and proposed[new] != value:
            conflicts.setdefault(new, []).append(old)
        else:
            proposed[new] = value
    return proposed, conflicts


def duplicate_conflicts(text):
    """Repeated assignments with different values require an explicit UI choice."""
    from qasystem_config.io import read_env
    seen, conflicts = {}, {}
    for line in text.splitlines():
        for old, value in read_env(line).items():
            key = ALIASES.get(old, old)
            if key in seen and seen[key] != value:
                conflicts.setdefault(key, []).append(old)
            seen[key] = value
    return conflicts


NATIVE_STARTUP_KEYS = {
    name + '_' + suffix for name in ('CLEF', 'NANOJEV') for suffix in
    ('MODEL_DIR', 'CUDA_VISIBLE_DEVICES', 'DEVICE', 'PORT', 'BIND', 'DTYPE', 'MAX_LENGTH', 'CPU_THREADS',
     'MAX_REQUEST_BYTES', 'MAX_QUESTIONS', 'MAX_OPTIONS', 'MAX_TOTAL_OPTIONS', 'MAX_QUEUE_SIZE')
}
WORKER_STARTUP_KEYS = {'VLLM_MODEL_PATH', 'VLLM_ENABLED', 'VLLM_BASE_PORT', 'VLLM_SERVED_MODEL_NAME',
                       'VLLM_GPU_MEMORY_UTILIZATION', 'VLLM_MAX_NUM_SEQS', 'VLLM_EXTRA_ARGS'}


def migrate_inference(values, models, services, routes):
    from copy import deepcopy
    proposed, services, routes = deepcopy(models), deepcopy(services), dict(routes)
    conflicts = {}
    def put(model_id, key, value):
        state = proposed.setdefault(model_id, {})
        if key in state and state[key] != value:
            conflicts[model_id + '.' + key] = ['旧模型启动参数']
        else:
            state[key] = value
    for prefix, model_id, adapter in [('CLEF', 'clef-flash', 'clef'), ('NANOJEV', 'nanojev', 'nanojev')]:
        startup = {key: values[key] for key in NATIVE_STARTUP_KEYS if key.startswith(prefix + '_') and key in values}
        if not startup: continue
        for key, value in [('engine', 'native'), ('adapter', adapter), ('manager', 'ops')]: put(model_id, key, value)
        for suffix, key in [('MODEL_DIR', 'model_dir'), ('PORT', 'port'), ('BIND', 'bind')]:
            old = values.get(prefix + '_' + suffix)
            if old is not None:
                if key == 'model_dir':
                    if old.startswith('/models/'): old = old[len('/models/'):]
                    elif old.startswith('/') or ':' in old: raise ValueError('旧权重目录不在 /models 内，需人工归位')
                if key == 'bind':
                    conflicts[model_id + '.bind'] = ['旧值是容器内部监听地址，需明确确认宿主发布地址']
                    proposed.setdefault(model_id, {}).setdefault('bind', '127.0.0.1')
                else:
                    put(model_id, key, old)
        device, cuda = values.get(prefix + '_DEVICE', ''), values.get(prefix + '_CUDA_VISIBLE_DEVICES', '')
        if device == 'cpu' and cuda:
            conflicts[model_id + '.device'] = [prefix + '_DEVICE', prefix + '_CUDA_VISIBLE_DEVICES']
        elif device == 'cpu': put(model_id, 'device', 'cpu')
        elif cuda: put(model_id, 'device', 'gpu:' + cuda)
        options = dict(proposed.get(model_id, {}).get('engine_options', {}))
        for suffix in ('DTYPE', 'MAX_LENGTH', 'CPU_THREADS', 'MAX_REQUEST_BYTES', 'MAX_QUESTIONS', 'MAX_OPTIONS', 'MAX_TOTAL_OPTIONS', 'MAX_QUEUE_SIZE'):
            if prefix + '_' + suffix in values:
                key = suffix.lower(); old = values[prefix + '_' + suffix]
                if key in options and options[key] != old: conflicts[model_id + '.engine_options'] = [prefix + '_' + suffix]
                else: options[key] = old
        put(model_id, 'engine_options', options)
    if any(key in values for key in WORKER_STARTUP_KEYS):
        model_id = 'mineru-vlm'
        for key, value in [('engine', 'vllm'), ('adapter', 'chat'), ('manager', 'backend-worker'), ('device', 'worker')]: put(model_id, key, value)
        put(model_id, 'enabled', values.get('VLLM_ENABLED', 'true').lower() not in {'0', 'false', 'no', 'off'})
        for old, key in [('VLLM_MODEL_PATH', 'model_dir'), ('VLLM_BASE_PORT', 'port'),
                         ('VLLM_GPU_MEMORY_UTILIZATION', 'utilization'), ('VLLM_MAX_NUM_SEQS', 'max_num_seqs')]:
            if old in values: put(model_id, key, values[old].removeprefix('/models/') if key == 'model_dir' else values[old])
        opts = dict(proposed.get(model_id, {}).get('engine_options', {}))
        for old, key in [('VLLM_SERVED_MODEL_NAME', 'served_model'), ('VLLM_EXTRA_ARGS', 'extra_args')]:
            if old in values: opts[key] = values[old]
        put(model_id, 'engine_options', opts)
    # Existing endpoint declarations remain remote services until an explicit local route is chosen.
    for purpose, prefix, service_id, adapter, model in [
        ('extract', 'KG_EXTRACT_LLM', 'legacy-extract', 'chat', values.get('KG_EXTRACT_LLM_MODEL', '')),
        ('entity_count', 'CLEF_SERVICE', 'legacy-clef', 'clef', values.get('CLEF_MODEL', 'clef-flash')),
        (None, 'NANOJEV_SERVICE', 'legacy-nanojev', 'nanojev', 'nanojev')]:
        base = values.get(prefix + ('_BASE_URL' if purpose == 'extract' else '_URL'))
        if base and service_id not in services:
            services[service_id] = {'kind': 'api', 'base_url': base, 'adapter': adapter, 'model': model,
                                    'api_key_env': prefix + '_API_KEY'}
            if purpose and purpose not in routes: routes[purpose] = service_id
    for model_id, state in proposed.items():
        if state.get('manager', 'ops') == 'ops':
            adapter = state.get('adapter', 'chat')
            key_env = 'CLEF_SERVICE_API_KEY' if adapter == 'clef' else 'NANOJEV_SERVICE_API_KEY' if adapter == 'nanojev' else ''
            services.setdefault(model_id, {'kind': 'local', 'model': model_id, 'api_key_env': key_env})
    return proposed, services, routes, conflicts
