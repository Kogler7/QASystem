"""Judge protocol regressions; all provider replies are local fixtures."""

import json
from copy import deepcopy

import pytest

from eval import run


def _client(**overrides):
    options = {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "test-secret-key",
        "model": "test-model",
        "timeout": 30,
        "max_tokens": 8192,
    }
    options.update(overrides)
    return run.JudgeClient(**options)


def _reply(content='{"entity_matches": []}', *, reasoning=None, finish_reason="stop", **extra):
    return {
        "id": "chatcmpl-test",
        "choices": [{"finish_reason": finish_reason, "message": {"content": content, "reasoning_content": reasoning}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        **extra,
    }


def _stub_http(monkeypatch, replies):
    requests = []
    responses = iter(replies)

    def request(url, **kwargs):
        requests.append({"url": url, "payload": json.loads(kwargs["data"])})
        response = next(responses)
        status, body = response if isinstance(response, tuple) else (200, response)
        return status, {}, json.dumps(body).encode()

    monkeypatch.setattr(run, "http_request", request)
    return requests


def _stub_http_responder(monkeypatch, responder):
    """按请求内容作答的 stub，用于并发（请求顺序不确定）场景。"""
    import threading

    requests = []
    lock = threading.Lock()

    def request(url, **kwargs):
        payload = json.loads(kwargs["data"])
        with lock:
            requests.append({"url": url, "payload": payload})
        response = responder(payload)
        status, body = response if isinstance(response, tuple) else (200, response)
        return status, {}, json.dumps(body).encode()

    monkeypatch.setattr(run, "http_request", request)
    return requests


def _judge(client):
    return run._judge_one_batch(client, "entity_matches", [{"type": "T", "name": "x"}], [{"type": "T", "name": "x"}], 0)


def _prompt_items(messages):
    prompt = messages[1]["content"]
    golden_json = prompt.split("## golden items\n", 1)[1].split("\n\n## predicted items", 1)[0]
    predicted_json = prompt.split("## predicted items\n", 1)[1].split("\n\n请只输出", 1)[0]
    return json.loads(golden_json), json.loads(predicted_json)


def test_deepseek_auto_uses_json_object_and_disables_thinking(monkeypatch):
    requests = _stub_http(monkeypatch, [_reply('{"entity_matches": [{"golden_index": 0, "predicted_index": 0}]}')])
    pairs, info = _judge(_client())
    assert pairs == [(0, 0)]
    assert len(requests) == 1
    assert requests[0]["payload"]["response_format"] == {"type": "json_object"}
    assert requests[0]["payload"]["thinking"] == {"type": "disabled"}
    assert info["attempts"] == [{
        "response_mode": "json_object", "http_status": 200, "response_id": "chatcmpl-test",
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        "finish_reason": "stop", "reasoning_content_length": 0,
        "content_length": 63, "status": "succeeded",
    }]


@pytest.mark.parametrize("base_url", ["https://example.test/v1", "https://api.deepseek.com.example.test/v1"])
def test_other_providers_keep_schema_without_vendor_thinking_parameter(monkeypatch, base_url):
    requests = _stub_http(monkeypatch, [_reply()])
    _judge(_client(base_url=base_url))
    assert requests[0]["payload"]["response_format"]["type"] == "json_schema"
    assert "thinking" not in requests[0]["payload"]


def test_explicit_format_and_thinking_configuration(monkeypatch):
    requests = _stub_http(monkeypatch, [_reply()])
    _judge(_client(response_format="json_schema", disable_thinking=False))
    assert requests[0]["payload"]["response_format"]["type"] == "json_schema"
    assert "thinking" not in requests[0]["payload"]
    assert _client(base_url="https://example.test/v1", response_format="json_object").response_modes == ("json_object", "plain")


@pytest.mark.parametrize("host", ["api.siliconflow.cn", "api.siliconflow.com"])
def test_siliconflow_judge_uses_provider_thinking_switch(monkeypatch, host):
    requests = _stub_http(monkeypatch, [_reply()])
    _judge(_client(base_url=f"https://{host}/v1"))
    assert requests[0]["payload"]["enable_thinking"] is False
    assert "thinking" not in requests[0]["payload"]


@pytest.mark.parametrize("base_url,disabled", [
    ("https://api.siliconflow.cn.example.test/v1", True),
    ("https://api.siliconflow.cn/v1", False),
])
def test_siliconflow_judge_does_not_leak_vendor_switch(monkeypatch, base_url, disabled):
    requests = _stub_http(monkeypatch, [_reply()])
    _judge(_client(base_url=base_url, disable_thinking=disabled))
    assert "enable_thinking" not in requests[0]["payload"]


def test_generic_schema_rejection_falls_back_with_attempt_diagnostics(monkeypatch):
    requests = _stub_http(monkeypatch, [(400, {"error": {"message": "unsupported response format"}}), _reply()])
    _, info = _judge(_client(base_url="https://example.test/v1"))
    assert [request["payload"]["response_format"]["type"] for request in requests] == ["json_schema", "json_object"]
    assert [attempt["status"] for attempt in info["attempts"]] == ["failed", "succeeded"]
    assert info["attempts"][0]["http_status"] == 400


def test_plain_fallback_requires_explicit_json_object_rejection(monkeypatch):
    requests = _stub_http(monkeypatch, [(422, {"error": {"message": "json_object is not supported"}}), _reply()])
    _, info = _judge(_client())
    assert "response_format" not in requests[1]["payload"]
    assert info["response_mode"] == "plain"
    assert "JudgeFormatUnsupportedError" in info["attempts"][0]["error"]


@pytest.mark.parametrize("reply", [
    _reply("not JSON"), _reply(None, reasoning="private reasoning"),
    _reply(finish_reason="length"), _reply('{"entity_matches": "wrong type"}'),
    (400, {"error": {"message": "invalid request"}}),
    (400, {"error": {"message": "model is not supported"}}),
    (400, {"error": {"message": "Invalid schema for response_format: keyword minimum is unsupported"}}),
    (401, {"error": {"message": "response_format is not supported"}}),
    (429, {"error": {"message": "response_format is not supported"}}),
    (500, {"error": {"message": "response_format is not supported"}}),
])
def test_non_format_failures_do_not_trigger_another_model_request(monkeypatch, reply):
    requests = _stub_http(monkeypatch, [reply])
    with pytest.raises(run.JudgeBatchError) as caught:
        _judge(_client())
    assert len(requests) == 1
    assert len(caught.value.judge_batches[0]["attempts"]) == 1


def test_timeout_is_not_retried_with_a_different_format(monkeypatch):
    calls = []

    def timeout(*args, **kwargs):
        calls.append(args)
        raise TimeoutError("request timed out")

    monkeypatch.setattr(run, "http_request", timeout)
    with pytest.raises(run.JudgeBatchError) as caught:
        _judge(_client())
    assert len(calls) == 1
    assert "TimeoutError" in caught.value.judge_batches[0]["attempts"][0]["error"]


def test_reasoning_is_never_used_as_final_json(monkeypatch):
    _stub_http(monkeypatch, [_reply(None, reasoning='{"entity_matches": []}')])
    client = _client()
    with pytest.raises(ValueError, match="reasoning_content 不能作为评分结果"):
        client.generate_structured(run._judge_schema("entity_matches"), [])
    assert client.last_diagnostics["content_length"] == 0
    assert client.last_diagnostics["reasoning_content_length"] == 22


def test_valid_content_takes_precedence_over_reasoning(monkeypatch):
    _stub_http(monkeypatch, [_reply(reasoning="private internal reasoning")])
    _, info = _judge(_client())
    assert "private internal reasoning" not in json.dumps(info)


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "tool_calls", "unexpected"])
def test_incomplete_or_filtered_responses_fail_even_with_valid_json(monkeypatch, finish_reason):
    _stub_http(monkeypatch, [_reply(finish_reason=finish_reason)])
    client = _client()
    with pytest.raises(ValueError, match="finish_reason="):
        client.generate_structured(run._judge_schema("entity_matches"), [])
    assert client.last_diagnostics["finish_reason"] == ("unknown" if finish_reason == "unexpected" else finish_reason)


