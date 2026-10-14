"""Fixed process and directory conventions, versioned with source releases."""
from pathlib import Path

RUNTIME_DIRECTORY_NAME = 'runtime-config'
PROCESS_ENVIRONMENT = {
    'ENVIRONMENT': 'production',
    'PYTHONPATH': '/app:/app/agent',
    'PYTHONUNBUFFERED': '1',
}


def runtime_directory(root):
    return Path(root).resolve() / RUNTIME_DIRECTORY_NAME


def in_container_runtime():
    return (Path('/.dockerenv').is_file() or Path('/run/.containerenv').is_file()
            or Path('/app/runtime-config').is_dir())


def internal_redis_url(database):
    """Deployment Redis topology; local development uses the exposed loopback port."""
    host = 'redis' if in_container_runtime() else 'localhost'
    return f'redis://{host}:6379/{int(database)}'
