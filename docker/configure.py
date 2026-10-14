"""Keyboard configuration editor. Opening it never starts or hashes models."""
from __future__ import annotations

import argparse
from datetime import datetime
import getpass
import json
import math
import os
from pathlib import Path
from copy import deepcopy
import re
import shutil
import shlex
import socket
import subprocess
import sys

try:
    from docker import setup_llm as helper
    from docker import terminal_ui as screen
    from docker.runtime_config import HOT_KEYS, LEGACY_MODEL_KEYS, read_document, read_settings
    from docker import config_guard as guard
except ImportError:
    import setup_llm as helper
    import terminal_ui as screen
    from runtime_config import HOT_KEYS, LEGACY_MODEL_KEYS, read_document, read_settings
    import config_guard as guard



def secret(key: str) -> bool:
    return any(word in key for word in ("KEY", "PASSWORD", "SECRET", "TOKEN", "DATABASE_URL", "REDIS_URL")) and not any(
        word in key for word in ("MAX_TOKENS", "INPUT_TOKENS", "OUTPUT_TOKENS", "BATCHED_TOKENS"))


def schema(text: str) -> list[dict]:
    """Use the release's .env.example as the single defaults/help catalog."""
    comments, rows, group = [], [], "通用"
    for line in text.splitlines():
        if not line.strip():
            comments = []
        elif line.startswith("#"):
            comments.append(line[1:].strip())
        elif re.match(r"^[A-Z][A-Z0-9_]*=", line):
            key = line.split("=", 1)[0]
            if comments:
                group = comments[0]
            default = helper.read_env(line)[key]
            rows.append(dict(key=key, default=default, description=" / ".join(comments) or f"{key}；当前版本默认值见下方",
                             group=group, hot=key in HOT_KEYS, secret=secret(key)))
            comments = []
    return rows


def slug(repo: str) -> str:
    return "llm_" + re.sub(r"[^a-z0-9]+", "_", repo.lower()).strip("_")


