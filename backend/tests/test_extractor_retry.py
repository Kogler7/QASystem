from types import SimpleNamespace

from algorithm.knowledge_extract import extractor


def _stream(text):
    midpoint = max(1, len(text) // 2)
    return iter(
        [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text[:midpoint]))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text[midpoint:]))]),
        ]
    )


def test_extract_v2_streams_and_retries_invalid_json(monkeypatch):
    responses = iter(
        [
            _stream("not-json"),
            _stream('{"entities":[{"type":"Term","name":"A","properties":{}}],"relations":[]}'),
        ]
    )
    create = lambda **_kwargs: next(responses)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=lambda: None,
    )
    monkeypatch.setattr(extractor, "_build_openai_client", lambda **_kwargs: client)
    monkeypatch.setattr(extractor.time, "sleep", lambda _seconds: None)
    result = extractor.extract_v2("class Term", "text", json_retries=1)
    assert result.get("_error") is None
    assert result["graph"]["nodes"][0]["uid"] == "Term::a"
    assert result["attempts"][0]["type"] == "json"


def test_extract_v2_reports_json_retry_details(monkeypatch):
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: _stream("invalid"))),
        close=lambda: None,
    )
    monkeypatch.setattr(extractor, "_build_openai_client", lambda **_kwargs: client)
    result = extractor.extract_v2("class Term", "text", json_retries=1)
    assert result["error_code"] == "LLM_JSON_PARSE_FAILED"
    assert len(result["attempts"]) == 2
