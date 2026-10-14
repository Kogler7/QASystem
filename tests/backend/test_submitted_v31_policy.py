"""Submitted extraction transports do not resolve a newer service route."""
from types import SimpleNamespace


def test_submitted_v31_factory_keeps_transport_when_current_route_is_unavailable(monkeypatch):
    import openai
    from algorithm.knowledge_extract.v3_1 import llm_client
    def unavailable():
        raise ValueError('current extract route is disabled')
    monkeypatch.setattr(llm_client, 'get_llm_config', unavailable)
    calls = []
    def sdk(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace()
    monkeypatch.setattr(openai, 'OpenAI', sdk)
    client = llm_client.create_llm_client(base_url='https://submitted.example/v1',
        api_key='submitted-test-only', model='submitted-model', max_tokens=128,
        semantic_thinking=False, schedule_requests=False)
    assert calls[0]['base_url'] == 'https://submitted.example/v1'
    assert client.model == 'submitted-model' and client.max_tokens == 128
    assert client.semantic_thinking is False
