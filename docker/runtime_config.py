"""Single reader for persisted, non-secret application configuration."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

HOT_KEYS = frozenset({
    "KG_EXTRACT_ALGO_VERSION", "KG_EXTRACT_LLM_BASE_URL", "KG_EXTRACT_LLM_MODEL",
    "KG_EXTRACT_LLM_MAX_TOKENS", "KG_EXTRACT_LLM_MAX_INPUT_TOKENS",
    "KG_EXTRACT_LLM_MAX_OUTPUT_TOKENS", "KG_EXTRACT_LLM_MAX_CONCURRENCY",
    "KG_EXTRACT_LLM_QPS", "KG_EXTRACT_LLM_QPM", "KG_EXTRACT_LLM_TIMEOUT_SECONDS",
    "KG_EXTRACT_LLM_DISABLE_THINKING", "KG_EXTRACT_LLM_QUOTA_GROUP",
    "KG_EXTRACT_V31_REQUEST_TIMEOUT", "KG_EXTRACT_V31_DOCUMENT_TIMEOUT",
    "EMBEDDING_SERVICE_URL", "EMBEDDING_SERVICE_PATH", "EMBEDDING_MODEL",
    "EMBEDDING_TIMEOUT_SECONDS", "EMBEDDING_MAX_BATCH_SIZE",
    "DOCUMENT_PARSE_SERVICE_URL", "DOCUMENT_PARSE_SERVICE_PATH", "DOCUMENT_PARSE_TIMEOUT_SECONDS",
    "CLEF_SERVICE_URL", "CLEF_SERVICE_PATH", "CLEF_SERVICE_TIMEOUT_SECONDS", "CLEF_MODEL",
    "NANOJEV_SERVICE_URL", "NANOJEV_SERVICE_PATH", "NANOJEV_SERVICE_TIMEOUT_SECONDS",
})

LEGACY_MODEL_KEYS = frozenset({"LLM_MODEL_DIR", "LLM_SERVED_MODEL_NAME", "LLM_GPU_DEVICE_IDS",
    "LLM_BIND", "LLM_HOST_PORT", "LLM_GPU_MEMORY_UTILIZATION", "LLM_MAX_MODEL_LEN",
    "LLM_MAX_NUM_SEQS", "LLM_MAX_NUM_BATCHED_TOKENS", "LLM_DTYPE"})

MODEL_DEFAULTS = dict(enabled=False, device='', engine='vllm', adapter='chat', manager='ops', engine_options={}, port='9998', context='32768', utilization='0.90',
                      bind='127.0.0.1', model_dir='', max_num_seqs='8', max_num_batched_tokens='2048', dtype='bfloat16')
MODEL_DESCRIPTIONS = {
    'enabled': '勾选只改变启用计划，部署后才启动服务。',
    'device': 'cpu 或 gpu:<宿主物理编号>；worker 仅用于 Backend 管理的解析引擎，不自动回退。',
    'engine': '推理方式：vllm 或 native。', 'adapter': '已注册的启动适配器：chat、clef、nanojev。',
    'manager': '唯一启动管理者：ops 或 backend-worker。',
    'engine_options': '引擎专属参数 JSON；不包含密钥值。',
    'port': '宿主机监听端口，不能与其他服务重复；新模型按清单顺序从 9997 分配。',
    'context': '单次输入与输出 token 的合计上限。',
    'utilization': 'vLLM 可使用的 GPU 显存比例，必须在 0 与 1 之间。',
    'bind': '127.0.0.1 只允许本机访问；0.0.0.0 监听所有 IPv4 网卡。',
    'model_dir': '相对 HOST_MODELS_DIR 的权重目录；模型默认路径为 llm/<仓库名，斜杠替换为双横线>。',
    'max_num_seqs': '同时处理的最大序列数，必须为正整数。',
    'max_num_batched_tokens': '每个调度批次的 token 上限，必须为正整数。',
    'dtype': 'vLLM 计算精度；auto 按权重配置选择。',
}


def runtime_path(root: Path | None = None, env=None) -> Path:
    root = Path.cwd() if root is None else Path(root)
    from docker.startup_contract import runtime_directory
    return Path('/app/runtime-config/config.json') if Path('/app/runtime-config').is_dir() else runtime_directory(root) / 'config.json'


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ValueError("Invalid runtime configuration file") from None


def read_document(path: Path | None = None, *, root: Path | None = None, env=None, migration=False) -> dict:
    path = runtime_path(root, env) if path is None else Path(path)
    data = _json(path)
    if data is None and path.name == "config.json":
        legacy = _json(path.with_name("settings.json"))
        models = _json(path.with_name("models.json"))
        data = {"settings": {} if legacy is None else legacy,
                "models": {} if models is None else models}
    elif data is None:
        data = {"settings": {}, "models": {}}
    elif path.name != "config.json" and isinstance(data, dict) and "settings" not in data:
        data = {"settings": data, "models": {}}
    if migration and isinstance(data, dict) and isinstance(data.get('settings'), dict):
        from docker.configuration import declarations
        retired = set(declarations().get('retired', []))
        data = {**data, 'settings': {k: v for k, v in data['settings'].items() if k not in retired}}
    if migration and isinstance(data, dict) and isinstance(data.get('models'), dict):
        data = {**data, 'models': {name: migrate_record(model) for name, model in data['models'].items()}}
    return _validate_document(data)


def _validate_document(data):
    if not isinstance(data, dict) or set(data) - {"settings", "models", "services", "routes"}:
        raise ValueError("Invalid runtime configuration structure")
    settings, models = data.get("settings", {}), data.get("models", {})
    if not isinstance(settings, dict) or any(k not in HOT_KEYS or not isinstance(v, str) for k, v in settings.items()):
        raise ValueError("Invalid runtime settings keys or values")
    for key, value in settings.items():
        if key.endswith('_SERVICE_URL') and value:
            url = urlsplit(value)
            if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError('Invalid runtime service URL')
        if key.endswith('_SERVICE_PATH') and value and (not value.startswith('/') or value.startswith('//') or '?' in value or '#' in value):
            raise ValueError('Invalid runtime service path')
        if key in {'EMBEDDING_TIMEOUT_SECONDS', 'DOCUMENT_PARSE_TIMEOUT_SECONDS',
                   'CLEF_SERVICE_TIMEOUT_SECONDS', 'NANOJEV_SERVICE_TIMEOUT_SECONDS'}:
            import math
            try:
                valid = math.isfinite(float(value)) and float(value) > 0
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('Invalid runtime service timeout')
        if key == 'EMBEDDING_MAX_BATCH_SIZE':
            try:
                valid = int(value) > 0
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('Invalid runtime embedding batch size')
    if not isinstance(models, dict) or any(not isinstance(m, dict) for m in models.values()):
        raise ValueError("Invalid runtime model configuration")
    fields = set(MODEL_DEFAULTS)
    for repo, model in models.items():
        if (not isinstance(repo, str) or set(model) - fields or
                ('enabled' in model and not isinstance(model['enabled'], bool)) or
                any(not isinstance(v, str) for k, v in model.items() if k not in {'enabled', 'engine_options'}) or
                ('engine_options' in model and (not isinstance(model['engine_options'], dict) or
                 any(not isinstance(k, str) or not isinstance(v, (str, bool, int, float)) for k, v in model['engine_options'].items())))):
            raise ValueError('Invalid runtime model keys or values')
    services, routes = data.get('services', {}), data.get('routes', {})
    if not isinstance(services, dict) or not isinstance(routes, dict):
        raise ValueError('Invalid service registry')
    if any(not isinstance(k, str) or not isinstance(v, dict) for k, v in services.items()):
        raise ValueError('Invalid service records')
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in routes.items()):
        raise ValueError('Invalid service routes')
    for service in services.values():
        allowed = {'kind', 'model', 'api_key_env'} if service.get('kind') == 'local' else {'kind', 'model', 'api_key_env', 'adapter', 'base_url'}
        if set(service) - allowed or service.get('kind') not in {'local', 'api', 'worker'}:
            raise ValueError('Invalid service record keys')
        if any(not isinstance(v, str) for v in service.values()): raise ValueError('Invalid service values')
        if service.get('kind') == 'api':
            url = urlsplit(service.get('base_url', ''))
            if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError('Invalid service URL')
        if service.get('api_key_env'):
            import re
            if not re.fullmatch(r'[A-Z][A-Z0-9_]*', service['api_key_env']): raise ValueError('Invalid credential reference')
    return {"settings": settings, "models": models, 'services': services, 'routes': routes}


def read_registry(path: Path | None = None, *, root: Path | None = None, env=None, with_revision=False):
    path = runtime_path(root, env) if path is None else Path(path)
    managed = path.name == 'config.json' and (path.is_file() or
        (path.parent / 'approved-config.json').exists() or (path.parent / 'history').is_dir() or
        str(path).replace('\\', '/') == '/app/runtime-config/config.json')
    if managed:
        try:
            from docker.config_guard import require_runtime
        except ImportError:
            from config_guard import require_runtime
        raw = path.read_bytes() if path.is_file() else None
        revision = require_runtime(path.parent, config_bytes=raw)
        try:
            data = _validate_document(json.loads(raw))
        except (ValueError, TypeError):
            raise ValueError('Invalid runtime configuration file') from None
        return (data, revision) if with_revision else data
    data = read_document(path, root=root, env=env)
    return (data, None) if with_revision else data


def migrate_record(model):
    model = dict(model)
    old = model.pop('gpu', None)
    if old is not None:
        device = 'gpu:' + old if old else ''
        if model.get('device') and model['device'] != device:
            raise ValueError('旧模型 GPU 与设备记录冲突；拒绝静默覆盖')
        model['device'] = device
    return model


def read_settings(path=None, *, root=None, env=None, with_revision=False):
    data, revision = read_registry(path, root=root, env=env, with_revision=True)
    return (data['settings'], revision) if with_revision else data['settings']