@pytest.mark.parametrize("data", [
    {}, {"entity_matches": [], "explanation": "extra"}, {"entity_matches": None},
    {"entity_matches": {}}, {"entity_matches": [[0, 0]]},
    {"entity_matches": [{"golden_index": 0}]},
    {"entity_matches": [{"golden_index": 0, "predicted_index": 0, "confidence": 1}]},
    *[{"entity_matches": [{"golden_index": value, "predicted_index": 0}]} for value in (True, False, "0", 0.0, -1, None)],
    {"entity_matches": [{"golden_index": 0, "predicted_index": True}]},
])
def test_local_schema_validation_rejects_malformed_match_data(monkeypatch, data):
    _stub_http(monkeypatch, [_reply(json.dumps(data))])
    with pytest.raises(ValueError, match="judge JSON"):
        _client().generate_structured(run._judge_schema("entity_matches"), [])


def test_schema_validation_preserves_distinct_candidate_edges(monkeypatch):
    pairs = [{"golden_index": g, "predicted_index": p} for g, p in [(0, 0), (0, 0), (1, 1), (2, 1)]]
    _stub_http(monkeypatch, [_reply(json.dumps({"entity_matches": pairs}))])
    matches, _ = run._judge_one_batch(_client(), "entity_matches", [{}, {}, {}], [{}, {}], 4)
    assert matches == [(0, 4), (1, 5), (2, 5)]


