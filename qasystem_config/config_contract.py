"""Static configuration contract gate (does not approve or regenerate declarations)."""
from __future__ import annotations

import ast
import json
from functools import lru_cache
from pathlib import Path
import re
import subprocess
from qasystem_config.environment_contract import central_reader_names, environment_errors, ENVIRONMENT_ACCESS_CONTRACT

APPLICATION_DIRS = ('backend', 'agent', 'algorithm', 'external_services', 'clef_service', 'nanojev_service', 'mcp_services', 'agent_mcp', 'model_runtime')
HISTORY_DIRECTORIES = ('docker/config-releases', 'qasystem_config/config-releases')
CORE_CONTRACT = {
    'package': 'qasystem_config', 'declaration': 'qasystem_config/config-declarations.json',
    'history_directories': list(HISTORY_DIRECTORIES),
    'forbidden_core_dependencies': ['docker', *APPLICATION_DIRS],
    'business_configuration_owner': 'qasystem_config', 'deployment_owner': 'docker',
    'io_owner': 'qasystem_config.io', 'native_entrypoint': 'model_runtime.native',
}


READERS = {'config_getenv','config_required','getenv','required','startup_getenv','setting','_env','_flag','_env_int','_env_float','_bounded_int'}


def reader_names(tree):
    return central_reader_names(tree, READERS)


def central_startup_contracts():
    source = Path(__file__).with_name('configuration.py')
    names = {'should_load_local_dotenv', 'bootstrap_local_environment',
             'prepare_dev_agent_startup', 'configure_mineru_loader_environment',
             'getenv', 'startup_getenv', 'approved_execution_plan'}
    return {node.name: ast.unparse(node) for node in ast.parse(source.read_text(encoding='utf-8-sig')).body
            if isinstance(node, ast.FunctionDef) and node.name in names}


def historical_release_errors(root):
    """Verify historical Git blobs, including already committed rewrites/deletes."""
    root = Path(root)
    if not (root / '.git').exists():
        return []
    log = subprocess.run(['git', 'log', 'HEAD', '--first-parent', '--diff-filter=A', '--format=COMMIT:%H',
                          '--name-only', '--', *HISTORY_DIRECTORIES], cwd=root,
                         capture_output=True, text=True, timeout=30)
    if log.returncode != 0:
        # A newly initialized fixture repository may have no commits. Production
        # workspaces with HEAD must never silently disable the historical check.
        head = subprocess.run(['git', 'rev-parse', '--verify', 'HEAD'], cwd=root,
                              capture_output=True, text=True, timeout=10)
        return [] if head.returncode != 0 else ['历史声明Git记录不可读取，拒绝跳过核验']
    originals = {}
    commit = None
    for line in log.stdout.splitlines():
        if line.startswith('COMMIT:'):
            commit = line.split(':', 1)[1]
        elif any(line.startswith(directory + '/') for directory in HISTORY_DIRECTORIES) and line.endswith('.json'):
            originals.setdefault(line, set()).add(commit)
    errors = []
    for relative, commits in sorted(originals.items()):
        path = root / relative
        if not path.is_file():
            errors.append(f'禁止删除既有声明版本文件：{relative}')
            continue
        current = subprocess.run(['git', 'hash-object', '--path', relative, str(path)], cwd=root,
                                 capture_output=True, text=True, timeout=10)
        if current.returncode != 0:
            errors.append(f'历史声明当前文件不可核验：{relative}')
            continue
        for commit in commits:
            previous = subprocess.run(['git', 'rev-parse', f'{commit}:{relative}'], cwd=root,
                                      capture_output=True, text=True, timeout=10)
            if previous.returncode != 0 or previous.stdout.strip() != current.stdout.strip():
                errors.append(f'禁止修改既有声明版本文件（包含已提交改写）：{relative}')
                break
    return errors


def reader_contracts(root):
    contracts = {}
    for path in files(root):
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        names = reader_names(tree)
        expressions = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) in names:
                expressions.append(ast.unparse(node))
            if isinstance(node, ast.ClassDef) and node.name == 'Settings':
                expressions.append(ast.unparse(node))
        if expressions:
            contracts[path.relative_to(root).as_posix()] = expressions
    return contracts


def git_ignored(root, paths):
    """被 .gitignore 忽略的路径不参与门禁：它们是本地实验副本，不进 Git 与交付包。"""
    rel = sorted({p.relative_to(root).as_posix() for p in paths})
    if not rel or not (root / '.git').exists():
        return set()
    done = subprocess.run(['git', 'check-ignore', '-z', '--stdin'],
                          cwd=root, input='\0'.join(rel), capture_output=True, text=True, timeout=30)
    if done.returncode not in (0, 1):
        return set()
    return {entry for entry in done.stdout.split('\0') if entry}


def files(root):
    root = Path(root)
    candidates = [path for directory in APPLICATION_DIRS
                  for path in (root / directory).rglob('*.py')
                  if 'tests' not in path.relative_to(root).parts]
    ignored = git_ignored(root, candidates)
    for path in candidates:
        if path.relative_to(root).as_posix() not in ignored:
            yield path


