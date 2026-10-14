from copy import deepcopy
import json
import multiprocessing
import pytest
from eval.tools.development_approval_policy import assess, enable, exception_allowed, observe, EXPECTED_CASES


def summary(tp=9, fn=1):
    def side(tp, fn):
        return dict(tp=tp, fp=0, fn=fn, golden=tp+fn, predicted=tp, precision=1, recall=tp/(tp+fn), f1=2*tp/(2*tp+fn))
    metrics=dict(match_mode='llm', entities=side(tp,fn), relations=side(0,1))
    return dict(finished_at='2026-10-14',match_mode='llm',extract_versions=['v3.1'],
        modes=['extract'],stage='backend',gate={'passed':True},failed=0,skipped=0,unscored=0,
        cases=[dict(group=x.split('/')[0],case=x.split('/')[1],status='succeeded',scored=True,
            backend={'modules':{'extract':{'status':'succeeded','metrics':deepcopy(metrics)}}}) for x in EXPECTED_CASES])


def test_production_never_gets_exception_and_threshold_disables_development(tmp_path):
    p=tmp_path/'policy.json';enable(p)
    assert exception_allowed(p,scope='88') and exception_allowed(p,scope='local')
    assert not exception_allowed(p,scope='production')
    s=tmp_path/'summary.json';s.write_text(json.dumps(summary()))
    assert observe(s,p)['qualified']
    assert not exception_allowed(p,scope='88')
    assert not (tmp_path/'approved-config.json').exists()


@pytest.mark.parametrize('failure',['partial','fallback','missing_case','unfinished','wrong_version','gate_null','invalid_metrics'])
def test_high_f1_does_not_restore_on_incomplete_or_invalid_run(failure):
    s=summary()
    if failure=='partial':s['cases'][0]['status']='partial'
    if failure=='fallback':s['cases'][0]['backend']['modules']['extract']['metrics']['match_mode']='identity_fallback'
    if failure=='missing_case':s['cases'].pop()
    if failure=='unfinished':s.pop('finished_at')
    if failure=='wrong_version':s['extract_versions']=['v3']
    if failure=='gate_null':s['gate']['passed']=None
    if failure=='invalid_metrics':s['cases'][0]['backend']['modules']['extract']['metrics']['entities']['f1']=float('nan')
    assert not assess(s)['qualified']


def test_threshold_uses_micro_entity_f1_and_leaves_exception_below_target(tmp_path):
    p=tmp_path/'policy.json';enable(p)
    s=tmp_path/'summary.json';s.write_text(json.dumps(summary(tp=5,fn=5)))
    assert not observe(s,p)['qualified']
    assert exception_allowed(p,scope='local')


def _write_after_barrier(path, value, barrier, queue):
    from eval.tools.development_approval_policy import _write
    barrier.wait(timeout=30)
    try:
        _write(path, {'schema': 1, 'enabled': True, 'scopes': ['local', '88'], 'writer': value})
        queue.put(None)
    except Exception as exc:  # 子进程异常需回传，否则父进程只能看到静默失败
        queue.put(repr(exc))


def test_concurrent_writers_never_leave_a_corrupted_state_file(tmp_path):
    path = tmp_path / 'policy.json'
    workers = 6
    barrier = multiprocessing.Barrier(workers)
    queue = multiprocessing.Queue()
    processes = [multiprocessing.Process(target=_write_after_barrier, args=(str(path), index, barrier, queue))
                 for index in range(workers)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
        assert process.exitcode == 0
    assert [queue.get(timeout=5) for _ in processes] == [None] * workers
    # 固定临时名会被并发进程交叉写入，留下多个 JSON 文档拼接的残档。
    json.loads(path.read_text(encoding='utf-8'))


@pytest.mark.parametrize('payload', [
    '{"schema": 1, "enabled": true}\n{"schema": 1, "enabled": true}\n',
    '{"schema": 1, "enabled": true',
    '["not", "an", "object"]',
])
def test_corrupted_state_is_treated_as_disabled_instead_of_crashing(tmp_path, capsys, payload):
    p = tmp_path / 'policy.json'
    p.write_text(payload, encoding='utf-8')
    assert not exception_allowed(p, scope='local')
    assert not exception_allowed(p, scope='88')
    assert observe(tmp_path / 'summary.json', p) is None
    assert '警告' in capsys.readouterr().err


def test_corrupted_state_does_not_abort_an_otherwise_complete_run(tmp_path):
    p = tmp_path / 'policy.json'
    p.write_text('{not json at all', encoding='utf-8')
    s = tmp_path / 'summary.json'
    s.write_text(json.dumps(summary()), encoding='utf-8')
    # 已完成的批次不能因为状态残档而失败；豁免保持关闭，等待修复。
    assert observe(s, p) is None
    assert p.read_text(encoding='utf-8') == '{not json at all'


def test_legacy_reader_crashed_on_corrupted_state(tmp_path):
    """旧读取路径没有损坏兜底，残档会直接把已完成的评测批次打挂。"""
    from eval.tools import development_approval_policy as policy
    p = tmp_path / 'policy.json'
    p.write_text('{"schema": 1, "enabled": true}\n{"schema": 1, "enabled": true}\n', encoding='utf-8')
    with pytest.raises(json.JSONDecodeError):  # 旧实现：直接 json.loads 整档
        json.loads(p.read_text(encoding='utf-8'))
    # 修复后的口径：同一份残档只告警，不抛异常，且不授权豁免。
    assert policy.exception_allowed(p, scope='local') is False
    assert policy.observe(tmp_path / 'summary.json', p) is None
