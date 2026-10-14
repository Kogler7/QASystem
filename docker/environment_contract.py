"""Static checks for direct process-environment access in application source.

This detects conventional imports, aliases and mutations. It is a source quality
gate, not a Python sandbox: reflective or dynamically generated code requires
review. Exceptions describe specific process-startup operations, not whole files.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True)
class EnvironmentAccess:
    line: int
    function: str
    operation: str
    keys: tuple[str, ...] | None = None


def _rule(path, function, operation, keys, description):
    return {'path': path, 'function': function, 'operation': operation,
            'keys': sorted(keys) if keys is not None else None, 'desc': description}


_CUDA = ('CUDA_DEVICE_ORDER', 'CUDA_VISIBLE_DEVICES')
_OFFLINE = ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'HF_HUB_DISABLE_TELEMETRY')
_CONTROLLED_FUNCTIONS = ('bootstrap_local_environment', 'prepare_dev_agent_startup',
                         'configure_mineru_loader_environment')

# Process startup is the only application-side reason to change process ENV.
# Keep this contract versioned when integrated into config-declarations.json.
PROCESS_ENVIRONMENT_ACCESS_CONTRACT = [
    _rule('backend/app/execution/worker.py', '_bind_gpu', 'write', _CUDA,
          'Bind the worker GPU before importing CUDA dependencies.'),
    _rule('backend/app/execution/worker.py', 'WorkerRuntime.__init__', 'write',
          ('EXECUTION_WORKER_ID', 'EXECUTION_WORKER_INSTANCE_ID'),
          'Publish the newly created worker process identity.'),
    _rule('clef_service/app.py', 'LocalRuntime.load', 'write', _CUDA + _OFFLINE,
          'Set model-process GPU isolation and offline dependency settings.'),
    _rule('nanojev_service/app.py', 'NanoRuntime.load', 'write', _CUDA + _OFFLINE,
          'Set model-process GPU isolation and offline dependency settings.'),
    *[_rule('backend/app/execution/supervisor.py', 'RuntimeSupervisor.' + function,
            'snapshot', None, 'Copy process ENV for a managed child process.')
      for function in ('_spawn', '_start_engines', '_wait_until_warm',
                       '_worker_env', '_start_core_workers', '_spawn_cpu_worker')],
    _rule('algorithm/preproc/preproc_src/processors/mineru4_processor.py',
          'Mineru4Processor._run_in_subprocess', 'snapshot', None,
          'Copy process ENV into the isolated parsing child process.'),
    _rule('agent/agentcode/tools/mcp_tools/mcp.py', '_merge_stdio_env', 'snapshot', None,
          'Inherit parent process ENV when launching an external stdio tool.'),
]

CONTROLLED_ENVIRONMENT_CALL_CONTRACT = [
    _rule('backend/app/environment.py', 'configure_runtime_environment',
          'controlled.bootstrap_local_environment', None, 'Unmanaged Backend development startup only.'),
    _rule('agent/agentcode/adapter.py', '_load_env',
          'controlled.bootstrap_local_environment', None, 'Unmanaged Agent development startup only.'),
    _rule('algorithm/ans/QA/config.py', 'Config.load',
          'controlled.bootstrap_local_environment', None, 'Unmanaged standalone graph-tool development startup only.'),
    _rule('agent/agentcode/api.py', 'main', 'controlled.prepare_dev_agent_startup', None,
          'Validated explicit development concurrency, never a managed deployment override.'),
    _rule('algorithm/preproc/preproc_src/processors/ocr_processor.py', 'OCRProcessor.__init__',
          'controlled.configure_mineru_loader_environment', None,
          'Forward the three registered MinerU loader dependency parameters at initialization.'),
]

ENVIRONMENT_ACCESS_CONTRACT = PROCESS_ENVIRONMENT_ACCESS_CONTRACT + CONTROLLED_ENVIRONMENT_CALL_CONTRACT


class _Scanner(ast.NodeVisitor):
    def __init__(self, tree):
        self.parents = {child: parent for parent in ast.walk(tree)
                        for child in ast.iter_child_nodes(parent)}
        self.bindings = {}
        self.accesses = []
        # A conservative fixed-point symbol table recognizes imported and
        # assigned aliases irrespective of traversal order. An alias capture is
        # itself forbidden, so an escaping alias cannot silently bypass checks.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for entry in node.names:
                    if entry.name in ('os', 'dotenv'):
                        self.bindings[entry.asname or entry.name] = entry.name
                    elif entry.name == 'docker.configuration':
                        self.bindings[entry.asname or entry.name] = 'configuration'
                    elif entry.name == 'docker':
                        self.bindings[entry.asname or entry.name] = 'docker'
            elif isinstance(node, ast.ImportFrom) and node.module in ('os', 'dotenv'):
                for entry in node.names:
                    if entry.name in ('environ', 'getenv', 'putenv', 'unsetenv', 'load_dotenv'):
                        self.bindings[entry.asname or entry.name] = entry.name
                    elif entry.name == '*':
                        self.record(node, 'wildcard_import')
            elif isinstance(node, ast.ImportFrom) and node.module == 'docker.configuration':
                for entry in node.names:
                    if entry.name in _CONTROLLED_FUNCTIONS:
                        self.bindings[entry.asname or entry.name] = 'controlled.' + entry.name
            elif isinstance(node, ast.ImportFrom) and node.module == 'docker':
                for entry in node.names:
                    if entry.name == 'configuration':
                        self.bindings[entry.asname or entry.name] = 'configuration'
        for _ in range(16):
            before = dict(self.bindings)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                    capability = self.capability(node.value)
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if capability and isinstance(target, ast.Name):
                            self.bindings[target.id] = capability
            if before == self.bindings:
                break

    def capability(self, node):
        if isinstance(node, ast.Name):
            return self.bindings.get(node.id)
        if isinstance(node, ast.Attribute):
            parent = self.capability(node.value)
            if parent == 'os' and node.attr in ('environ', 'getenv', 'putenv', 'unsetenv'):
                return node.attr
            if parent == 'dotenv' and node.attr == 'load_dotenv':
                return 'load_dotenv'
            if parent == 'environ':
                return 'environ.' + node.attr
            if parent == 'environment_snapshot':
                return 'snapshot.' + node.attr
            if parent == 'docker' and node.attr == 'configuration':
                return 'configuration'
            if parent == 'configuration' and node.attr in _CONTROLLED_FUNCTIONS:
                return 'controlled.' + node.attr
        if isinstance(node, ast.Call) and self.capability(node.func) == 'environ.copy':
            return 'environment_snapshot'
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'getattr' and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)):
            owner, member = self.capability(node.args[0]), node.args[1].value
            if owner == 'os' and member in ('environ', 'getenv', 'putenv', 'unsetenv'):
                return member
            if owner == 'environ' and isinstance(member, str):
                return 'environ.' + member
            if owner == 'dotenv' and member == 'load_dotenv':
                return 'load_dotenv'
        return None

    def function(self, node):
        ancestors = []
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                ancestors.append(node.name)
        return '.'.join(reversed(ancestors)) or '<module>'

    def record(self, node, operation, keys=None):
        self.accesses.append(EnvironmentAccess(node.lineno, self.function(node), operation,
                                              tuple(keys) if keys is not None else None))

    @staticmethod
    def literal_key(node):
        return (node.value,) if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    def visit_Subscript(self, node):
        if self.capability(node.value) == 'environ':
            operation = {ast.Load: 'read', ast.Store: 'write', ast.Del: 'delete'}[type(node.ctx)]
            self.record(node, operation, self.literal_key(node.slice))
        elif self.capability(node.value) == 'environment_snapshot' and isinstance(node.ctx, ast.Load):
            self.record(node, 'snapshot_read', self.literal_key(node.slice))
        self.generic_visit(node)

    def visit_Call(self, node):
        capability = self.capability(node.func)
        key = self.literal_key(node.args[0]) if node.args else None
        if capability in ('getenv', 'environ.get', 'environ.__getitem__', 'environ.getitem'):
            self.record(node, 'read', key)
        elif capability in ('putenv', 'environ.__setitem__'):
            self.record(node, 'write', key)
        elif capability in ('unsetenv', 'environ.pop', 'environ.__delitem__'):
            self.record(node, 'delete', key)
        elif capability == 'environ.copy':
            self.record(node, 'snapshot')
        elif capability == 'load_dotenv':
            self.record(node, 'dotenv')
        elif capability and capability.startswith('controlled.'):
            self.record(node, capability)
        elif capability in ('snapshot.get', 'snapshot.items', 'snapshot.keys',
                            'snapshot.values', 'snapshot.__getitem__'):
            self.record(node, 'snapshot_read', key)
        elif capability and capability.startswith('environ.'):
            # Bulk writes and uncommon methods cannot use a per-key exception:
            # doing so would allow a second positional dict or **kwargs escape.
            self.record(node, 'environment_method')
        elif capability in ('environ',):
            self.record(node, 'expose')
        elif self.capability(node) in ('environ', 'getenv', 'putenv', 'unsetenv', 'load_dotenv'):
            self.record(node, 'expose')
        self.generic_visit(node)

    def visit_Dict(self, node):
        for key, value in zip(node.keys, node.values):
            if key is None and self.capability(value) == 'environ':
                self.record(value, 'snapshot')
        self.generic_visit(node)

    def _reference(self, node):
        capability = self.capability(node)
        if capability not in ('environ', 'getenv', 'putenv', 'unsetenv', 'load_dotenv') and not (
                capability and capability.startswith(('environ.', 'controlled.'))):
            return
        parent = self.parents.get(node)
        if isinstance(parent, ast.Attribute) and parent.value is node:
            return
        if isinstance(parent, ast.Call) and parent.func is node:
            return
        if isinstance(parent, ast.Subscript) and parent.value is node:
            return
        if isinstance(parent, ast.Dict) and any(k is None and v is node for k, v in zip(parent.keys, parent.values)):
            return
        self.record(node, 'expose')

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self._reference(node)

    def visit_Attribute(self, node):
        self._reference(node)
        self.generic_visit(node)


def scan_environment_access(tree):
    if isinstance(tree, str):
        tree = ast.parse(tree)
    scanner = _Scanner(tree)
    scanner.visit(tree)
    return sorted(set(scanner.accesses), key=lambda item: (item.line, item.operation, item.function))


def access_allowed(path, access, *, contract=None):
    contract = ENVIRONMENT_ACCESS_CONTRACT if contract is None else contract
    for entry in contract:
        if (path == entry['path'] and access.function == entry['function']
                and access.operation == entry['operation']):
            if entry['keys'] is None:
                return access.keys is None
            if access.keys and set(access.keys) <= set(entry['keys']):
                return True
    return False


def environment_errors(path, tree):
    return [f'{path}:{access.line} 禁止绕过集中配置模块读取或修改进程环境 '
            f'({access.operation}, {access.function})'
            for access in scan_environment_access(tree) if not access_allowed(path, access)]


def central_reader_names(tree, initial=()):
    """Recognize supported central imports and assigned getter aliases.

    The caller keeps its existing explicit wrapper contracts. This extends the
    declaration/default gate to aliases without treating unrelated '.getenv'
    methods as central configuration readers.
    """
    readers = set(initial)
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in ('docker.configuration', 'algorithm.runtime_settings'):
                readers.update(entry.asname or entry.name for entry in node.names
                               if entry.name in ('getenv', 'required', 'startup_getenv'))
            elif node.module == 'docker':
                modules.update(entry.asname or entry.name for entry in node.names
                               if entry.name == 'configuration')
        elif isinstance(node, ast.Import):
            for entry in node.names:
                if entry.name in ('docker.configuration', 'algorithm.runtime_settings'):
                    modules.add(entry.asname or entry.name)
                elif entry.name == 'docker':
                    modules.add((entry.asname or entry.name) + '.configuration')
    readers.update(module + '.' + member for module in modules
                   for member in ('getenv', 'required', 'startup_getenv'))
    for _ in range(16):
        before = set(readers)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and node.value:
                if ast.unparse(node.value) in readers:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    readers.update(target.id for target in targets if isinstance(target, ast.Name))
        if before == readers:
            break
    return readers
