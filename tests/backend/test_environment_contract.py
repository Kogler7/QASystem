import ast

import pytest

from qasystem_config.environment_contract import environment_errors, scan_environment_access, central_reader_names


@pytest.mark.parametrize('code', [
    'import os\nx = os.getenv("K")',
    'import os as system\nx = system.environ.get("K")',
    'import os\nx = os.environ["K"]',
    'from os import getenv as read\nx = read("K")',
    'import os\nos.environ["K"] = "v"',
    'import os\ndel os.environ["K"]',
    'import os\nos.environ.update(K="v")',
    'import os\nos.environ.setdefault("K", "v")',
    'import os\nos.environ.pop("K")',
    'import os\nos.environ.clear()',
    'import os\nos.putenv("K", "v")',
    'import os\nos.unsetenv("K")',
    'import os\nenv = os.environ\nenv.get("K")',
    'import os\nread = os.getenv\nread("K")',
    'from os import environ as environment\nf = environment.pop\nf("K")',
    'from dotenv import load_dotenv as load\nload()',
    'import dotenv as dot\nloader = dot.load_dotenv\nloader()',
    'import os\nhelper(os.environ)',
    'import os\nfor key in os.environ: pass',
    'import os\ngetattr(os,"environ")["K"]="v"',
    'import os\ngetattr(os,"getenv")("K")',
    'from os import *',
])
def test_environment_bypasses_are_rejected(code):
    assert environment_errors('backend/business.py', ast.parse(code))


def test_central_getter_and_local_dict_do_not_touch_environment():
    source = 'from qasystem_config.configuration import getenv\nx=getenv("K")\nenv={"K":"x"}\nenv["K"]="y"\nx=env.get("K")'
    assert not environment_errors('backend/business.py', source)


def test_gpu_binding_is_only_allowed_for_exact_path_function_and_keys():
    source = 'import os\ndef _bind_gpu(device):\n os.environ["CUDA_VISIBLE_DEVICES"] = device\n os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"'
    assert not environment_errors('backend/app/execution/worker.py', source)
    assert environment_errors('backend/business.py', source)
    assert environment_errors('backend/app/execution/worker.py', source.replace('_bind_gpu', 'do_work'))
    assert environment_errors('backend/app/execution/worker.py', source.replace('CUDA_VISIBLE_DEVICES', 'API_KEY'))
    assert environment_errors('backend/app/execution/worker.py', source.replace('"CUDA_VISIBLE_DEVICES"', 'device'))


def test_startup_copy_does_not_permit_reads_or_later_business_writes():
    source = 'import os\nclass RuntimeSupervisor:\n def _spawn(self):\n  env=os.environ.copy()\n  env["K"]="value"'
    path = 'backend/app/execution/supervisor.py'
    assert not environment_errors(path, source)
    assert environment_errors(path, source + '\n  secret=os.getenv("K")')
    assert environment_errors(path, source + '\n  os.environ["K"]="value"')
    assert environment_errors(path, source + '\n  secret=env.get("API_KEY")')
    assert environment_errors(path, source + '\n  secret=env["API_KEY"]')


def test_mcp_merge_only_permits_process_inheritance_in_exact_helper():
    source = 'import os\ndef _merge_stdio_env(env):\n return {**os.environ, **(env or {})}'
    assert not environment_errors('agent/agentcode/tools/mcp_tools/mcp.py', source)
    assert environment_errors('agent/agentcode/tools/mcp_tools/mcp.py', source.replace('_merge_stdio_env', 'get_config'))


def test_scope_limited_exception_does_not_allow_nested_business_function():
    source = 'import os\ndef _bind_gpu(device):\n def business():\n  os.environ["CUDA_VISIBLE_DEVICES"] = device'
    assert environment_errors('backend/app/execution/worker.py', source)


def test_source_access_has_traceable_operation_and_function():
    source = 'import os\nclass Worker:\n def run(self):\n  os.environ["K"]="v"'
    access, = scan_environment_access(source)
    assert (access.line, access.function, access.operation, access.keys) == (4, 'Worker.run', 'write', ('K',))


@pytest.mark.parametrize(('source', 'function'), [
    ('from qasystem_config import configuration as cfg\nx=cfg.getenv("K")', 'cfg.getenv'),
    ('import qasystem_config.configuration as cfg\nx=cfg.required("K")', 'cfg.required'),
    ('import qasystem_config as d\nx=d.configuration.getenv("K")', 'd.configuration.getenv'),
    ('from qasystem_config.configuration import getenv as cfg\nread=cfg\nf=read\nx=f("K")', 'f'),
    ('from qasystem_config.configuration import startup_getenv\nread:object=startup_getenv\nx=read("K")', 'read'),
])
def test_central_import_and_assignment_aliases_are_registered(source, function):
    assert function in central_reader_names(ast.parse(source))


def test_unrelated_getenv_is_not_registered_as_central():
    assert 'client.getenv' not in central_reader_names(ast.parse('x=client.getenv("K")'))


@pytest.mark.parametrize('code', [
    'from qasystem_config.configuration import prepare_dev_agent_startup\nprepare_dev_agent_startup(8)',
    'from qasystem_config.configuration import bootstrap_local_environment\nbootstrap_local_environment()',
    'from qasystem_config import configuration as cfg\ncfg.configure_mineru_loader_environment("x", "local")',
])
def test_controlled_environment_helpers_cannot_be_called_by_business_code(code):
    assert environment_errors('backend/business.py', code)


def test_controlled_startup_and_dependency_calls_require_exact_consumers():
    source = 'from qasystem_config.configuration import prepare_dev_agent_startup as prepare\ndef main():\n prepare(8)'
    assert not environment_errors('agent/agentcode/api.py', source)
    assert environment_errors('agent/agentcode/api.py', source.replace('main', 'handle_request'))
    source = 'from qasystem_config.configuration import configure_mineru_loader_environment as setup\nclass OCRProcessor:\n def __init__(self):\n  setup("x", "local")'
    assert not environment_errors('algorithm/preproc/preproc_src/processors/ocr_processor.py', source)
    assert environment_errors('algorithm/preproc/preproc_src/processors/ocr_processor.py', source.replace('__init__', 'process'))