def check(root, *, deployed=False):
    root = Path(root)
    catalog = json.loads((root / 'qasystem_config/config-declarations.json').read_text(encoding='utf-8'))
    errors = []
    if not catalog.get('version'):
        errors.append('配置声明缺少版本')
    actual_readers = reader_contracts(root)
    expected_readers = catalog.get('reader_contracts', {})
    for name in sorted(set(actual_readers) | set(expected_readers)):
        if actual_readers.get(name) != expected_readers.get(name):
            errors.append(f'{name} 配置读取或默认回退变化，必须登记配置声明新版本')
    release = root / 'qasystem_config/config-releases' / (str(catalog.get('version')) + '.json')
    if not release.is_file() or json.loads(release.read_text(encoding='utf-8')) != catalog:
        errors.append('当前声明与不可变版本记录不一致；修改声明必须登记新版本')
    if (root / '.git').exists():
        # Historical release files tracked by Git cannot be revised in place.
        changed = subprocess.run(['git', 'diff', 'HEAD', '--name-only', '--diff-filter=DMR', '--', *HISTORY_DIRECTORIES],
                                 cwd=root, capture_output=True, text=True, timeout=10)
        if changed.returncode == 0 and changed.stdout.strip():
            errors.append('禁止修改既有声明版本文件；请新增版本文件')
        errors.extend(historical_release_errors(root))
    from qasystem_config.runtime_config import HOT_KEYS
    from qasystem_config.runtime_config import MODEL_DEFAULTS, MODEL_DESCRIPTIONS
    from qasystem_config.config_migrations import ALIASES, MODEL_KEYS
    from qasystem_config.startup_contract import PROCESS_ENVIRONMENT, RUNTIME_DIRECTORY_NAME, EXECUTION_LIVENESS, EXECUTION_TOPOLOGY, SERVICE_TOPOLOGY
    if catalog.get('schema') == 1:
        if catalog.get('core_contract') != CORE_CONTRACT:
            errors.append('公共配置核心边界变化，必须登记新声明版本')
        if catalog.get('environment_access_contract') != ENVIRONMENT_ACCESS_CONTRACT:
            errors.append('进程环境访问/受控启动接口合同变化，必须登记配置声明新版本')
        if catalog.get('central_startup_contracts') != central_startup_contracts():
            errors.append('集中启动环境适配规则变化，必须登记配置声明新版本')
        if catalog.get('aliases') != ALIASES:
            errors.append('别名迁移规则与当前版本声明不一致')
        if catalog.get('model_defaults') != MODEL_DEFAULTS:
            errors.append('模型配置默认值变化，必须登记配置声明新版本')
        if catalog.get('model_descriptions') != MODEL_DESCRIPTIONS:
            errors.append('模型配置说明变化，必须登记配置声明新版本')
        if catalog.get('model_migrations') != {**MODEL_KEYS, 'LLM_SERVED_MODEL_NAME': 'models.<repo>'}:
            errors.append('模型配置迁移规则与当前版本声明不一致')
        if catalog.get('fixed_contracts') != {'process_environment': PROCESS_ENVIRONMENT, 'runtime_directory': RUNTIME_DIRECTORY_NAME, 'execution_liveness': EXECUTION_LIVENESS, 'execution_topology': EXECUTION_TOPOLOGY, 'service_topology': SERVICE_TOPOLOGY}:
            errors.append('固定启动约定变化，必须登记配置声明新版本')
        if 'inference_contract' in catalog:
            from qasystem_config.model_registry import REGISTRY_CONTRACT
            from qasystem_config.config_migrations import NATIVE_STARTUP_KEYS, WORKER_STARTUP_KEYS
            actual = {**REGISTRY_CONTRACT, 'native_migration': sorted(NATIVE_STARTUP_KEYS), 'worker_migration': sorted(WORKER_STARTUP_KEYS)}
            if catalog['inference_contract'] != actual:
                errors.append('模型引擎/记录/迁移契约变化，必须登记配置声明新版本')
    from qasystem_config.api_services import API_REGISTRY_CONTRACT
    if catalog.get('schema') == 1 and catalog.get('api_registry_contract') != API_REGISTRY_CONTRACT:
        errors.append('API服务/账号登记规则变化，必须登记新配置声明版本')
    for key, item in catalog['items'].items():
        from qasystem_config.runtime_config import PROCESS_KEYS
        expected_mode = 'processes' if key in PROCESS_KEYS else 'hot' if key in HOT_KEYS else 'startup'
        if item.get('mode') != expected_mode:
            errors.append(f'{key} 热更新属性与实际读取规则不一致')
        if key in PROCESS_KEYS and item.get('source') != 'runtime':
            errors.append(f'{key} 进程计划必须使用批准配置文件作为唯一来源')
    template = root / '.env.example'
    if template.is_file():
        for line in template.read_text(encoding='utf-8-sig').splitlines():
            if '=' not in line or line.startswith('#'):
                continue
            key, value = line.split('=', 1)
            if key not in catalog['items'] or catalog['items'][key]['default'] != value:
                errors.append(f'{key} 模板与版本声明的默认值不一致')
    sources = {}
    source_readers = {}
    for path in (root / 'qasystem_config').glob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            imports = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
            if any(name.split('.')[0] in CORE_CONTRACT['forbidden_core_dependencies'] for name in imports):
                errors.append(f'{path.relative_to(root)}:{node.lineno} 公共配置核心禁止依赖部署或业务模块')
    for path in files(root):
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        sources[path.relative_to(root).as_posix()] = tree
        aliases = {'os'}
        names = reader_names(tree)
        source_readers[path.relative_to(root).as_posix()] = names
        errors.extend(environment_errors(path.relative_to(root).as_posix(), tree))
        for node in ast.walk(tree):
            imports = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
            if any(name == 'docker' or name.startswith('docker.') for name in imports):
                errors.append(f'{path.relative_to(root)}:{node.lineno} 业务模块禁止依赖Docker配置或运维层')
            if 'algorithm.runtime_settings' in imports:
                errors.append(f'{path.relative_to(root)}:{node.lineno} 禁止恢复旧配置转发层，请直接使用公共配置接口')
            if isinstance(node, ast.Import):
                aliases.update(alias.asname or 'os' for alias in node.names if alias.name == 'os')
            if isinstance(node, ast.ImportFrom) and node.module == 'os' and any(n.name in {'getenv', 'environ'} for n in node.names):
                errors.append(f'{path.relative_to(root)}:{node.lineno} 禁止直接导入环境读取接口')
            raw = False
            if isinstance(node, ast.Call):
                function = ast.unparse(node.func)
                raw = any(function in {f'{name}.getenv', f'{name}.environ.get'} for name in aliases)
                if function in names:
                    argument = node.args[0] if node.args else next(
                        (kw.value for kw in node.keywords if kw.arg == 'key'), None)
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        key = argument.value
                        if key not in catalog['items']:
                            errors.append(f'{path.relative_to(root)}:{node.lineno} 未声明配置 {key}')
            elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
                raw = ast.unparse(node.value) in {f'{name}.environ' for name in aliases}
            if raw:
                errors.append(f'{path.relative_to(root)}:{node.lineno} 禁止绕过集中配置模块')
    for key, item in catalog['items'].items():
        if not item.get('desc') or not item.get('consumers') or 'default' not in item:
            errors.append(f'{key} 声明缺少说明、默认值或消费者')
            continue
        found = False
        for consumer in item['consumers']:
            consumer_found = False
            path = root / consumer['path']
            if not path.is_file():
                # External tooling (e.g. eval) is intentionally not delivered as
                # runtime source. Its contract is checked before packaging.
                if deployed and consumer['kind'] == 'external':
                    found = True
                if not deployed or consumer['kind'] != 'external':
                    errors.append(f"{key} 消费者路径不存在：{consumer['path']}")
                continue
            if consumer['kind'] == 'external':
                consumer_found = consumer['token'] in path.read_text(encoding='utf-8-sig')
            else:
                tree = sources.get(consumer['path'])
                if tree is None and path.suffix == '.py':
                    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
                    source_readers[consumer['path']] = reader_names(tree)
                if tree:
                    if consumer['kind'] == 'read':
                        consumer_found = any(isinstance(node, ast.Call) and
                            ast.unparse(node.func) in source_readers[consumer['path']] and
                            any(isinstance(arg, ast.Constant) and arg.value == consumer['token']
                                for arg in [*node.args, *(kw.value for kw in node.keywords if kw.arg == 'key')])
                            for node in ast.walk(tree))
                    else:
                        consumer_found = any(isinstance(node, ast.Constant) and node.value == consumer['token'] for node in ast.walk(tree))
            if not consumer_found:
                errors.append(f"{key} 消费者已失效：{consumer['path']} ({consumer['kind']})")
            found |= consumer_found
        if not found:
            errors.append(f'{key} 已失去全部消费者，请删除或迁移声明')
    return errors


@lru_cache(maxsize=8)
def _checked(root, signature, deployed):
    return tuple(check(Path(root), deployed=deployed))


def require_contract(root):
    root = Path(root)
    paths = list(files(root)) + [path for directory in HISTORY_DIRECTORIES for path in (root / directory).glob('*.json')]
    paths += [root / '.env.example', root / 'qasystem_config/config-declarations.json']
    paths += list((root / 'qasystem_config').glob('*.py'))
    signature = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in sorted(paths) if path.is_file())
    return _checked(str(root), signature, (root / 'SOURCE_COMMIT').is_file())


if __name__ == '__main__':
    errors = check(Path(__file__).resolve().parents[1])
    print('\n'.join(errors) if errors else '配置声明与消费者门禁通过')
    raise SystemExit(bool(errors))
