"""Static configuration contract gate (does not approve or regenerate declarations)."""
from __future__ import annotations

import ast
import json
from functools import lru_cache
from pathlib import Path
import re
import subprocess

APPLICATION_DIRS = ('backend', 'agent', 'algorithm', 'external_services', 'clef_service', 'nanojev_service', 'mcp_services', 'agent_mcp')


READERS = {'config_getenv','config_required','getenv','setting','_env','_flag','_env_int','_env_float','_bounded_int'}


def reader_names(tree):
    names = set(READERS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {'docker.configuration', 'algorithm.runtime_settings'}:
            names.update(alias.asname or alias.name for alias in node.names if alias.name in {'getenv','required','startup_getenv'})
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {'docker.configuration', 'algorithm.runtime_settings'}:
                    prefix = alias.asname or alias.name
                    names.update(prefix + '.' + member for member in ('getenv','required','startup_getenv'))
    return names


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
    catalog = json.loads((root / 'docker/config-declarations.json').read_text(encoding='utf-8'))
    errors = []
    if not catalog.get('version'):
        errors.append('配置声明缺少版本')
    actual_readers = reader_contracts(root)
    expected_readers = catalog.get('reader_contracts', {})
    for name in sorted(set(actual_readers) | set(expected_readers)):
        if actual_readers.get(name) != expected_readers.get(name):
            errors.append(f'{name} 配置读取或默认回退变化，必须登记配置声明新版本')
    release = root / 'docker/config-releases' / (str(catalog.get('version')) + '.json')
    if not release.is_file() or json.loads(release.read_text(encoding='utf-8')) != catalog:
        errors.append('当前声明与不可变版本记录不一致；修改声明必须登记新版本')
    if (root / '.git').exists():
        # Historical release files tracked by Git cannot be revised in place.
        changed = subprocess.run(['git', 'diff', 'HEAD', '--name-only', '--diff-filter=DMR', '--', 'docker/config-releases'],
                                 cwd=root, capture_output=True, text=True, timeout=10)
        if changed.returncode == 0 and changed.stdout.strip():
            errors.append('禁止修改既有声明版本文件；请新增版本文件')
    from docker.runtime_config import HOT_KEYS
    from docker.runtime_config import MODEL_DEFAULTS, MODEL_DESCRIPTIONS
    from docker.config_migrations import ALIASES, MODEL_KEYS
    from docker.startup_contract import PROCESS_ENVIRONMENT, RUNTIME_DIRECTORY_NAME, EXECUTION_LIVENESS
    if catalog.get('schema') == 1:
        if catalog.get('aliases') != ALIASES:
            errors.append('别名迁移规则与当前版本声明不一致')
        if catalog.get('model_defaults') != MODEL_DEFAULTS:
            errors.append('模型配置默认值变化，必须登记配置声明新版本')
        if catalog.get('model_descriptions') != MODEL_DESCRIPTIONS:
            errors.append('模型配置说明变化，必须登记配置声明新版本')
        if catalog.get('model_migrations') != {**MODEL_KEYS, 'LLM_SERVED_MODEL_NAME': 'models.<repo>'}:
            errors.append('模型配置迁移规则与当前版本声明不一致')
        if catalog.get('fixed_contracts') != {'process_environment': PROCESS_ENVIRONMENT, 'runtime_directory': RUNTIME_DIRECTORY_NAME, 'execution_liveness': EXECUTION_LIVENESS}:
            errors.append('固定启动约定变化，必须登记配置声明新版本')
        if 'inference_contract' in catalog:
            from docker.model_services import REGISTRY_CONTRACT
            from docker.config_migrations import NATIVE_STARTUP_KEYS, WORKER_STARTUP_KEYS
            actual = {**REGISTRY_CONTRACT, 'native_migration': sorted(NATIVE_STARTUP_KEYS), 'worker_migration': sorted(WORKER_STARTUP_KEYS)}
            if catalog['inference_contract'] != actual:
                errors.append('模型引擎/记录/迁移契约变化，必须登记配置声明新版本')
    from docker.api_services import API_REGISTRY_CONTRACT
    if catalog.get('schema') == 1 and catalog.get('api_registry_contract') != API_REGISTRY_CONTRACT:
        errors.append('API服务/账号登记规则变化，必须登记新配置声明版本')
    for key, item in catalog['items'].items():
        if item.get('mode') != ('hot' if key in HOT_KEYS else 'startup'):
            errors.append(f'{key} 热更新属性与实际读取规则不一致')
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
    for path in files(root):
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        sources[path.relative_to(root).as_posix()] = tree
        aliases = {'os'}
        names = reader_names(tree)
        source_readers[path.relative_to(root).as_posix()] = names
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                aliases.update(alias.asname or 'os' for alias in node.names if alias.name == 'os')
            if isinstance(node, ast.ImportFrom) and node.module == 'os' and any(n.name in {'getenv', 'environ'} for n in node.names):
                errors.append(f'{path.relative_to(root)}:{node.lineno} 禁止直接导入环境读取接口')
            raw = False
            if isinstance(node, ast.Call):
                function = ast.unparse(node.func)
                raw = any(function in {f'{name}.getenv', f'{name}.environ.get'} for name in aliases)
                if function in names:
                    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                        key = node.args[0].value
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
                found |= consumer['token'] in path.read_text(encoding='utf-8-sig')
            else:
                tree = sources.get(consumer['path'])
                if tree:
                    if consumer['kind'] == 'read':
                        found |= any(isinstance(node, ast.Call) and
                            ast.unparse(node.func) in source_readers[consumer['path']] and
                            any(isinstance(arg, ast.Constant) and arg.value == consumer['token'] for arg in node.args)
                            for node in ast.walk(tree))
                    else:
                        found |= any(isinstance(node, ast.Constant) and node.value == consumer['token'] for node in ast.walk(tree))
        if not found:
            errors.append(f'{key} 已失去全部消费者，请删除或迁移声明')
    return errors


@lru_cache(maxsize=8)
def _checked(root, signature, deployed):
    return tuple(check(Path(root), deployed=deployed))


def require_contract(root):
    root = Path(root)
    paths = list(files(root)) + list((root / 'docker/config-releases').glob('*.json'))
    paths += [root / '.env.example', root / 'docker/config-declarations.json']
    signature = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in sorted(paths) if path.is_file())
    return _checked(str(root), signature, (root / 'SOURCE_COMMIT').is_file())


if __name__ == '__main__':
    errors = check(Path(__file__).resolve().parents[1])
    print('\n'.join(errors) if errors else '配置声明与消费者门禁通过')
    raise SystemExit(bool(errors))