@pytest.mark.parametrize("content", [
    '[{"entity_matches": []}]', 'Explanation: {"entity_matches": []}',
    '{"entity_matches": []} extra text', '{"entity_matches": []} {"entity_matches": []}',
])
def test_decoder_does_not_extract_an_object_from_an_invalid_document(monkeypatch, content):
    _stub_http(monkeypatch, [_reply(content)])
    with pytest.raises(ValueError):
        _client().generate_structured(run._judge_schema("entity_matches"), [])


def test_plain_markdown_code_fence_is_accepted(monkeypatch):
    _stub_http(monkeypatch, [_reply('```json\n{"entity_matches": []}\n```')])
    assert _client().generate_structured(run._judge_schema("entity_matches"), []) == {"entity_matches": []}


def test_failure_keeps_safe_diagnostics_in_fallback_metrics(monkeypatch):
    replies = [
        _reply(None, reasoning="private reasoning", finish_reason="length", usage={
            "completion_tokens": 8192, "completion_tokens_details": {"reasoning_tokens": 8192, "raw": "secret"},
            "provider_secret": "not for artifacts",
        }),
    ]
    _stub_http(monkeypatch, replies)
    golden = {"entities": [{"type": "T", "name": "golden-x"}], "relations": []}
    predicted = {"entities": [{"type": "T", "name": "predicted-x"}], "relations": []}
    metrics = run.score_against_golden(golden, predicted, judge_client=_client())
    assert metrics["match_mode"] == "identity_fallback"
    attempts = metrics["details"]["judge_batches"][0]["attempts"]
    assert attempts[0]["finish_reason"] == "length"
    assert attempts[0]["usage"] == {"completion_tokens": 8192, "completion_tokens_details": {"reasoning_tokens": 8192}}
    assert len(attempts) == 1
    serialized = json.dumps(metrics)
    for secret in ("test-secret-key", "another-secret", "private reasoning", "not for artifacts"):
        assert secret not in serialized


def test_provider_error_body_is_not_copied_into_artifacts(monkeypatch):
    error = {
        "message": "rejected test-secret-key and Bearer another-secret; original prompt: sensitive document text",
        "type": {"prompt": "nested type payload"}, "code": "secret-code-123", "param": "private-param-456",
        "request": {"messages": [{"role": "user", "content": "sensitive document text"}]},
        "details": {"token": "nested-token-789", "prompt": "nested prompt text"},
    }
    _stub_http(monkeypatch, [(400, {"error": error})])
    golden = {"entities": [{"type": "T", "name": "golden-x"}], "relations": []}
    predicted = {"entities": [{"type": "T", "name": "predicted-x"}], "relations": []}
    metrics = run.score_against_golden(golden, predicted, judge_client=_client())
    serialized = json.dumps(metrics)
    for secret in (
        "test-secret-key", "another-secret", "sensitive document text", "nested type payload",
        "secret-code-123", "private-param-456", "nested-token-789", "nested prompt text",
    ):
        assert secret not in serialized
    assert metrics["details"]["judge_batches"][0]["attempts"][0]["error_reason"] == "request_rejected"


