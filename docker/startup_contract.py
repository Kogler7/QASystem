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