class Configuration:
    @staticmethod
    def process_environment():
        from docker.startup_contract import PROCESS_ENVIRONMENT
        return dict(PROCESS_ENVIRONMENT)

    MIGRATION_STATE = ('approved-config.json', 'history', 'versions', 'events', 'observed',
                       'applied-models.json', 'applied.compose.json')

    def legacy_snapshot(self):
        if self.input_directory == self.directory:
            return {}
        return {str(p.relative_to(self.input_directory)): guard.digest(p.read_bytes())
                for p in self.input_directory.rglob('*') if p.is_file()}

    def copy_legacy_runtime(self):
        """Explicit save copies private state; caller rolls back on any failed write."""
        copied = []
        if self.input_directory == self.directory:
            return copied
        if any((self.directory / name).exists() for name in self.MIGRATION_STATE):
            raise ValueError('目标配置目录已存在历史或实际状态；拒绝覆盖')
        try:
            for name in self.MIGRATION_STATE:
                source, target = self.input_directory / name, self.directory / name
                if not source.exists():
                    continue
                if target.exists():
                    raise ValueError('目标配置目录已存在历史或实际状态；拒绝覆盖')
                copied.append(target)
                if source.is_dir():
                    shutil.copytree(source, target)
                    target.chmod(0o700)
                else:
                    shutil.copy2(source, target)
                    target.chmod(0o600)
        except Exception:
            self.rollback_migration(copied)
            raise
        return copied

    @staticmethod
    def rollback_migration(copied):
        for target in reversed(copied):
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.catalog = json.loads(Path(__file__).with_name("llm-models.json").read_text(encoding="utf-8"))
        release_template = Path(__file__).resolve().parents[1] / '.env.example'
        self.template = (release_template if release_template.is_file() else root / '.env.example').read_text(encoding='utf-8-sig')
        self.fields = schema(self.template)
        self.original = (root / ".env").read_text(encoding="utf-8-sig") if (root / ".env").exists() else ""
        from docker.config_migrations import migrate, duplicate_conflicts
        original_values, self.migration_conflicts = migrate(helper.read_env(self.original))
        self.resolved_migrations = set()
        self.values = {**helper.read_env(self.template), **original_values}
        from docker.startup_contract import runtime_directory
        self.directory = runtime_directory(root)
        old_directory = Path(self.values.get('QASYSTEM_RUNTIME_CONFIG_DIR', './runtime-config'))
        self.input_directory = (root / old_directory).resolve()
        self.approval_directory = self.directory
        if self.input_directory != self.directory and self.input_directory.exists():
            if any((self.directory / name).exists() for name in ('config.json', 'settings.json', 'models.json', *self.MIGRATION_STATE)):
                raise ValueError('新旧配置目录同时含实际配置；拒绝自动合并或覆盖')
            self.approval_directory = self.input_directory
        else:
            self.input_directory = self.directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        hot = self.input_directory / "config.json"
        document = read_document(hot, migration=True)
        original_values, self.migration_conflicts = migrate({**helper.read_env(self.original), **document['settings']})
        self.migration_conflicts.update({key: old for key, old in duplicate_conflicts(self.original).items()
                                         if key in helper.read_env(self.template)})
        self.values.update(original_values)
        from docker.config_migrations import ALIASES
        for field in self.fields:
            old = [key for key, target in ALIASES.items() if target == field['key'] and key in helper.read_env(self.original)]
            if old:
                field['description'] += ' / 迁移：' + ', '.join(old) + ' → ' + field['key']
            if field['key'] in self.migration_conflicts:
                field['description'] += ' / [冲突] 请编辑本项，明确确认最终取值后才能保存'
        self.persisted_hot = {k: v for k, v in original_values.items() if k in HOT_KEYS}
        self.persisted_hot.update(document['settings'])
        self.initial_hot = {k: self.values[k] for k in HOT_KEYS if k in self.values}
        from docker.config_migrations import migrate_models
        self.models, model_conflicts = migrate_models(helper.read_env(self.original), document['models'], self.catalog)
        self.migration_conflicts.update(model_conflicts)
        from docker.config_migrations import migrate_inference
        self.models, self.services, self.routes, conflicts = migrate_inference(
            {**helper.read_env(self.original), **document['settings']}, self.models, document['services'], document['routes'])
        self.migration_conflicts.update(conflicts)
        known = {m['repo'] for m in self.catalog}
        from docker.model_services import BUILTIN_MODELS
        for model_id, defaults in BUILTIN_MODELS.items():
            self.catalog.append({'repo': model_id, 'files': [], 'defaults': deepcopy(defaults)})
        known = {m['repo'] for m in self.catalog}
        for model_id in set(self.models) - known:
            self.catalog.append({'repo': model_id, 'files': []})
        models = self.input_directory / "models.json"  # migration input only
        applied = self.input_directory / "applied-models.json"
        self.previous_models = json.loads(applied.read_text()) if applied.exists() else {}
        from docker.runtime_config import migrate_record
        self.previous_models = {name: migrate_record(record) for name, record in self.previous_models.items()}
        self.model_defaults = {}
        for index, model in enumerate(self.catalog):
            from docker.runtime_config import MODEL_DEFAULTS
            self.model_defaults[model['repo']] = {**MODEL_DEFAULTS, 'port': str(9997 + index),
                'model_dir': 'llm/' + model['repo'].replace('/', '--'), **model.get('defaults', {})}
            self.models[model['repo']] = deepcopy({**self.model_defaults[model['repo']], **self.models.get(model['repo'], {})})
        # Back up on entry, before any edits. Permission-restricted, never displayed.
        self.backup = self.directory / ("backups/" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        self.backup.mkdir(parents=True, mode=0o700)
        for name, source in [("deployment.env", root / ".env"), ("config.json", hot),
                             ('deployment.env.example', root / '.env.example'),
                             ("settings.json", self.input_directory / "settings.json"),
                             ("models.json", models), ("applied-models.json", applied),
                             ("models.compose.json", self.input_directory / "models.compose.json")]:
            if source.exists():
                target = self.backup / name
                target.write_bytes(source.read_bytes())
                target.chmod(0o600)
        self.expected = {p: p.read_bytes() if p.exists() else None for p in
            (root / ".env", root / '.env.example', hot, models, self.directory / "settings.json", self.directory / "models.compose.json")}
        self.expected.update({p: p.read_bytes() if p.exists() else None for p in
                              (self.directory / 'config.json',
                               self.input_directory / 'settings.json', self.input_directory / 'models.compose.json')})
        for model_id, record in self.models.items():
            if record['manager'] == 'ops':
                adapter = record['adapter']
                key_env = 'CLEF_SERVICE_API_KEY' if adapter == 'clef' else 'NANOJEV_SERVICE_API_KEY' if adapter == 'nanojev' else ''
                self.services.setdefault(model_id, {'kind': 'local', 'model': model_id, 'api_key_env': key_env})
        self.legacy_expected = self.legacy_snapshot()
        self.live = []
        self.gpu_identity = {}
        self.live_error = "实际状态尚未读取"
        self.pending_reservations = set()

    def refresh_live(self) -> None:
        """Read only selected non-secret environment keys; never dump container Env."""
        self.live = []
        try:
            ids = helper.run(["docker", "ps", "-aq"], cwd=self.root).split()
            if not ids:
                self.live_error = "未发现容器"; return
            keys = {r['key'] for r in self.fields if not r['secret']}
            keys.update({'CUDA_VISIBLE_DEVICES', 'NVIDIA_VISIBLE_DEVICES'})
            condition = "or " + " ".join('(eq (index (split . "=") 0) ' + json.dumps(k) + ')' for k in sorted(keys))
            template = ('{"name":{{json .Name}},"image":{{json .Config.Image}},"status":{{json .State.Status}},'
                '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}"none"{{end}},'
                '"ports":{{json .NetworkSettings.Ports}},"mounts":{{json .Mounts}},'
                '"devices":{{json .HostConfig.DeviceRequests}},"command":{{json .Config.Cmd}}}\n'
                '{{range .Config.Env}}{{if ' + condition + '}}{"env":{{json .}}}\n{{end}}{{end}}')
            raw = helper.run(["docker", "inspect", "--format", template, *ids], cwd=self.root)
            container = None
            for line in raw.splitlines():
                if not line.strip(): continue
                entry = json.loads(line)
                if 'name' in entry:
                    container = entry
                    container['name'] = container['name'].lstrip('/')
                    container['env'] = {}
                    if container['image'].split(':')[0] in {'qasystem-env-v1', 'vllm/vllm-openai'}:
                        self.live.append(container)
                elif container is not None and 'env' in entry:
                    key, _, value = entry['env'].partition('=')
                    container['env'][key] = value
            gpu_names = {}
            try:
                gpu_raw = helper.run(['nvidia-smi', '--query-gpu=index,uuid', '--format=csv,noheader,nounits'], cwd=self.root)
                for line in gpu_raw.splitlines():
                    index, sep, uuid = line.partition(',')
                    if sep and index.strip().isdigit(): gpu_names[uuid.strip()] = index.strip()
            except (OSError, RuntimeError, subprocess.TimeoutExpired): pass
            self.gpu_identity = gpu_names
            for c in self.live:
                env = c['env']; command = c.get('command') or []
                tokens = []
                for part in command:
                    try: tokens.extend(shlex.split(part))
                    except ValueError: pass
                def flag(name):
                    try: return tokens[tokens.index(name) + 1]
                    except (ValueError, IndexError): return ''
                c['model'] = flag('--served-model-name') or flag('--record') or env.get('LLM_SERVED_MODEL_NAME', '')
                if not c['model'] and 'vllm' in tokens and 'serve' in tokens:
                    try: c['model'] = tokens[tokens.index('serve') + 1]
                    except IndexError: pass
                if c['name'] == 'qasystem_clef': c['model'] = 'clef-flash'
                if c['name'] == 'qasystem_nanojev': c['model'] = 'nanojev'
                dedicated = env.get('CLEF_CUDA_VISIBLE_DEVICES') if c['name'] == 'qasystem_clef' else env.get('NANOJEV_CUDA_VISIBLE_DEVICES') if c['name'] == 'qasystem_nanojev' else ''
                requested = [device for request in c.get('devices') or [] for device in request.get('DeviceIDs') or []]
                declared = dedicated or env.get('CUDA_VISIBLE_DEVICES') or env.get('LLM_GPU_DEVICE_IDS')
                if not declared:
                    declared = ','.join(requested) or env.get('NVIDIA_VISIBLE_DEVICES', '')
                if declared in {'all', ''}: declared = '未固定GPU'
                c['gpu'] = ','.join(gpu_names.get(value.strip(), value.strip()) for value in declared.split(','))
                c['context'] = flag('--max-model-len') or env.get('LLM_MAX_MODEL_LEN', '')
                c['host_ports'] = ', '.join(sorted({p['HostPort'] for bindings in (c['ports'] or {}).values() for p in bindings or []})) or '未发布'
                # Discard raw command: arbitrary arguments may include credentials.
                c.pop('command', None)
            self.live_error = ""
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
            self.live_error = "读取实际状态失败；检查 Docker 权限/连接，按 r 重试"

    def actual_value(self, key: str) -> str:
        backend = next((c for c in self.live if c['name'] == 'qasystem_backend' and c['status'] == 'running'), None)
        if backend is None: return '未确认（没有运行中的 Backend）'
        if key in HOT_KEYS:
            mount = next((m for m in backend['mounts'] if m['Destination'] == '/app/runtime-config'), None)
            if mount:
                path = Path(mount['Source']) / 'config.json'
                try:
                    data = read_settings(path)
                    if key in data: return data[key] or '(空)'
                except FileNotFoundError: pass
                except (OSError, ValueError): return '未确认（热配置读取失败）'
        if key in backend['env']: return backend['env'][key] or '(空)'
        return '未显式配置（使用程序默认值）'

    def model_root(self) -> Path:
        path = Path(self.values["HOST_MODELS_DIR"]).expanduser()
        return (self.root / path).resolve() if not path.is_absolute() else path.resolve()

    def rows(self, query: str = "") -> list[dict]:
        rows = []
        for container in self.live:
            status = {'running': '运行中', 'exited': '已停止', 'created': '未启动', 'restarting': '重启中'}.get(container['status'], container['status'])
            if container['health'] not in {'none', 'healthy'}: status += ' / ' + container['health']
            rows.append(dict(kind='runtime', key=container['name'], container=container,
                display_name=container['name'], display_value=status,
                label=container['name'] + ' ' + status + ' ' + container['model']))
        for model in self.catalog:
            repo = model["repo"]
            state = self.models[repo]
            exists = (self.model_root() / state['model_dir']).is_dir()
            rows.append(dict(kind="model", repo=repo, key="enabled",
                display_name=f"[{state['engine']}] [{ 'x' if state['enabled'] else ' ' }] {repo}",
                display_value='权重已找到' if exists else '无本地权重',
                label=f"[{ 'x' if state['enabled'] else ' ' }] {repo} ({'权重已找到' if exists else '无本地权重'})"))
            if state["enabled"] or any(key.startswith(repo + '.') for key in self.migration_conflicts):
                for key, name in (("engine", "推理方式"), ("adapter", "启动适配器"), ("manager", "唯一管理者"), ("device", "设备选择"), ("engine_options", "引擎专属参数JSON"), ("port", "宿主端口"), ("context", "上下文上限"), ("utilization", "显存比例"),
                                  ('bind', '监听地址'), ('model_dir', '权重相对路径'), ('max_num_seqs', '最大并发序列'),
                                  ('max_num_batched_tokens', '批处理 token 上限'), ('dtype', '计算精度')):
                    conflict = repo + '.' + key in self.migration_conflicts and repo + '.' + key not in self.resolved_migrations
                    from docker.runtime_config import MODEL_DESCRIPTIONS
                    rows.append(dict(kind="model", repo=repo, key=key, display_name=f"  [启动] {name}",
                        default=self.model_defaults[repo][key],
                        description=('[冲突] 请编辑本项确认取值。' if conflict else '') + MODEL_DESCRIPTIONS[key],
                        display_value=('! ' if conflict else '') + (str(state[key]) or '(空)'), label=f"    {name}: {state[key]}"))
        for name, value in [('services', self.services), ('routes', self.routes)]:
            rows.append(dict(kind='registry', key=name, display_name='[审核] ' + name,
                display_value=json.dumps(value, ensure_ascii=False), label=name,
                description='集中服务登记与用途路由；输入 JSON，无凭据值，仅引用已有 key 环境变量名。'))
        for field in self.fields:
            if field["key"] in LEGACY_MODEL_KEYS: continue
            value = self.values.get(field["key"], "")
            shown = "******" if field["secret"] and value else value
            actual = self.actual_value(field['key']) if not field['secret'] else '隐藏（未读取容器凭据）'
            pending = not field['secret'] and not actual.startswith(('未', '隐藏')) and actual != (value or '(空)')
            rows.append(dict(kind="env", **field, actual_value=actual,
                display_name=f"{'[热更]' if field['hot'] else '[启动]'} {field['key']}",
                display_value=('! ' if pending else '') + (shown or '(空)'),
                label=f"{'[热更]' if field['hot'] else '[启动]'} {field['key']} = {shown}"))
        return [r for r in rows if query.lower() in (r["label"] + r.get("description", "")).lower()]

    def validate(self, probe: bool = False) -> list[str]:
        errors, ports, gpus = [], {}, {}
        for key, old in self.migration_conflicts.items():
            if key not in self.resolved_migrations:
                errors.append(f'{key} 与旧配置 {", ".join(old)} 冲突；在 UI 中编辑标准项确认取值')
        allowed = set(helper.read_env("GPU_DEVICE_IDS=" + self.values.get("GPU_DEVICE_IDS", ""))["GPU_DEVICE_IDS"].split(","))
        reserved = set(self.values.get("EXECUTION_GPU_RESERVED_DEVICE_IDS", "").split(","))
        own = {self.model_gpu(m) for m in self.previous_models.values() if m.get("enabled")}
        own.update(self.pending_reservations)
        try:
            guard.require(self.root, self.directory)
            approved_values = guard.approved(self.directory)['snapshot']['values']
            own.update(self.model_gpu(m) for repo, m in self.models.items()
                       if approved_values.get(f'models.{repo}.enabled', {}).get('value') == 'True'
                       and approved_values.get(f'models.{repo}.device', {}).get('value') == m['device'])
        except (ValueError, OSError):
            pass
        for key in ("BACKEND_HOST_PORT", "AGENT_HOST_PORT"):
            if self.values.get(key):
                try:
                    port = int(self.values[key])
                    if not 1 <= port <= 65535: raise ValueError()
                    if port in ports: errors.append(f"{key} 与 {ports[port]} 端口重复")
                    ports[port] = key
                except ValueError: errors.append(f"{key} 必须是有效端口")
        for repo, model in self.models.items():
            if not model["enabled"]: continue
            try:
                port, length, ratio = int(model["port"]), int(model["context"]), float(model["utilization"])
                if not 1 <= port <= 65535 or not 1024 <= length <= 262144 or not math.isfinite(ratio) or not 0 < ratio < 1:
                    raise ValueError()
                if (int(model['max_num_seqs']) < 1 or int(model['max_num_batched_tokens']) < 1 or
                        model['dtype'] not in {'auto', 'half', 'float16', 'bfloat16', 'float', 'float32'} or
                        model['bind'] not in {'127.0.0.1', '0.0.0.0'}):
                    raise ValueError()
                if port in ports: errors.append(f"{repo} 与 {ports[port]} 端口重复")
                ports[port] = repo
                gpu = self.model_gpu(model)
                if gpu and self.values.get("GPU_DEVICE_IDS") and self.gpu_identity.get(gpu, gpu) not in allowed:
                    errors.append(f"{repo} GPU 不在允许列表")
                if gpu:
                    budget_gpu = self.gpu_identity.get(gpu, gpu)
                    previous_name, previous_budget = gpus.get(budget_gpu, ('', 0.0))
                    if previous_budget + ratio > 1: errors.append(f'{repo} 与 {previous_name} 使用同一卡的显存预算超过100%')
                    gpus[budget_gpu] = (repo, previous_budget + ratio)
                if gpu in reserved and gpu not in own: errors.append(f"{repo} GPU 已被其他服务预留")

                from docker.model_services import validate_record
                validate_record(repo, model, root=self.model_root())
                path = (self.model_root() / model['model_dir']).resolve()
                if not model['model_dir'] or not path.is_relative_to(self.model_root()):
                    raise ValueError()
                if not path.is_dir(): errors.append(f"{repo} 本地权重目录不存在")
                previous = self.previous_models.get(repo, {})
                unchanged_port = previous.get("enabled") and previous.get("port") == model["port"]
                if probe and not unchanged_port and model['manager'] == 'ops':
                    with socket.socket() as sock: sock.bind((model['bind'], port))
            except (ValueError, OSError): errors.append(f"{repo} 参数无效或端口被其他进程占用")
        for key, value in self.values.items():
            if key in HOT_KEYS and value and any(suffix in key for suffix in ("MAX_TOKENS", "INPUT_TOKENS", "OUTPUT_TOKENS", "CONCURRENCY", "QPS", "QPM", "TIMEOUT")):
                try:
                    number = float(value)
                    if not math.isfinite(number) or number < 0 or (number == 0 and not key.endswith(("QPS", "QPM"))): raise ValueError()
                    if "TIMEOUT" not in key and not number.is_integer(): raise ValueError()
                except ValueError: errors.append(f"{key} 数值无效")
        from external_services.model_routes import resolve_service
        proposed = {'models': self.models, 'services': self.services, 'routes': self.routes}
        from docker.runtime_config import _validate_document
        try: _validate_document({'settings': {}, **proposed})
        except ValueError as exc: errors.append(str(exc))
        names = {}
        from docker.model_services import service_name
        for model_id in self.models:
            name = service_name(model_id)
            if name in names: errors.append('模型记录生成相同服务名称；请调整记录ID')
            names[name] = model_id
        for purpose in self.routes:
            try: resolve_service(purpose, registry=proposed)
            except ValueError as exc: errors.append(str(exc))
        if self.values.get("KG_EXTRACT_ALGO_VERSION") not in {"latest", "v0", "v2", "v2.1", "v2.2", "v3", "v3.1", "jev"}:
            errors.append("抽取算法版本无效")
        a, b = self.values.get("KG_EXTRACT_LLM_MAX_TOKENS"), self.values.get("KG_EXTRACT_LLM_MAX_OUTPUT_TOKENS")
        if a and b and a != b: errors.append("MAX_TOKENS 与 MAX_OUTPUT_TOKENS 必须一致（或后者留空）")
        for key in ("KG_EXTRACT_LLM_BASE_URL", "QASYSTEM_JUDGE_BASE_URL"):
            value = self.values.get(key, "")
            if value:
                from urllib.parse import urlsplit
                url = urlsplit(value)
                if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                    errors.append(f"{key} 必须是无凭据的 HTTP(S) 地址")
        if self.values.get("QASYSTEM_ENV_IMAGE", "").split(":")[0] != "qasystem-env-v1": errors.append("环境镜像必须为 qasystem-env-v1")
        return errors

    @staticmethod
    def model_gpu(model):
        from docker.model_services import physical_gpu
        return physical_gpu(model)

    def compose(self) -> dict:
        from docker.model_services import service_name, startup_command
        services = {}
        for model_id, model in self.models.items():
            if not model['enabled'] or model['manager'] != 'ops': continue
            environment = {**self.process_environment(), 'CUDA_VISIBLE_DEVICES': self.model_gpu(model),
                           'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'VLLM_NO_USAGE_STATS': '1'}
            service = dict(image=self.values['QASYSTEM_ENV_IMAGE'], pull_policy='never',
                entrypoint=['python', '/app/docker/config_guard.py', '--project-dir', '/app', '--runtime-dir', '/app/runtime-config', '--exec'],
                restart='unless-stopped', init=True, environment=environment, env_file=[str(self.root / '.env')],
                volumes=[f'{self.model_root()}:/models:ro', f"{(self.root / self.values.get('QASYSTEM_SOURCE_DIR', '.')).resolve()}:/app:ro",
                         f'{self.directory}:/app/runtime-config:ro', f'{self.directory / "observed"}:/app/runtime-config/observed',
                         f'{self.root / ".env"}:/app/.env:ro', f'{self.root / ".env.example"}:/app/deployment.env.example:ro'],
                ports=[f"{model['bind']}:{model['port']}:8000"], command=startup_command(model_id, model),
                working_dir='/app', healthcheck=dict(test=['CMD-SHELL', "curl --noproxy '*' -fsS http://127.0.0.1:8000/health"],
                    interval='30s', timeout='10s', retries=5, start_period='180s'))
            if self.model_gpu(model): service['gpus'] = 'all'
            services[service_name(model_id)] = service
        return {'services': services}

    def pending_payloads(self) -> dict:
        from docker.configuration import declarations
        from docker.config_migrations import ALIASES
        retired = set(declarations().get('retired', [])) | set(ALIASES)
        static = {k: v for k, v in self.values.items() if k not in HOT_KEYS and k not in retired}
        original_values = helper.read_env(self.original or self.template)
        # Rewrite declared keys once, eliminating duplicate assignments.
        # Unknown user-owned values remain untouched until consumers are known.
        changes = {k: v for k, v in static.items() if k in declarations()['items'] or original_values.get(k) != v}
        env = helper.update_env(self.original or self.template, changes)
        env = "\n".join(line for line in env.splitlines() if not (
            (match := re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", line)) and match[1] in HOT_KEYS | retired)) + "\n"
        settings = dict(self.persisted_hot)
        settings.update({k: v for k, v in self.values.items() if k in HOT_KEYS and self.initial_hot.get(k) != v})
        return {self.root / '.env': env, self.root / '.env.example': self.template,
                self.directory / 'config.json': json.dumps({'settings': settings, 'models': self.models, 'services': self.services, 'routes': self.routes}, ensure_ascii=False, indent=2) + '\n',
                self.directory / 'models.compose.json': json.dumps(self.compose(), ensure_ascii=False, indent=2) + '\n'}

    def save(self) -> None:
        errors = self.validate(probe=True)
        if errors: raise ValueError("；".join(errors))
        for path, expected in self.expected.items():
            if (path.read_bytes() if path.exists() else None) != expected:
                raise ValueError("配置已被其他进程修改；请退出并重新打开，避免覆盖")
        if self.legacy_snapshot() != self.legacy_expected:
            raise ValueError('旧配置目录已被其他进程修改；请重新打开')
        payloads = self.pending_payloads()
        written, copied = [], []
        try:
            copied = self.copy_legacy_runtime()
            for path, content in payloads.items():
                helper.atomic_write(path, content)
                written.append(path)
        except Exception:
            for path in reversed(written):
                old = self.expected[path]
                if old is None: path.unlink(missing_ok=True)
                else: helper.atomic_write(path, old.decode("utf-8-sig"))
            self.rollback_migration(copied)
            raise
        self.approval_directory = self.directory
        self.input_directory = self.directory
        self.legacy_expected = {}
        # Old inputs were backed up on entry. Never continue to overlay them.
        for legacy in (self.directory / "settings.json", self.directory / "models.json"):
            legacy.unlink(missing_ok=True)
        self.expected = {p: p.read_bytes() if p.exists() else None for p in self.expected}
        self.persisted_hot = json.loads(payloads[self.directory / 'config.json'])['settings']
        self.initial_hot = {k: self.values[k] for k in HOT_KEYS if k in self.values}

    def verify_weights(self) -> None:
        for entry in self.catalog:
            if self.models[entry["repo"]]["enabled"]:
                if not entry['files']: raise ValueError('此模型未登记 SHA 文件清单；不会伪称完整校验通过')
                folder = self.model_root() / self.models[entry['repo']]['model_dir']
                for i, part in enumerate(entry["files"], 1):
                    helper.progress(f"{entry['repo']} 校验 {i}/{len(entry['files'])}，{part['size']/1024**3:.2f} GiB：{part['rfilename']}")
                    helper.verify(folder, [part])

    def apply(self) -> None:
        """Explicit action only; never called on entry or Save."""
        from docker.model_services import validate_record
        for model_id, record in self.models.items():
            if record.get('enabled'): validate_record(model_id, record, root=self.model_root(), check_files=True)
        # Approval is checked before even temporary Docker probes.
        guard.require(self.root, self.directory)
        if any((p.read_text(encoding='utf-8') if p.exists() else None) != value
               for p, value in self.pending_payloads().items()):
            raise ValueError('存在未审核的编辑；请先在 UI 确认保存')
        errors = self.validate(probe=True)
        if errors: raise ValueError("；".join(errors))
        image = self.values["QASYSTEM_ENV_IMAGE"]
        helper.run(["docker", "image", "inspect", image], cwd=self.root)
        helper.run(["docker", "run", "--rm", "--entrypoint", "python", image, "-c",
            "import vllm; assert tuple(map(int,vllm.__version__.split('.')[:2])) >= (0,31)"], cwd=self.root, timeout=120)
        command = ["docker", "compose", "--project-directory", str(self.root), "-f", str(self.root / "docker-compose.yml")]
        prior_compose = self.directory / "applied.compose.json"
        if prior_compose.exists(): command += ["-f", str(prior_compose)]
        command += ["-f", str(self.directory / "models.compose.json")]
        guard.require(self.root, self.directory)
        helper.run(command + ["config", "--quiet"], cwd=self.root)
        from docker.model_services import service_name
        previous_services = json.loads(prior_compose.read_text()).get('services', {}) if prior_compose.exists() else {}
        changed = [name for repo, old in self.previous_models.items() if old.get('enabled') and self.models.get(repo) != old
                   for name in (slug(repo), service_name(repo)) if name in previous_services]
        if changed:
            helper.progress("停用或重启本界面管理且参数发生变化的模型；不处理其他模型服务")
            helper.run(command + ["stop", *changed], cwd=self.root, timeout=120)
        gpus = helper.gpu_rows(helper.run(["docker", "run", "--rm", "--gpus", "all", "--entrypoint", "nvidia-smi", image,
            "--query-gpu=index,name,memory.total,memory.used,utilization.gpu", "--format=csv,noheader,nounits"], cwd=self.root))
        for entry in self.catalog:
            model = self.models[entry["repo"]]
            if not model["enabled"] or not self.model_gpu(model) or model["manager"] != "ops": continue
            if self.previous_models.get(entry["repo"], {}) == model: continue
            check = dict(self.values, utilization=model["utilization"])
            own = {self.model_gpu(m) for m in self.previous_models.values() if m.get("enabled")}
            own.update(self.model_gpu(m) for m in self.models.values() if m.get('enabled'))
            check["EXECUTION_GPU_RESERVED_DEVICE_IDS"] = ",".join(set(check.get("EXECUTION_GPU_RESERVED_DEVICE_IDS", "").split(",")) - own)
            size = sum(p["size"] for p in entry["files"] if p["rfilename"].endswith(".safetensors"))
            helper.choose_gpu(gpus, self.gpu_identity.get(self.model_gpu(model), self.model_gpu(model)), check, size)
        # Reserve GPUs only when deploying, not when merely saving a plan.
        reserved = set(filter(None, self.values.get("EXECUTION_GPU_RESERVED_DEVICE_IDS", "").split(",")))
        reserved.update(self.model_gpu(m) for m in self.models.values() if m["enabled"] and self.model_gpu(m))
        reserved.update(self.model_gpu(m) for m in self.previous_models.values() if m.get("enabled") and self.model_gpu(m))
        configured = set(filter(None, self.values.get('EXECUTION_GPU_RESERVED_DEVICE_IDS', '').split(',')))
        if not reserved <= configured:
            raise ValueError('模型 GPU 预留尚未审核；请重新确认保存配置')
        guard.require(self.root, self.directory)
        helper.progress("更新启动配置和目录挂载（Backend/Agent 会重建）；请确保现有任务已完成")
        helper.run(command + ["up", "-d", "--no-deps", "--force-recreate", "backend", "agent"], cwd=self.root, timeout=120)
        names = list(self.compose()["services"])
        if names:
            helper.run(command + ["up", "-d", "--no-deps", *names], cwd=self.root, timeout=120)
        helper.progress("启动命令已提交；模型加载及健康状态请查看 docker compose ps / logs。保存不等于部署成功。")
        self.previous_models = json.loads(json.dumps(self.models))
        helper.atomic_write(self.directory / "applied-models.json", json.dumps(self.previous_models, indent=2))
        helper.atomic_write(prior_compose, json.dumps(self.compose(), indent=2))

    def prepare_deployment(self):
        errors = self.validate(probe=True)
        if errors:
            raise ValueError('；'.join(errors))
        self.pending_reservations = {self.model_gpu(m) for m in self.models.values() if m.get('enabled') and m.get('manager') == 'ops' and self.model_gpu(m)}
        self.pending_reservations.update(self.model_gpu(m) for m in self.previous_models.values() if m.get('enabled') and self.model_gpu(m))
        reserved = set(filter(None, self.values.get('EXECUTION_GPU_RESERVED_DEVICE_IDS', '').split(',')))
        self.values['EXECUTION_GPU_RESERVED_DEVICE_IDS'] = ','.join(sorted(reserved | self.pending_reservations))


def read_key() -> str:
    if os.name == "nt":
        import msvcrt
        key = msvcrt.getwch()
        if key in {"\x00", "\xe0"}: return {"H": "up", "P": "down", "K": "left", "M": "right", "I": "pageup", "Q": "pagedown", "G": "home", "O": "end"}.get(msvcrt.getwch(), "")
        return key
    import termios, tty, select
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        key = os.read(fd, 1).decode(errors="ignore")
        if key == "\x1b":
            tail = ""
            while select.select([sys.stdin], [], [], 0.05)[0]: tail += os.read(fd, 1).decode(errors="ignore")
            return {"[A": "up", "[B": "down", "[C": "right", "[D": "left", "[5~": "pageup", "[6~": "pagedown", "[H": "home", "[F": "end"}.get(tail, "escape")
        return key
    finally: termios.tcsetattr(fd, termios.TCSADRAIN, original)


def ui(config: Configuration) -> None:
    # Restore the original terminal even when interrupted during editing.
    print("\x1b[?1049h", end="", flush=True)
    try:
        _ui(config)
    finally:
        print("\x1b[0m\x1b[?25h\x1b[?1049l", end="", flush=True)


def review_save(config: Configuration) -> bool:
    """Review every page before explicit, interactive approval; no headless bypass."""
    if not sys.stdin.isatty():
        raise ValueError('人工审核需要交互终端，不能通过管道自动批准')
    errors = config.validate(probe=True)
    if errors:
        raise ValueError('；'.join(errors))
    payloads = config.pending_payloads()
    current = guard.snapshot(config.root, config.directory, payloads=payloads)
    previous = guard.approved(config.approval_directory)
    revision = int(previous.get('revision', 0)) + 1 if previous else 1
    changes = guard.changes(previous, current)
    if not changes and previous and current == previous['snapshot']:
        guard.require(config.root, config.directory)
        return True
    page, visited = 0, set()
    while True:
        columns, lines = shutil.get_terminal_size((100, 28))
        cells, height = max(30, columns - 2), max(4, lines - 7)
        rendered = [part for line in changes for part in screen.wrap(line, cells)] or ['首次配置批准；全部配置将纳入记录。']
        pages = max(1, math.ceil(len(rendered) / height))
        page = min(page, pages - 1)
        visited.add(page)
        print('\x1b[H\x1b[J' + '\n'.join([
            f'配置变更审核 · 拟批准 r{revision} · {page + 1}/{pages} 页',
            '凭据只显示变更，不显示内容。首次审核包含完整清单。', '',
            *rendered[page * height:(page + 1) * height], '',
            '←/PgUp 上页  →/PgDn 下页  Enter 确认  q 取消（不保存）']), end='', flush=True)
        key = read_key()
        if key in {'q', 'escape', '\x03'}:
            return False
        if key in {'right', 'pagedown', 'down', 'j'}:
            page = min(pages - 1, page + 1)
        elif key in {'left', 'pageup', 'up', 'k'}:
            page = max(0, page - 1)
        elif key in {'\r', '\n'}:
            if len(visited) != pages:
                continue
            reason = input('\n变更原因（必填）: ').strip()
            if not reason:
                continue
            if input(f'输入 confirm r{revision} 批准并保存，其他输入取消: ') != f'confirm r{revision}':
                return False
            # A concurrent edit is rejected by save before any writes.
            config.save()
            actual = guard.snapshot(config.root, config.directory)
            if actual != current:
                raise ValueError('审核过程中配置发生变化；未生成批准记录，请重新审核')
            guard.record(config.directory, actual, reason=reason, reviewer=getpass.getuser(), root=config.root)
            return True


def _ui(config: Configuration) -> None:
    index, query, notice, selected = 0, "", "原配置已备份。保存、校验、部署分别操作。", 0
    baseline = json.dumps([config.values, config.models, config.services, config.routes], sort_keys=True)
    while True:
        category = screen.SECTIONS[selected]
        rows = screen.filter_rows(config.rows(query), category)
        index = min(index, max(0, len(rows) - 1))
        columns, lines = shutil.get_terminal_size((100, 28))
        height = screen.page_size(columns, lines)
        dirty = baseline != json.dumps([config.values, config.models, config.services, config.routes], sort_keys=True)
        frame = screen.render(config, rows, index, category, query, notice, columns, lines,
                              dirty=dirty, color="NO_COLOR" not in os.environ)
        print("\x1b[H" + frame + "\x1b[J", end="", flush=True)
        row = rows[index] if rows else None
        key = read_key()
        try:
            if key in {"q", "\x03", "escape"}: return
            if key in {"down", "j"}: index = min(len(rows) - 1, index + 1)
            elif key in {"up", "k"}: index = max(0, index - 1)
            elif key in {"left", "right", "\t"}:
                selected = (selected + (-1 if key == "left" else 1)) % len(screen.SECTIONS); index = 0
            elif key == "pageup": index = max(0, index - height)
            elif key == "pagedown": index = min(max(0, len(rows) - 1), index + height)
            elif key == "home": index = 0
            elif key == "end": index = max(0, len(rows) - 1)
            elif key == 'n':
                model_id = input('新模型记录 ID（或仓库名）: ').strip()
                if not model_id or model_id in config.models: raise ValueError('记录 ID 为空或已存在')
                from docker.runtime_config import MODEL_DEFAULTS
                engine = input('推理方式 vllm/native: ').strip()
                adapter = input('适配器 chat/clef/nanojev: ').strip()
                if engine not in {'vllm', 'native'} or adapter not in {'chat', 'clef', 'nanojev'}:
                    raise ValueError('推理方式/适配器未登记')
                state = deepcopy(MODEL_DEFAULTS)
                state.update(engine=engine, adapter=adapter, model_dir='llm/' + model_id.replace('/', '--'))
                config.models[model_id] = state
                config.model_defaults[model_id] = deepcopy(state)
                config.catalog.append({'repo': model_id, 'files': []})
                config.services[model_id] = {'kind': 'local', 'model': model_id, 'api_key_env': ''}
                notice = '已添加关闭的模型计划；请编辑目录/设备/端口，审核后才可启动。'
            elif key == "/":
                print("\n", end="")
                query = input("搜索配置名称/说明（空为全部）: ").strip()
                selected = screen.SECTIONS.index("全部"); index = 0
            elif key == "r":
                config.refresh_live()
                notice = config.live_error or "实际服务状态已刷新；没有修改配置或启动服务。"
            elif key in {" ", "\r", "\n", "d"} and row:
                if row['kind'] == 'runtime':
                    notice = "此行为运行中的实际服务；启用计划请切换到“模型”分区编辑。"
                    continue
                if row['kind'] == 'registry':
                    if key != ' ':
                        value = json.loads(input(row['key'] + ' JSON → ').strip())
                        if not isinstance(value, dict): raise ValueError('服务/路由必须为 JSON 对象')
                        setattr(config, row['key'], value)
                    continue
                if row["kind"] == "model":
                    state = config.models[row["repo"]]
                    if row["key"] == "enabled": state["enabled"] = not state["enabled"]
                    elif key == 'd': state[row['key']] = row['default']
                    elif key != " ":
                        value = input(f"{row['label']} → ").strip()
                        state[row["key"]] = json.loads(value) if row["key"] == "engine_options" else value
                    if key != " ": config.resolved_migrations.add(row['repo'] + '.' + row['key'])
                else:
                    name = row["key"]; value = config.values.get(name, "")
                    if key == "d": config.values[name] = row["default"]
                    elif key == " " and (value.lower() in {"true", "false"} or
                            (value in {"0", "1"} and any(s in name for s in ("ENABLED", "DISABLE", "VERIFY", "SCHEDULING")))):
                        config.values[name] = {"true": "false", "false": "true", "0": "1", "1": "0"}[value.lower()]
                    elif key != " ": config.values[name] = (getpass.getpass if row["secret"] else input)(f"{name} 新值: ")
                    if key != " ": config.resolved_migrations.add(name)
            elif key == "s":
                if review_save(config):
                    baseline = json.dumps([config.values, config.models, config.services, config.routes], sort_keys=True)
                    notice = '配置已审核、保存并记录版本；没有启动模型。'
                else:
                    notice = '已取消审核，文件未修改。'
            elif key == "v":
                if input("将完整读取已勾选模型的权重，可能耗时数分钟。输入 verify 开始: ") == "verify":
                    config.verify_weights(); input("权重校验完成，回车返回。")
            elif key == "a":
                print("将保存配置、重建 Backend/Agent 并启动勾选模型；请先完成在途任务。")
                if input("输入 deploy 执行，其他输入返回: ") == "deploy":
                    config.prepare_deployment()
                    if not review_save(config):
                        notice = '已取消审核；未部署。'
                        continue
                    config.apply()
                    baseline = json.dumps([config.values, config.models, config.services, config.routes], sort_keys=True)
                    input("回车返回配置界面。")
        except (ValueError, OSError, RuntimeError) as exc: notice = str(exc)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path)
    parser.add_argument("--check", action="store_true", help="只检查配置结构与冲突，不部署、不校验权重")
    args = parser.parse_args(argv)
    try:
        config = Configuration((args.project_dir or helper.project_dir()).resolve())
        if args.check:
            errors = config.validate()
            print("\n".join(errors) if errors else "配置检查通过（未部署、未校验权重）")
            return int(bool(errors))
        if not sys.stdin.isatty(): raise ValueError("配置界面需要交互终端，请使用 ssh -t")
        print("正在读取容器实际配置（只读查询，不启动服务）……", flush=True)
        config.refresh_live()
        ui(config)
        return 0
    except (OSError, ValueError, KeyboardInterrupt, EOFError):
        print("配置界面未完成；未显示配置值，请检查路径、JSON 格式或交互终端。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
