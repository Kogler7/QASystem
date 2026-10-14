"""Public config works without Docker, including the packaged host entry."""
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def delivered_source(tmp_path):
    deploy = tmp_path/'delivery'
    source = deploy/'runtime'
    source.mkdir(parents=True)
    shutil.copy2(ROOT/'ops.py', deploy/'ops.py')
    shutil.copytree(ROOT/'qasystem_config', source/'qasystem_config', ignore=shutil.ignore_patterns('__pycache__'))
    (source/'docker').mkdir()
    for name in ('__init__.py', 'ops.py', 'configure.py', 'setup_llm.py', 'terminal_ui.py',
                 'model_services.py', 'api_ui.py', 'download_llm_models.py', 'llm-models.json'):
        shutil.copy2(ROOT/'docker'/name, source/'docker'/name)
    return deploy, source


def test_packaged_ops_help_runs_outside_repository_without_daemon(tmp_path):
    deploy, _ = delivered_source(tmp_path)
    caller = tmp_path/'unrelated'
    caller.mkdir()
    # An empty Docker endpoint is not used by help or public config imports.
    result = subprocess.run([sys.executable, str(deploy/'ops.py'), '--help'], cwd=caller,
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'config' in result.stdout and 'runtime' in result.stdout


def test_public_config_has_no_docker_or_subprocess_dependency(tmp_path):
    _, source = delivered_source(tmp_path)
    caller = tmp_path/'unrelated'
    caller.mkdir()
    code = '''import builtins,subprocess,sys
sys.path.insert(0,sys.argv[1])
original=builtins.__import__
def imports(name,*args,**kwargs):
 if name=='docker' or name.startswith('docker.'):
  raise AssertionError('public configuration imported Docker tools')
 return original(name,*args,**kwargs)
builtins.__import__=imports
def forbidden(*args,**kwargs): raise AssertionError('public configuration launched a process')
subprocess.run=forbidden
subprocess.Popen=forbidden
from qasystem_config import configuration,runtime_config,model_registry,io
assert configuration.declarations()['items']
assert isinstance(configuration.read_registry(),dict)
assert io.read_env('TEST_VALUE=literal')['TEST_VALUE']=='literal'
assert model_registry.service_name('test-service')
print('public configuration available')
'''
    result = subprocess.run([sys.executable, '-c', code, str(source)], cwd=caller,
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'public configuration available'


def test_delivery_keeps_only_historical_config_files_in_docker():
    script = (ROOT/'package-deploy.ps1').read_text(encoding='utf-8-sig')
    assert '"qasystem_config"' in script and '"model_runtime"' in script
    assert '-m qasystem_config.config_contract' in script
    for name in ('configuration.py', 'runtime_config.py', 'config_guard.py',
                 'config_contract.py', 'config_transaction.py', 'api_services.py'):
        assert not (ROOT/'docker'/name).exists()
    # Pre-migration releases are immutable evidence, not a second runtime entry.
    assert (ROOT/'docker/config-releases/2026.10.14.11.json').is_file()