def test_relation_failure_preserves_successful_entity_batch_diagnostics(monkeypatch):
    _stub_http(monkeypatch, [_reply(), _reply("not JSON")])
    golden = {
        "entities": [{"type": "T", "name": "golden-x"}],
        "relations": [{"from_type": "T", "from_name": "golden-x", "rel_type": "R", "to_type": "T", "to_name": "golden-y"}],
    }
    predicted = {
        "entities": [{"type": "T", "name": "predicted-x"}],
        "relations": [{"from_type": "T", "from_name": "predicted-x", "rel_type": "R", "to_type": "T", "to_name": "predicted-y"}],
    }
    metrics = run.score_against_golden(golden, predicted, judge_client=_client())
    batches = metrics["details"]["judge_batches"]
    assert [batch["collection"] for batch in batches] == ["entity_matches", "relation_matches"]
    assert [len(batch["attempts"]) for batch in batches] == [1, 1]


def test_build_judge_client_reads_configuration(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_LLM_BASE_URL", "https://api.deepseek.com/v1")
    client = run.build_judge_client({"eval": {"judge_model": "deepseek-flash", "judge_base_url": "https://api.deepseek.com/v1", "judge_response_format": "json_object", "judge_disable_thinking": False}})
    assert client.response_modes == ("json_object", "plain")
    assert client.disable_thinking is False


@pytest.mark.parametrize("options", [{"response_format": "xml"}, {"disable_thinking": "false"}])
def test_invalid_judge_configuration_is_rejected(options):
    with pytest.raises(ValueError):
        _client(**options)


@pytest.mark.parametrize("collection,golden_count,predicted_count", [
    ("entity_matches", 34, 20), ("relation_matches", 2, 7), ("entity_matches", 1, 1),
])
def test_judge_prompt_bounds_output_and_labels_each_input_position(collection, golden_count, predicted_count):
    golden = [{"type": "T", "name": f"golden-{i}"} for i in range(golden_count)]
    predicted = [{"type": "T", "name": f"predicted-{i}"} for i in range(predicted_count)]
    messages = run._build_judge_messages(collection, golden, predicted)
    prompt = messages[1]["content"]
    assert f"golden_index 只能是 0 到 {golden_count - 1} 的整数" in prompt
    assert f"predicted_index 只能是 0 到 {predicted_count - 1} 的整数" in prompt
    assert f"最多输出 {golden_count * predicted_count} 个候选 pair" in prompt
    assert "同一个 golden_index 或 predicted_index 可以出现在多个不同 pair 中，禁止重复相同 pair" in prompt
    assert "禁止复制实体、关系、属性或输入原文" in prompt
    assert "闭合最外层 } 后立即结束" in prompt
    assert "不继续生成其他对象、重复内容或填充空白" in prompt
    assert f'若没有匹配，输出 {{"{collection}": []}}' in prompt
    simplify = run._simple_entities if collection == "entity_matches" else run._simple_relations
    labeled_golden, labeled_predicted = _prompt_items(messages)
    assert labeled_golden == [dict(item, golden_index=i) for i, item in enumerate(simplify(golden))]
    assert labeled_predicted == [dict(item, predicted_index=i) for i, item in enumerate(simplify(predicted))]
    if predicted_count == 1:
        assert '"predicted_index": 1' not in prompt


def test_judge_prompt_empty_input_has_no_usable_index():
    prompt = run._build_judge_messages("entity_matches", [], [])[1]["content"]
    assert "最多输出 0 个候选 pair" in prompt
    assert "0 到 -1" not in prompt
    assert '若没有匹配，输出 {"entity_matches": []}' in prompt


@pytest.mark.parametrize("collection", ["entity_matches", "relation_matches"])
def test_explicit_indices_preserve_empty_duplicate_and_nested_input_items(collection):
    properties = {
        "golden_index": "domain-golden-index",
        "predicted_index": "domain-predicted-index",
        "nested": {"values": [1, {"label": "保留原始属性"}]},
    }
    if collection == "entity_matches":
        semantic_item = {"type": "Device", "name": "设备甲", "properties": properties}
        empty_item = {"type": None, "name": None, "properties": {}}
    else:
        semantic_item = {
            "from_type": "Device", "from_name": "设备甲", "rel_type": "locatedIn",
            "to_type": "Location", "to_name": "车间乙", "properties": properties,
        }
        empty_item = {
            "from_type": None, "from_name": None, "rel_type": None,
            "to_type": None, "to_name": None,
        }
    original = {
        **deepcopy(semantic_item),
        "golden_index": 811,
        "predicted_index": 912,
        "evidence": [{"quote": "原文证据", "source": {"range": [2, 8]}}],
    }
    golden = [deepcopy(original), {}, deepcopy(original)]
    predicted = [{}, deepcopy(original), deepcopy(original)]
    before = deepcopy((golden, predicted))

    labeled_golden, labeled_predicted = _prompt_items(run._build_judge_messages(collection, golden, predicted))

    assert labeled_golden == [
        {**semantic_item, "golden_index": 0},
        {**empty_item, "golden_index": 1},
        {**semantic_item, "golden_index": 2},
    ]
    assert labeled_predicted == [
        {**empty_item, "predicted_index": 0},
        {**semantic_item, "predicted_index": 1},
        {**semantic_item, "predicted_index": 2},
    ]
    assert (golden, predicted) == before


@pytest.mark.parametrize("collection,block", [
    ("entity_matches", "entities"), ("relation_matches", "relations"),
])
def test_batch_local_indices_map_once_to_global_items_and_match_details(monkeypatch, collection, block):
    def display(prefix, index):
        if collection == "entity_matches":
            return {"type": "Device", "name": f"{prefix}-device-{index}"}
        return {
            "from_type": "Device", "from_name": f"{prefix}-device-{index}",
            "rel_type": "locatedIn", "to_type": "Location", "to_name": f"{prefix}-location-{index}",
        }

    golden = [display("golden", i) for i in range(5)]
    predicted_order = [4, 1, 3, 0, 2]
    predicted = [display("predicted", i) for i in predicted_order]
    before = deepcopy((golden, predicted))
    replies = [
        _reply(json.dumps({collection: [
            {"golden_index": 4, "predicted_index": 0}, {"golden_index": 1, "predicted_index": 1},
        ]})),
        _reply(json.dumps({collection: [
            {"golden_index": 3, "predicted_index": 0}, {"golden_index": 0, "predicted_index": 1},
        ]})),
        _reply(json.dumps({collection: [{"golden_index": 2, "predicted_index": 0}]})),
    ]
    requests = _stub_http(monkeypatch, replies)
    arguments = {
        "golden_entities": golden if block == "entities" else [],
        "golden_relations": golden if block == "relations" else [],
        "predicted_entities": predicted if block == "entities" else [],
        "predicted_relations": predicted if block == "relations" else [],
    }

    metrics = run.llm_judge_score_case(**arguments, judge_client=_client(), batch_size=2)

    assert len(requests) == 3
    for batch_number, request in enumerate(requests):
        labeled_golden, labeled_predicted = _prompt_items(request["payload"]["messages"])
        start = batch_number * 2
        assert [item["golden_index"] for item in labeled_golden] == list(range(5))
        assert [item["predicted_index"] for item in labeled_predicted] == list(range(len(predicted[start:start + 2])))
        name_key = "name" if block == "entities" else "from_name"
        assert [item[name_key] for item in labeled_predicted] == [item[name_key] for item in predicted[start:start + 2]]
    assert [batch["predicted_start"] for batch in metrics["details"]["judge_batches"]] == [0, 2, 4]
    assert [batch["predicted_count"] for batch in metrics["details"]["judge_batches"]] == [2, 2, 1]
    assert metrics[block]["tp"] == 5
    assert metrics[block]["fp"] == metrics[block]["fn"] == 0
    assert metrics["details"][block] == {
        "matched": [{"golden": display("golden", i), "predicted": display("predicted", i)} for i in range(5)],
        "missing": [],
        "extra": [],
    }
    assert (golden, predicted) == before


# --- 语义 judge 口径：语义一致即判对 -------------------------------------------------

@pytest.mark.parametrize("golden_name,predicted_name", [
    ("中国中车股份有限公司", "中国中车股份有限公司"),
    ("Standard", "standard"),
    ("关于 开展 排查 工作的通知", "关于开展排查工作的通知"),
    ("《科技研究开发计划管理办法》", "科技研究开发计划管理办法"),
    ("GB/T 31486", "ＧＢ／Ｔ　３１４８６"),
])
def test_normalized_identity_is_matched_without_any_judge_call(monkeypatch, golden_name, predicted_name):
    requests = _stub_http(monkeypatch, [])
    metrics = run.llm_judge_score_case(
        [{"type": "Standard", "name": golden_name}], [], [{"type": "standard", "name": predicted_name}], [],
        judge_client=_client(),
    )
    assert requests == []
    assert metrics["entities"]["tp"] == 1
    assert metrics["details"]["judge_coverage"]["entities"]["auto_matched"] == 1
    assert metrics["details"]["judge_batches"] == []


def test_normalization_never_guesses_unrelated_punctuation(monkeypatch):
    """破折号与连字符不作为同一字符自动判对，交给 judge 做语义判断。"""
    requests = _stub_http(monkeypatch, [
        _reply('{"entity_matches": [{"golden_index": 0, "predicted_index": 0}]}'),
    ])
    metrics = run.llm_judge_score_case(
        [{"type": "Standard", "name": "GB/T 31486—2024"}], [],
        [{"type": "Standard", "name": "GB/T 31486-2024"}], [],
        judge_client=_client(),
    )
    assert len(requests) == 1
    assert metrics["entities"]["tp"] == 1
    assert metrics["details"]["judge_coverage"]["entities"]["auto_matched"] == 0


def test_agreeing_properties_keep_auto_match_and_extra_keys_are_ignored(monkeypatch):
    requests = _stub_http(monkeypatch, [])
    metrics = run.llm_judge_score_case(
        [{"type": "Constrain", "name": "初始容量极差约束", "properties": {"最大值": "5%"}}], [],
        [{"type": "Constrain", "name": "初始容量极差约束", "properties": {"最大值": "5 %", "单位": "%"}}], [],
        judge_client=_client(),
    )
    assert requests == []
    assert metrics["entities"]["tp"] == 1


def test_shared_property_conflict_is_not_auto_matched(monkeypatch):
    empty = _reply('{"entity_matches": []}')
    requests = _stub_http(monkeypatch, [empty, empty])
    metrics = run.llm_judge_score_case(
        [{"type": "Constrain", "name": "初始容量极差约束", "properties": {"最大值": "5%"}}], [],
        [{"type": "Constrain", "name": "初始容量极差约束", "properties": {"最大值": "10%"}}], [],
        judge_client=_client(),
    )
    # 属性冲突不能本地判对：一次全图扫描 + 一次针对该候选的聚焦补判
    # （整窗提示过大时 judge 会回空数组，把候选静默丢成漏抽）。
    assert len(requests) == 2
    assert metrics["entities"]["tp"] == 0
    coverage = metrics["details"]["judge_coverage"]["entities"]
    assert coverage["auto_matched"] == 0
    assert coverage["deferred_candidates"] == 1
    assert coverage["focused_windows"] == 1
    assert coverage["focused_empty_windows"] == 1


def test_semantically_equal_alias_without_lexical_overlap_is_accepted(monkeypatch):
    """judge 只看语义，不做字面预筛：别名/同义表达必须能判对。"""
    _stub_http(monkeypatch, [_reply('{"entity_matches": [{"golden_index": 0, "predicted_index": 0}]}')])
    metrics = run.llm_judge_score_case(
        [{"type": "DocumentUnit", "name": "通知正文"}], [],
        [{"type": "DocumentUnit", "name": "文件主体部分"}], [],
        judge_client=_client(),
    )
    assert metrics["entities"]["tp"] == 1


def test_one_to_one_resolution_is_maximum_not_first_come_first_served(monkeypatch):
    """(g0,p0)/(g0,p1)/(g1,p0) 的正确结果是 2 对；先到先得只会得到 1 对。"""
    _stub_http(monkeypatch, [_reply(json.dumps({"entity_matches": [
        {"golden_index": 0, "predicted_index": 0},
        {"golden_index": 0, "predicted_index": 1},
        {"golden_index": 1, "predicted_index": 0},
    ]}))])
    metrics = run.llm_judge_score_case(
        [{"type": "Device", "name": "设备甲"}, {"type": "Device", "name": "设备乙"}], [],
        [{"type": "Device", "name": "device A"}, {"type": "Device", "name": "device B"}], [],
        judge_client=_client(),
    )
    assert metrics["entities"]["tp"] == 2
    assert metrics["entities"]["fp"] == metrics["entities"]["fn"] == 0


def test_window_sweep_presents_every_pair_exactly_once(monkeypatch):
    golden = [{"type": "T", "name": f"g{i}"} for i in range(5)]
    predicted = [{"type": "T", "name": f"p{i}"} for i in range(7)]
    seen: list[tuple[str, str]] = []
    sizes: list[tuple[int, int]] = []

    def responder(payload):
        golden_items, predicted_items = _prompt_items(payload["messages"])
        sizes.append((len(golden_items), len(predicted_items)))
        for golden_item in golden_items:
            for predicted_item in predicted_items:
                seen.append((golden_item["name"], predicted_item["name"]))
        return _reply('{"entity_matches": []}')

    requests = _stub_http_responder(monkeypatch, responder)
    metrics = run.llm_judge_score_case(
        golden, [], predicted, [], judge_client=_client(), batch_size=2, golden_batch_size=3
    )
    assert len(requests) == 2 * 4  # ceil(5/3) × ceil(7/2)
    assert len(seen) == len(set(seen)) == 5 * 7
    assert max(golden_count for golden_count, _ in sizes) <= 3
    assert max(predicted_count for _, predicted_count in sizes) <= 2
    coverage = metrics["details"]["judge_coverage"]["entities"]
    assert coverage["pairs_total"] == coverage["pairs_presented"] == 35
    assert coverage["windows"] == 8
    assert coverage["empty_windows"] == 8


def test_large_golden_never_lands_in_one_oversized_prompt(monkeypatch):
    """回归：旧实现每批都塞全部 golden，399 条关系时模型回空数组且静默通过。"""
    def relation(index, prefix):
        return {
            "from_type": "T", "from_name": f"{prefix}-a{index}", "rel_type": "R",
            "to_type": "T", "to_name": f"{prefix}-b{index}",
        }

    golden = [relation(i, "golden") for i in range(120)]
    predicted = [relation(i, "predicted") for i in range(3)]
    sizes: list[tuple[int, int]] = []

    def responder(payload):
        golden_items, predicted_items = _prompt_items(payload["messages"])
        sizes.append((len(golden_items), len(predicted_items)))
        return _reply('{"relation_matches": []}')

    requests = _stub_http_responder(monkeypatch, responder)
    metrics = run.llm_judge_score_case(
        [], golden, [], predicted, judge_client=_client(), batch_size=20, golden_batch_size=40
    )
    assert len(requests) == 3  # ceil(120/40) × ceil(3/20)
    assert max(golden_count for golden_count, _ in sizes) <= 40
    coverage = metrics["details"]["judge_coverage"]["relations"]
    assert coverage["pairs_presented"] == coverage["pairs_total"] == 120 * 3
    assert coverage["empty_windows"] == 3
    assert metrics["relations"]["tp"] == 0


def test_judge_concurrency_preserves_results_and_per_batch_diagnostics(monkeypatch):
    golden = [{"type": "T", "name": f"g{i}"} for i in range(6)]
    predicted = [{"type": "T", "name": f"p{i}"} for i in range(6)]

    def responder(payload):
        golden_items, predicted_items = _prompt_items(payload["messages"])
        pairs = [{"golden_index": 0, "predicted_index": 0}] if golden_items and predicted_items else []
        return _reply(json.dumps({"entity_matches": pairs}))

    _stub_http_responder(monkeypatch, responder)
    arguments = {"batch_size": 2, "golden_batch_size": 3}
    sequential = run.llm_judge_score_case(golden, [], predicted, [], judge_client=_client(), max_concurrency=1, **arguments)
    concurrent = run.llm_judge_score_case(golden, [], predicted, [], judge_client=_client(), max_concurrency=4, **arguments)

    assert sequential["entities"] == concurrent["entities"]
    assert sequential["relations"] == concurrent["relations"]
    assert len(concurrent["details"]["judge_batches"]) == 6  # ceil(6/3) × ceil(6/2)
    for batch in concurrent["details"]["judge_batches"]:
        assert batch["attempts"][0]["status"] == "succeeded"
        assert batch["attempts"][0]["response_mode"] == "json_object"
        assert batch["golden_count"] == 3 and batch["predicted_count"] == 2


def test_empty_property_value_counts_as_missing_not_conflict(monkeypatch):
    """golden 有值、predicted 是空串/空数组时属于"没填"，不判冲突。"""
    requests = _stub_http(monkeypatch, [])
    metrics = run.llm_judge_score_case(
        [{"type": "DocumentUnit", "name": "通知正文", "properties": {"单元标题": "关于印发管理办法的通知", "单元类型": "正文"}}], [],
        [{"type": "DocumentUnit", "name": "通知正文", "properties": {"单元标题": "", "单元类型": "正文", "原文内容": []}}], [],
        judge_client=_client(),
    )
    assert requests == []
    assert metrics["entities"]["tp"] == 1
