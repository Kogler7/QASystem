"""Declared application configuration access. OS environment is an adapter only."""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import re


class UndeclaredConfigurationError(RuntimeError):
    """Cannot be swallowed as an ordinary numeric environment parse error."""


@lru_cache(maxsize=1)
def declarations():
    return json.loads(Path(__file__).with_name('config-declarations.json').read_text(encoding='utf-8'))


def declared(key):
    catalog = declarations()
    if key in catalog['items'] or any(re.fullmatch(pattern, key) for pattern in catalog['dynamic_keys']):
        return
    raise UndeclaredConfigurationError('读取了未声明的配置项；请更新当前版本配置声明')


def startup_getenv(key, default=None):
    declared(key)
    return os.getenv(key, default)


def _observe(key, value, source, revision=None):
    # Directory mounts are read-only. Consumption evidence belongs to a writable
    # service data volume, never to the approved configuration directory.
    if not Path('/app/runtime-config/approved-config.json').is_file():
        return
    from docker.config_database import observe_read
    from docker.config_guard import verified_record
    if revision is None:
        revision = verified_record(Path('/app/runtime-config'))['revision']
    directory = Path('/app/runtime-config/observed')
    if not directory.is_dir() or not os.access(directory, os.W_OK):
        raise ValueError('配置读取历史目录不可写；请通过 ops deploy 更新挂载')
    observe_read(key, value, directory=directory, revision=revision, source=source)


def getenv(key, default=None):
    declared(key)
    from docker.runtime_config import HOT_KEYS, read_settings
    if key in HOT_KEYS:
        settings, revision = read_settings(with_revision=True)
        if key in settings:
            _observe(key, settings[key], 'hot', revision=revision)
            return settings[key]
    value = os.getenv(key, default)
    _observe(key, value, 'environment' if key in os.environ else 'fallback')
    return value


def required(key):
    value = getenv(key)
    if value is None:
        raise KeyError(key)
    return value
