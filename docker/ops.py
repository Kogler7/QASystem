"""Unified QASystem control with validated, audited configuration transactions."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import uuid

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qasystem_config import config_guard as guard
from qasystem_config.runtime_config import HOT_KEYS, read_document, read_settings
from qasystem_config.io import read_env
from qasystem_config.config_database import append_event, database
from qasystem_config.configuration import UndeclaredConfigurationError


class ControlError(ValueError):
    """Safe operator-facing error without configuration values."""


def install_command(root):
    """Install a user-level launcher; never touches application approval."""
    existing = shutil.which('ops')
    windows = sys.platform == 'win32'
    if windows:
        import winreg
        target = Path(os.environ['LOCALAPPDATA']) / 'QASystem' / 'bin' / 'ops.cmd'
    else:
        target = Path.home() / '.local' / 'bin' / 'ops'
    marker = 'QASystem managed ops launcher'
    if existing and Path(existing).resolve() != target.resolve():
        raise ControlError('PATH 中已有其他 ops 命令，拒绝覆盖；请使用交付目录内的 ./ops 或 ops.ps1')
    if target.exists() and marker not in target.read_text(encoding='utf-8'):
        raise ControlError('安装位置已有其他命令，拒绝覆盖')
    target.parent.mkdir(parents=True, exist_ok=True)
    if windows:
        # Cmd quoting cannot safely carry expansion characters in installation paths.
        if any(char in str(root) + sys.executable for char in '%!\r\n'):
            raise ControlError('安装路径包含不支持的命令展开字符，请选择其他路径')
        target.write_text(f'@echo off\nrem {marker}\n"{sys.executable}" "{root / "ops.py"}" %*\n', encoding='utf-8')
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            try:
                current, _ = winreg.QueryValueEx(key, 'Path')
            except FileNotFoundError:
                current = ''
            if str(target.parent).casefold() not in [p.casefold() for p in current.split(';')]:
                winreg.SetValueEx(key, 'Path', 0, winreg.REG_EXPAND_SZ, current.rstrip(';') + ';' + str(target.parent))
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, ctypes.c_wchar_p('Environment'), 0x0002, 2000, None)
        print('已安装用户级命令并登记用户 PATH。请重新打开终端；无需管理员权限。')
    else:
        target.write_text(f'#!/bin/sh\n# {marker}\nexec {shlex.quote(sys.executable)} '
                          f'{shlex.quote(str(root / "ops.py"))} "$@"\n', encoding='utf-8')
        target.chmod(0o755)
        line = '\n# QASystem ops user command\nexport PATH="$HOME/.local/bin:$PATH"\n'
        for name in ('.profile', '.bashrc', '.zshrc'):
            profile = Path.home() / name
            original = profile.read_text(encoding='utf-8') if profile.exists() else ''
            if '# QASystem ops user command' not in original:
                if profile.exists():
                    shutil.copy2(profile, profile.with_name(profile.name + '.qasystem-ops.bak'))
                profile.write_text(original + line, encoding='utf-8')
        print('已安装到 ~/.local/bin/ops，登记 Bash/Zsh 的 PATH；修改前已备份已有启动文件。'
              '重新登录 SSH 后可直接输入 ops；当前终端可先执行 export PATH="$HOME/.local/bin:$PATH"。')
    print(f'命令位置：{target}\n绑定交付目录：{root}')
    print('验证：ops --help；若交付目录移动，请在新目录重新执行 install。')
    return 0


def directory_for(root):
    from qasystem_config.startup_contract import runtime_directory
    return runtime_directory(root)


def probe_runtime(root, directory):
    """Run inside a service with its inherited process environment, without writes."""
    revision = guard.require(root, directory)
    settings = read_settings(directory / 'config.json')
    expected = read_env((root / '.env').read_text(encoding='utf-8-sig'))
    stale = sorted(k for k, v in expected.items() if k not in HOT_KEYS and os.getenv(k) != v)
    if stale:
        raise ControlError('启动环境未同步，需要重新创建容器：' + ', '.join(stale))
    # Validate the same extraction configuration used at submission, not merely JSON syntax.
    from backend.app.services.extraction_config import resolve_config
    try:
        resolve_config({})
    except (ValueError, ImportError, AttributeError, UndeclaredConfigurationError):
        raise ControlError('抽取配置或运行依赖校验失败；未输出配置值，请检查所选算法与模型配置') from None
    print(json.dumps({'revision': revision, 'hot_keys': sorted(settings),
                      'scope': 'new requests / new clients'}, ensure_ascii=False))
    return 0


def compose_command(root, directory):
    command = ['docker', 'compose', '--project-directory', str(root), '-f', str(root / 'docker-compose.yml')]
    override = directory / 'models.compose.json'
    if override.is_file():
        command += ['-f', str(override)]
    return command


def run(command, root):
    return subprocess.run(command, cwd=root, check=False).returncode


def reload_services(root, directory, services):
    revision = guard.require(root, directory)
    planned = read_document(directory / 'config.json')['models']
    applied_path = directory / 'applied-models.json'
    applied = json.loads(applied_path.read_text()) if applied_path.is_file() else {}
    active = lambda models: {k: v for k, v in models.items() if v.get('enabled')}
    if active(planned) != active(applied):
        raise ControlError('模型启动计划尚未部署；reload 不加载权重或变更 GPU，请使用 ops deploy')
    command = compose_command(root, directory)
    for service in services:
        print(f'检查 {service} 可读取批准版本 r{revision}（不重启）', flush=True)
        code = run(command + ['exec', '-T', service, 'python', '-m', 'docker.ops',
                              '--project-dir', '/app', '_probe'], root)
        if code:
            return code
    print(f'批准版本 r{revision} 已通过容器读取与抽取配置检查；动态值供后续请求/新建客户端使用。'
          '在途任务不变；此检查不代表下游服务连通性测试。', flush=True)
    append_event(directory, {'event': 'verified-readable', 'revision': revision, 'services': services})
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-dir', type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('install', help='安装用户级 ops 命令并登记 PATH，无需管理员权限')
    config_parser = sub.add_parser('config', help='配置 GUI 或受控自动配置事务')
    config_parser.add_argument('operation', nargs='?', choices=['plan', 'diff', 'apply'])
    config_parser.add_argument('--patch', type=Path, help='私有 JSON patch 文件，不在命令参数中填写凭据')
    config_parser.add_argument('--reason', help='变更原因，不得包含凭据')
    config_parser.add_argument('--actor', choices=['human', 'agent', 'automation'], default='agent')
    config_parser.add_argument('--base-revision', type=int, help='计划基准版本；首次为 0，apply 必填')
    sub.add_parser('check', help='校验批准版本及配置冲突，不部署')
    sub.add_parser('status', help='显示批准版本及容器状态，不显示凭据')
    history = sub.add_parser('history', help='查看配置版本和实际读取记录，生成本地 JSON 数据库视图')
    history.add_argument('--json', action='store_true', help='输出脱敏 JSON 历史')
    sub.add_parser('deploy', help='调用现有部署流程；必须通过有效配置事务')
    runtime = sub.add_parser('runtime', help='控制 Backend CPU 执行进程，不重启网关或容器')
    runtime.add_argument('operation', choices=['status', 'drain', 'restart', 'apply'])
    runtime.add_argument('--role', choices=['cpu'], default='cpu')
    runtime.add_argument('--request-id', help='重试同一操作时使用同一编号；输出包含操作状态')
    reload_parser = sub.add_parser('reload', help='核对动态配置在容器内可读，不重启容器')
    reload_parser.add_argument('--services', nargs='+', choices=['backend', 'agent'], default=['backend', 'agent'])
    for action in ('restart', 'logs'):
        child = sub.add_parser(action)
        child.add_argument('services', nargs='*', choices=['backend', 'agent', 'clef', 'nanojev'], default=['backend', 'agent'])
    sub.add_parser('_probe', help='容器内部配置检查（由 reload 调用）')
    args = parser.parse_args(argv)
    root = args.project_dir.resolve()
    try:
        if args.action == 'config':
            if args.operation:
                from docker.configure import run_transaction as configuration_transaction
                report = configuration_transaction(root, operation=args.operation, patch_path=args.patch,
                    reason=args.reason, actor=args.actor, base_revision=args.base_revision)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0
            if args.patch is not None or args.reason is not None or args.base_revision is not None:
                raise ControlError('请指定 config plan/diff/apply；无子操作时打开 GUI')
            from docker.configure import main as configure
            return configure(['--project-dir', str(root)])
        if args.action == 'install':
            return install_command(root)
        directory = root / 'runtime-config' if args.action == '_probe' else directory_for(root)
        if args.action == 'history':
            data = database(directory)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                for version in data['versions']:
                    print(f"r{version['revision']}  声明版本 {version['release']}  {version['time']}  已审核")
                print(f"流程事件 {len(data['events'])} 条；进程实际读取 {len(data['observations'])} 条。"
                      '读取记录不等于下游调用成功。')
            return 0
        if args.action == '_probe':
            return probe_runtime(root, directory)
        if args.action == 'status':
            valid = True
            try:
                print(f'配置批准版本：r{guard.require(root, directory)}')
            except ValueError as exc:
                valid = False
                print(str(exc))
            code = run(compose_command(root, directory) + ['ps'], root)
            return code if valid else 2
        if args.action == 'logs':
            return run(compose_command(root, directory) + ['logs', '--tail', '100', *args.services], root)
        revision = guard.require(root, directory)
        if args.action == 'runtime':
            command = compose_command(root, directory) + ['exec', '-T', 'backend', 'python', '-m',
                'app.execution.process_control', args.operation]
            if args.operation != 'status':
                request_id = args.request_id or uuid.uuid4().hex
                print(f'进程操作编号：{request_id}；accepted/running 表示处理中，applied 仅表示进程操作完成。', flush=True)
                command += ['--role', args.role, '--revision', str(revision), '--request-id', request_id]
            return run(command, root)
        if args.action == 'reload':
            return reload_services(root, directory, args.services)
        from docker.configure import Configuration
        config = Configuration(root)
        errors = config.validate()
        if errors:
            raise ValueError('；'.join(errors))
        if args.action == 'check':
            print('配置批准版本与结构、冲突检查通过（未检查权重或下游连通性）')
            return 0
        if args.action == 'deploy':
            config.apply()
            return 0
        return run(compose_command(root, directory) + ['restart', *args.services], root)
    except ControlError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (ValueError, OSError):
        # Files, dependency validation and subprocess errors may include secrets.
        print('操作失败：请先用 ops config 审核配置，并检查文件、依赖和 Docker。'
              '启动参数修改请使用 deploy；未自动批准任何变更。', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
