import json

import pytest

from eval.tools.cfuse_eval_proxy import MODEL, UpstreamCompletionError, decode_completion, make_handler


def test_shared_pacing_separates_requests_and_does_not_burst_after_idle():
    from eval.tools.cfuse_eval_proxy import RequestPacer
    now, waits = [100.0], []
    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds
    pacer = RequestPacer(4, clock=lambda: now[0], sleep=sleep)
    pacer.wait()
    pacer.wait()
    assert waits == [15.0]
    now[0] += 90
    pacer.wait()
    pacer.wait()
    assert waits == [15.0, 15.0]


def test_default_pacing_does_not_wait():
    from eval.tools.cfuse_eval_proxy import RequestPacer
    RequestPacer(sleep=lambda _: pytest.fail('default pacing must be disabled')).wait()


@pytest.mark.parametrize('rate', [-1, 1.5, True])
def test_invalid_pacing_rate_is_rejected(rate):
    from eval.tools.cfuse_eval_proxy import RequestPacer
    with pytest.raises(ValueError, match='nonnegative integer'):
        RequestPacer(rate)


def completion():
    return {"model": MODEL, "choices": [{"message": {"role": "assistant", "content": "OK"},
                                        "finish_reason": "stop"}], "usage": {"total_tokens": 9}}


@pytest.mark.parametrize("sse", [False, True])
def test_preserves_complete_response_and_usage(sse):
    value = completion()
    body = json.dumps(value)
    if sse:
        body = "data:" + body + "\n\ndata: [DONE]\n\n"
    assert decode_completion(body) == value


def test_rejects_model_substitution():
    value = completion()
    value["model"] = "different-model"
    with pytest.raises(ValueError, match="different model"):
        decode_completion(json.dumps(value))


def test_rejects_multiple_responses_instead_of_truncating():
    body = "data:" + json.dumps(completion()) + "\n\n"
    with pytest.raises(ValueError, match="one complete"):
        decode_completion(body * 2)


def test_rejects_unfinished_stream():
    value = completion()
    value["choices"][0]["finish_reason"] = None
    with pytest.raises(ValueError, match="Incomplete"):
        decode_completion(json.dumps(value))


@pytest.mark.parametrize('error,status,category,delay', [
    ({'code': 'rate_limit_exceeded', 'message': 'private upstream details', 'retry_after': 30}, 429, 'upstream_rate_limit', 30),
    ({'code': 429, 'retry_after': 999}, 429, 'upstream_rate_limit', 60),
    ({'message': 'unknown upstream error', 'retry_after': 'NaN'}, 502, 'upstream_completion_error', 10),
    ({'code': 'invalid_api_key', 'message': 'private key'}, 401, 'upstream_authentication_error', 0),
])
def test_sse_error_preserves_safe_retry_category_without_leaking_content(error, status, category, delay):
    body = 'data:' + json.dumps({'error': error}) + '\n\n'
    with pytest.raises(UpstreamCompletionError) as captured:
        decode_completion(body)
    exc = captured.value
    assert (exc.status, exc.category, exc.retry_after) == (status, category, delay)
    assert 'private' not in str(exc)


def test_missing_completion_is_retried_with_a_bounded_delay():
    with pytest.raises(UpstreamCompletionError) as captured:
        decode_completion('data:{"status":"error"}\n\n')
    assert captured.value.status == 502 and captured.value.retry_after == 10


@pytest.mark.parametrize('sse', [False, True])
@pytest.mark.parametrize('code,status,category', [
    (429, 429, 'upstream_rate_limit'),
    (401, 401, 'upstream_authentication_error'),
    (503, 502, 'upstream_completion_error'),
])
def test_top_level_gateway_error_is_not_a_missing_completion(sse, code, status, category):
    body = json.dumps({'object': 'error', 'type': 'rpcError', 'code': code,
                       'message': 'private upstream detail'})
    if sse:
        body = 'data:' + body + '\n\ndata: [DONE]\n\n'
    with pytest.raises(UpstreamCompletionError) as captured:
        decode_completion(body)
    assert (captured.value.status, captured.value.category) == (status, category)
    assert 'private' not in str(captured.value)


def test_proxy_returns_retry_after_for_wrapped_upstream_error():
    from email.message import Message
    from io import BytesIO
    from types import SimpleNamespace
    payload = json.dumps({'model': MODEL, 'messages': []}).encode()
    class Client:
        def post(self, *args, **kwargs):
            return SimpleNamespace(status_code=200,
                text='data:{"error":{"code":"rate_limit_exceeded","message":"private details"}}\n\n')
    handler_type = make_handler(Client(), {})
    handler = handler_type.__new__(handler_type)
    handler.path = '/v1/chat/completions'
    handler.headers = Message()
    handler.headers['Content-Type'] = 'application/json'
    handler.headers['Content-Length'] = str(len(payload))
    handler.rfile, handler.wfile = BytesIO(payload), BytesIO()
    received = {}
    handler.send_response = lambda status: received.update(status=status)
    handler.send_header = lambda name, value: received.update({name: value})
    handler.end_headers = lambda: None
    handler.do_POST()
    assert received['status'] == 429 and received['Retry-After'] == '10'
    assert json.loads(handler.wfile.getvalue())['error']['message'] == 'upstream_rate_limit'
    assert b'private' not in handler.wfile.getvalue()
