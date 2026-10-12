"""Explicit migrations into the current configuration vocabulary."""
ALIASES = {
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
    'LLM_MODEL_DIR': 'model_dir', 'LLM_GPU_DEVICE_IDS': 'gpu',
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
            if field in state and state[field] != values[old]:
                conflicts[f'{repo}.{field}'] = [old]
            else:
                state[field] = values[old]
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
    from docker.setup_llm import read_env
    seen, conflicts = {}, {}
    for line in text.splitlines():
        for old, value in read_env(line).items():
            key = ALIASES.get(old, old)
            if key in seen and seen[key] != value:
                conflicts.setdefault(key, []).append(old)
            seen[key] = value
    return conflicts
