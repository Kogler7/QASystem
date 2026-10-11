import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from algorithm.extraction_trace import ExtractionTrace
from algorithm.knowledge_extract.request_runtime import ScheduledClient

@pytest.mark.parametrize('failed', [False, True])
def test_transport_messages_and_response_are_saved_without_headers(tmp_path, failed):
    client = ScheduledClient(SimpleNamespace(), {'quota_group':uuid4().hex, 'max_concurrency':1, 'qps':10, 'qpm':100, 'request_timeout':10, 'disable_thinking':True})
    trace = client.extraction_trace = ExtractionTrace(tmp_path, secrets=('secret-value',))
    trace.local.call_id = 'logical-call'
    messages = [{'role':'system','content':'JSONL'}, {'role':'user','content':'原文'}]
    def generate(**kwargs):
        assert kwargs['messages'] == messages
        if failed:
            raise TimeoutError('secret-value')
        return {'choices':[{'message':{'content':'结果'}}]}
    if failed:
        with pytest.raises(TimeoutError):
            client._call(generate, messages=messages, model='test', extra_headers={'Authorization':'secret-value'})
    else:
        assert client._call(generate, messages=messages, model='test', extra_headers={'Authorization':'secret-value'})['choices']
    record = next(trace.directory.glob('*-http-request.json'))
    request = json.loads(record.read_text())
    assert request['call_id'] == 'logical-call' and request['request']['messages'] == messages
    response = json.loads(record.with_name(record.stem+'-response.json').read_text())
    assert response['status'] == ('failed' if failed else 'returned')
    assert 'secret-value' not in ''.join(p.read_text() for p in trace.directory.glob('*.json'))
