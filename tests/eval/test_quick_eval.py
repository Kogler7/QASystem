import json
import subprocess
from pathlib import Path

import pytest

from eval import quick_eval, run


def setup_profile(tmp_path):
    for name in ['a', 'b', 'c']:
        path = tmp_path / 'official' / name
        path.mkdir(parents=True)
        (path / 'chunks.json').write_text('{}')
    params = tmp_path / 'params.json'
    params.write_text(json.dumps({'quick_eval': {'cases': ['official/a', 'official/b', 'official/c']}}))
    return ['--workspace', str(tmp_path), '--params', str(params)]


def test_quick_fixed_inputs_without_forced_time_budgets(tmp_path):
    command, wall = quick_eval.build_command(setup_profile(tmp_path) + ['--extract-version', 'v2'])
    args = run.parse_args(command[2:])
    assert args.mode == ['extract']
    assert args.case_attempts == 1
    assert args.extract_version == 'v2'
    assert args.extract_document_timeout is None
    assert args.extract_request_timeout is None
    assert len(args.targets) == 3
    assert Path(args.from_run) == tmp_path
    assert wall is None


@pytest.mark.parametrize('extra', [['--wall-seconds', '0'],
                                 ['--mode', 'pipeline'], ['--reuse-extract-from', 'old']])
def test_quick_rejects_invalid_or_cached_modes(tmp_path, extra):
    with pytest.raises(ValueError):
        quick_eval.build_command(setup_profile(tmp_path) + extra)


def test_quick_missing_chunks_fails_before_dispatch(tmp_path):
    args = setup_profile(tmp_path)
    (tmp_path / 'official/a/chunks.json').unlink()
    with pytest.raises(ValueError, match='缺少固定切片'):
        quick_eval.build_command(args)


def test_quick_timeout_is_incomplete_not_success(tmp_path, monkeypatch):
    args = setup_profile(tmp_path) + ['--wall-seconds', '900']
    monkeypatch.setattr(run, '__file__', str(tmp_path / 'eval/run.py'))
    def expire(*args, **kwargs):
        assert kwargs['timeout'] == 900
        raise subprocess.TimeoutExpired('fixture', 900)
    monkeypatch.setattr(quick_eval.subprocess, 'run', expire)
    assert quick_eval.main(args) == 124
    status = json.loads(next((tmp_path / 'tmp/eval/quick').glob('*/status.json')).read_text())
    assert status['status'] == 'timed_out'


def test_quick_without_deadline_records_actual_elapsed(tmp_path, monkeypatch):
    args = setup_profile(tmp_path)
    monkeypatch.setattr(run, '__file__', str(tmp_path / 'eval/run.py'))
    clock = iter([10.0, 2010.25])
    monkeypatch.setattr(quick_eval.time, 'monotonic', lambda: next(clock))
    def complete(command, **kwargs):
        assert kwargs['timeout'] is None
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(quick_eval.subprocess, 'run', complete)
    assert quick_eval.main(args) == 0
    status = json.loads(next((tmp_path / 'tmp/eval/quick').glob('*/status.json')).read_text())
    assert status['elapsed_seconds'] == 2000.25
    assert status['status'] == 'succeeded'


def test_no_poll_deadline_for_queued_and_inline_execution(monkeypatch):
    calls = []
    replies = iter([(202, {'data': {'job_id': 'job'}}),
                    (200, {'data': {'status': 'running'}}),
                    (200, {'data': {'status': 'succeeded'}}),
                    (200, {'code': 0, 'data': {'done': True}})])
    def request(url, **kwargs):
        calls.append(kwargs)
        return next(replies)
    monkeypatch.setattr(run, 'json_request', request)
    monkeypatch.setattr(run.time, 'sleep', lambda _: None)
    monkeypatch.setattr(run.time, 'monotonic', lambda: pytest.fail('unbounded polling needs no deadline'))
    result = run.submit_and_wait('http://test/submit', base_url='http://test',
                                data=b'', headers={}, params={'poll_timeout_seconds': 0})
    assert result['data']['done'] is True
    assert calls[0]['timeout'] is None
    monkeypatch.setattr(run, 'json_request', lambda *a, **kw: (200, {'code': 0}))
    assert run.submit_and_wait('http://test/submit', base_url='http://test',
                              data=b'', headers={}, params={'poll_timeout_seconds': 0}) == {'code': 0}


def test_case_attempts_one_does_not_reextract_partial(tmp_path, monkeypatch):
    calls = []
    def stages(**kwargs):
        calls.append(1)
        return {'status': 'partial'}, None, ''
    monkeypatch.setattr(run, 'run_case_stages', stages)
    result = run.run_case_with_retries(label='case', run_backend=True, run_plugin=False,
        base_url='', mcp_url='', ontology='', sources=[], options={}, params={}, golden=None,
        out_dir=tmp_path, score_ctx=None, modes=['extract'], reuse_dir=None, max_attempts=1)
    assert len(calls) == 1
    assert result[3] == 1
    assert result[0]['status'] == 'partial'
