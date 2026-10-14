"""Fixed process and directory conventions, versioned with source releases."""
from pathlib import Path

RUNTIME_DIRECTORY_NAME = 'runtime-config'
PROCESS_ENVIRONMENT = {
    'ENVIRONMENT': 'production',
    'PYTHONPATH': '/app:/app/agent',
    'PYTHONUNBUFFERED': '1',
}
EXECUTION_LIVENESS = {
    'heartbeat_seconds': 5,
    'heartbeat_ttl_seconds': 30,
    'lease_ttl_seconds': 900,
    'renewal_max_interval_seconds': 5,
}
EXECUTION_TOPOLOGY = {
    'mode': 'async',
    'redis_database': 1,
    'data_directory': '/app/backend/data/execution',
    'runtime_ready_file': '/tmp/qasystem-runtime-ready',
    'reaper_enabled': True,
    'reaper_interval_seconds': 60,
}


def execution_data_directory():
    if in_container_runtime():
        return Path(EXECUTION_TOPOLOGY['data_directory'])
    return Path(__file__).resolve().parents[1] / 'backend/data/execution'


def runtime_ready_file():
    return Path(EXECUTION_TOPOLOGY['runtime_ready_file'])


def runtime_directory(root):
    return Path(root).resolve() / RUNTIME_DIRECTORY_NAME


def in_container_runtime():
    return (Path('/.dockerenv').is_file() or Path('/run/.containerenv').is_file()
            or Path('/app/runtime-config').is_dir())


def internal_redis_url(database):
    """Deployment Redis topology; local development uses the exposed loopback port."""
    host = 'redis' if in_container_runtime() else 'localhost'
    return f'redis://{host}:6379/{int(database)}'


INTERNAL_MCP_BIND = '0.0.0.0'
INTERNAL_MCP_PORT = 8890
INTERNAL_AGENT_MCP_PORT = 9181
SERVICE_TOPOLOGY = {
    'mcp_bind': INTERNAL_MCP_BIND, 'mcp_port': INTERNAL_MCP_PORT,
    'agent_mcp_port': INTERNAL_AGENT_MCP_PORT, 'agent_mcp_host': 'agent-mcp',
    'agent_host': 'agent', 'agent_port': 9180,
}


def internal_agent_mcp_url():
    host = SERVICE_TOPOLOGY['agent_mcp_host'] if in_container_runtime() else '127.0.0.1'
    return f'http://{host}:{INTERNAL_AGENT_MCP_PORT}/mcp'


def internal_agent_url():
    host = SERVICE_TOPOLOGY['agent_host'] if in_container_runtime() else '127.0.0.1'
    return f"http://{host}:{SERVICE_TOPOLOGY['agent_port']}"
