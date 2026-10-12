#!/usr/bin/env python3
"""Small fixed-input extraction comparison through eval/run.py."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

try:
    from eval import run
except ImportError:
    import run


QUICK_CASES = ['official/official_01', 'official/official_03', 'official/official_04']
QUICK_ARGUMENTS = ['--extract-version', 'v3', '--extract-provider', 'siliconflow', '--no-persist-graph',
                   '--poll-timeout-seconds', '0', '--judge-timeout-seconds', '30', '--judge-max-tokens', '4096']


def build_command(argv):
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('--quick-cases', nargs='+', default=QUICK_CASES)
    parser.add_argument('--wall-seconds', type=int)
    args, forwarded = parser.parse_known_args(argv)
    parsed = run.parse_args(forwarded)
    if parsed.targets or parsed.groups or parsed.cases or parsed.reuse_extract_from:
        raise ValueError('快速集只接受 --quick-cases 选择，不允许复用旧抽取结果')
    if parsed.stage != 'backend' or parsed.system or parsed.mode not in (None, ['extract']):
        raise ValueError('快速集只支持 Backend extract；切片必须预先准备')
    cases = args.quick_cases
    if not isinstance(cases, list) or not 1 <= len(cases) <= 3 or not all(isinstance(c, str) for c in cases):
        raise ValueError('--quick-cases 必须指定 1–3 个完整用例')
    if len(set(cases)) != len(cases):
        raise ValueError('--quick-cases 不允许重复用例')
    wall = args.wall_seconds if args.wall_seconds is not None else None
    if wall is not None and (type(wall) is not int or wall <= 0):
        raise ValueError('wall_seconds 必须为正整数，省略则不限制整轮耗时')
    workspace = Path(parsed.workspace).resolve()
    chosen = []
    for case in cases:
        target = (workspace / case).resolve()
        if not target.is_relative_to(workspace) or not target.is_dir():
            raise ValueError(f'快速用例不存在或越出 workspace: {case}')
        chosen.append(str(target))
    source = str(Path(parsed.from_run).resolve()) if parsed.from_run else str(workspace)
    # Check fixed inputs before any API calls. Never fall back to parsing here.
    for case in cases:
        if not (Path(source) / case / 'chunks.json').is_file():
            raise ValueError(f'缺少固定切片: {case}/chunks.json；请指定 --from-run')
    command = [sys.executable, str(Path(run.__file__).resolve()), *chosen, *QUICK_ARGUMENTS, *forwarded,
               '--mode', 'extract', '--stage', 'backend', '--from-run', source,
               '--case-attempts', '1', '--runs-retention-days', '0']
    return command, wall


def main(argv=None):
    try:
        command, wall = build_command(sys.argv[1:] if argv is None else argv)
    except (ValueError, OSError, RuntimeError) as exc:
        run.log(str(exc))
        return 1
    root = Path(run.__file__).resolve().parents[1] / 'tmp/eval/quick'
    directory = root / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8])
    directory.mkdir(parents=True)
    status = {'status': 'running', 'wall_seconds': wall, 'case_attempts': 1}
    status_path = directory / 'status.json'
    def save():
        status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    limit = f'最多 {wall} 秒' if wall is not None else '不限制整轮耗时'
    run.log(f'小规模评测：{limit}，整篇只抽一次；日志 {directory / "run.log"}')
    started = time.monotonic()
    try:
        with (directory / 'run.log').open('w', encoding='utf-8') as output:
            completed = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=wall, check=False)
        status.update(status='succeeded' if completed.returncode == 0 else 'failed', exit_code=completed.returncode)
    except subprocess.TimeoutExpired:
        status.update(status='timed_out', exit_code=124,
                      message='总时限到达，评测进程已停止；部分产物不是完整结果。在途 Backend 调用仍受文档预算约束。')
    except KeyboardInterrupt:
        status.update(status='cancelled', exit_code=130)
    except OSError as exc:
        status.update(status='failed', exit_code=1, error_type=type(exc).__name__)
    finally:
        status['elapsed_seconds'] = round(time.monotonic() - started, 3)
        save()
    run.log(f'快速评测结束：{status["status"]}；详情 {status_path}')
    return status['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
