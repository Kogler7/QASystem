"""Development-evaluation exception state; never creates deployment approvals.

Only evaluation launchers consult this file. Production guards do not read it.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from eval.quality import build_quality_gate, build_quality_summary

DEFAULT_STATE = Path(__file__).resolve().parents[2] / 'tmp/eval/development-approval-policy.json'
EXPECTED_CASES = tuple([f'official/official_{i:02d}' for i in range(1, 6)] + [
    'rule/rule_01', 'simple/Simple_故障规则的诊断', 'trouble/trouble_mini'])
THRESHOLD = 0.85


def _write(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时名必须带进程号：并行评测批次会同时改写同一状态文件，
    # 固定名会让两个进程交叉读写，留下拼接后的残档。
    temporary = path.with_name(f'{path.name}.{os.getpid()}.pending')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    os.replace(temporary, path)


def _read_state(path):
    """读取状态文件；内容损坏时按未启用的安全口径处理，绝不让评测批次崩掉。

    损坏状态直接报错会中止已经跑完的评测批次，而抛异常也不是安全的失败方向：
    豁免必须依赖可解析的启用记录，因此这里告警并视作未启用。
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        print(f'警告: 开发评测配置批准豁免状态无法解析，按未启用处理: {path}: {exc}', file=sys.stderr)
        return None
    if not isinstance(state, dict):
        print(f'警告: 开发评测配置批准豁免状态不是对象，按未启用处理: {path}', file=sys.stderr)
        return None
    return state


def enable(path=DEFAULT_STATE):
    state = {'schema': 1, 'enabled': True, 'scopes': ['local', '88'],
             'version': 'v3.1', 'entity_micro_f1_threshold': THRESHOLD,
             'expected_cases': list(EXPECTED_CASES),
             'checks_waived': ['configuration_approval_record'],
             'production_exception': False,
             'enabled_at': datetime.now(timezone.utc).isoformat()}
    _write(path, state)
    return state


def exception_allowed(path=DEFAULT_STATE, *, scope):
    if scope not in {'local', '88'}:
        return False
    state = _read_state(path)
    if state is None:
        return False
    return (state.get('schema') == 1 and state.get('enabled') is True
            and scope in state.get('scopes', []) and state.get('production_exception') is False
            and state.get('checks_waived') == ['configuration_approval_record'])


def assess(summary, expected_cases=EXPECTED_CASES):
    labels = [f"{r.get('group')}/{r.get('case')}" for r in summary.get('cases', [])]
    if (not summary.get('finished_at') or summary.get('interrupted')
            or summary.get('match_mode') != 'llm'
            or summary.get('extract_versions') != ['v3.1']
            or len(labels) != len(expected_cases) or set(labels) != set(expected_cases)
            or any(r.get('status') != 'succeeded' for r in summary.get('cases', []))
            or any(summary.get(k, 0) for k in ('failed', 'skipped', 'unscored'))
            or summary.get('gate', {}).get('passed') is not True):
        return {'qualified': False, 'reason': 'incomplete_or_invalid_run'}
    if not build_quality_gate(summary, require_scored=True)['passed']:
        return {'qualified': False, 'reason': 'invalid_scoring'}
    modules = build_quality_summary(summary)['modules']
    selected = [modules[k] for k in ('backend_system', 'backend_extract') if k in modules]
    if len(selected) != 1:
        return {'qualified': False, 'reason': 'ambiguous_scoring_scope'}
    result = selected[0]
    coverage = result['coverage']
    if (coverage['scored'] != len(expected_cases) or any(coverage[k] for k in (
            'failed', 'missing', 'fallback', 'invalid', 'mode_mismatch', 'unscored'))):
        return {'qualified': False, 'reason': 'incomplete_score_coverage'}
    entities = result['micro']['entities']
    tp, fp, fn = (entities[k] for k in ('tp', 'fp', 'fn'))
    denominator = 2 * tp + fp + fn
    f1 = 2 * tp / denominator if denominator else None
    return {'qualified': denominator > 0 and 200 * tp >= 85 * denominator,
            'entity_micro_f1': f1, 'threshold': THRESHOLD, 'coverage': coverage}


def observe(summary_path, path=DEFAULT_STATE):
    state = _read_state(path)
    if state is None or not state.get('enabled'):
        return None
    result = assess(json.loads(Path(summary_path).read_text(encoding='utf-8')), state['expected_cases'])
    state['last_assessment'] = {**result, 'summary': str(Path(summary_path).resolve())}
    if result['qualified']:
        state['enabled'] = False
        state['restored_at'] = datetime.now(timezone.utc).isoformat()
    _write(path, state)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=DEFAULT_STATE)
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument('--enable', action='store_true')
    commands.add_argument('--observe', type=Path)
    commands.add_argument('--status', action='store_true')
    args = parser.parse_args()
    result = enable(args.state) if args.enable else observe(args.observe, args.state) if args.observe else (
        _read_state(args.state) or {'enabled': False})
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
