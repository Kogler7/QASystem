"""Declared application configuration access. OS environment is an adapter only."""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path


class UndeclaredConfigurationError(RuntimeError):
    """Cannot be swallowed as an ordinary numeric environment parse error."""


@lru_cache(maxsize=1)
def declarations():
    return json.loads(Path(__file__).with_name('config-declarations.json').read_text(encoding='utf-8'))


def declared(key):
    catalog = declarations()
    if key in catalog.get('retired', []):
        raise UndeclaredConfigurationError('配置项已删除或迁移；请使用当前版本标准配置')
    if key in catalog['items']:
        return False
    from qasystem_config.credentials import bindings
    document, revision = read_registry(with_revision=True)
    if revision is not None and key in bindings(document):
        return True
    raise UndeclaredConfigurationError('读取了未声明的配置项；请更新当前版本配置声明')


def startup_getenv(key, default=None):
    declared(key)
    from qasystem_config.runtime_config import PROCESS_KEYS
    if key in PROCESS_KEYS:
        return getenv(key, default)
    return os.getenv(key, default)


def _observe(key, value, source, revision=None, credential=False):
    # Directory mounts are read-only. Consumption evidence belongs to a writable
    # service data volume, never to the approved configuration directory.
    if not Path('/app/runtime-config/approved-config.json').is_file():
        return
    from qasystem_config.config_database import observe_read
    from qasystem_config.config_guard import verified_record
    if revision is None:
        revision = verified_record(Path('/app/runtime-config'))['revision']
    directory = Path('/app/runtime-config/observed')
    if not directory.is_dir() or not os.access(directory, os.W_OK):
        raise ValueError('配置读取历史目录不可写；请通过 ops deploy 更新挂载')
    observe_read(key, value, directory=directory, revision=revision, source=source, credential=credential)


def getenv(key, default=None):
    credential = declared(key)
    from qasystem_config.runtime_config import HOT_KEYS, PROCESS_KEYS, read_settings
    if key in PROCESS_KEYS:
        settings, revision = read_settings(with_revision=True)
        if revision is None or key not in settings:
            raise ValueError('执行进程配置未提交；请通过 ops 更新配置')
        _observe(key, settings[key], 'process-plan', revision=revision)
        return settings[key]
    if key in HOT_KEYS:
        settings, revision = read_settings(with_revision=True)
        if key in settings:
            _observe(key, settings[key], 'hot', revision=revision)
            return settings[key]
    value = os.getenv(key, default)
    _observe(key, value, 'environment' if key in os.environ else 'fallback', credential=credential)
    return value


def required(key):
    value = getenv(key)
    if value is None:
        raise KeyError(key)
    return value


def read_registry(*, with_revision=False):
    """Read one approved snapshot; callers retain it for in-flight work."""
    from qasystem_config.runtime_config import read_registry as read
    return read(with_revision=with_revision)


def approved_execution_plan(*, revision=None):
    """Read the committed process plan; old container ENV never overrides it."""
    document, actual_revision = read_registry(with_revision=True)
    if actual_revision is None or (revision is not None and actual_revision != revision):
        raise ValueError('执行进程配置尚未提交或版本已变化；请通过 ops 更新配置')
    configured = document.get('settings', {}).get('EXECUTION_CPU_WORKERS')
    if not isinstance(configured, str):
        raise ValueError('配置缺少 CPU 执行计划；请通过 ops 升级配置')
    normalized = configured.strip().lower()
    if normalized != 'auto':
        if not normalized.isdigit() or not 1 <= int(normalized) <= 32:
            raise ValueError('CPU 进程数必须为 auto 或 1–32')
        normalized = str(int(normalized))
    _observe('EXECUTION_CPU_WORKERS', normalized, 'process-plan', revision=actual_revision)
    return {'revision': actual_revision, 'cpu_workers': normalized}


def should_load_local_dotenv():
    from qasystem_config.startup_contract import in_container_runtime
    return not in_container_runtime() and startup_getenv('ENVIRONMENT') is None


def bootstrap_local_environment():
    """Load only the repository-root dotenv for unmanaged development startup."""
    if not should_load_local_dotenv():
        return False
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / '.env', override=False)
    return True


def prepare_dev_agent_startup(concurrency):
    """Carry one validated CLI option to spawned development API processes.

    Managed deployments can only change this setting through approved ops
    configuration. The application-side caller is limited by the source gate.
    """
    from qasystem_config.startup_contract import in_container_runtime
    if in_container_runtime() or startup_getenv('ENVIRONMENT') is not None:
        raise ValueError('受管部署不能用CLI覆盖任务并发，请通过ops审核配置')
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 256:
        raise ValueError('开发启动并发必须为1到256之间的整数')
    os.environ['AGENT_WORKER_CONCURRENCY'] = str(concurrency)


def configure_mineru_loader_environment(config_path, model_source):
    """Registered MinerU loader dependency initialization; not a generic setter."""
    os.environ['MINERU_TOOLS_CONFIG_JSON'] = str(config_path)
    os.environ['MINERU_CONFIG_PATH'] = str(config_path)
    os.environ['MINERU_MODEL_SOURCE'] = str(model_source)
