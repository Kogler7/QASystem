"""Relation judging retains same-side endpoint context without inventing TP."""

import json
from copy import deepcopy

import pytest

from eval import run


def _entity(etype, name, **properties):
    return {"type": etype, "name": name, "properties": properties}


def _relation(source="通知", target="排查要求二"):
    return {"from_type": "OfficialDocument", "from_name": source, "rel_type": "HAS_PART",
            "to_type": "DocumentUnit", "to_name": target}


def _section(prompt, heading, next_heading):
    return json.loads(prompt.split(f"## {heading}\n", 1)[1].split(f"\n\n## {next_heading}", 1)[0])


def test_context_preserves_duplicate_identity_candidates_without_merging_or_mutating():
    entities = [
        _entity("OfficialDocument", "通知", 文件标题="风险排查通知"),
        _entity("DocumentUnit", "排查要求二", 原文内容="内部办公网络与互联网有效隔离", 顺序号="二"),
        _entity("DocumentUnit", "排查要求二", 原文内容="备用文档中不同的第二条", 来源="备用"),
        _entity("DocumentUnit", "排查要求二", 原文内容="内部办公网络与互联网有效隔离", 顺序号="二"),
        _entity("OperationalActivity", "排查要求二", 动作词="不应混入另一个类型"),
    ]
    entities[1]["evidence"] = {"quote": "excluded evidence marker"}
    relations = [_relation(), {**_relation(), "properties": {"顺序": "第二"}}]
    before = deepcopy((entities, relations))

    enriched = run._relations_with_entity_context(relations, entities)
    simplified = run._simple_relations(enriched)
    table = run._relation_endpoint_table(simplified)

    target = next(item for item in table if item["type"] == "DocumentUnit")
    assert target == {"type": "DocumentUnit", "name": "排查要求二", "candidates": [
        {"properties": {"原文内容": "内部办公网络与互联网有效隔离", "顺序号": "二"}},
        {"properties": {"原文内容": "备用文档中不同的第二条", "来源": "备用"}},
    ]}
    assert len(table) == 2
    assert simplified[1]["properties"] == {"顺序": "第二"}
    assert "excluded evidence marker" not in json.dumps(table)
    assert (entities, relations) == before


def test_relation_prompt_deduplicates_long_shared_context_and_keeps_item_indices():
    entities = [_entity("OfficialDocument", "通知", 原文内容="shared-long-document-marker"),
                _entity("DocumentUnit", "排查要求二", 原文内容="second-section-marker")]
    relations = [_relation(), {**_relation(), "rel_type": "REFERENCES"}]
    enriched = run._relations_with_entity_context(relations, entities)
    before = deepcopy(enriched)

    messages = run._build_judge_messages("relation_matches", enriched, [])
    prompt = messages[1]["content"]
    table = _section(prompt, "golden endpoint context", "predicted endpoint context")
    items = _section(prompt, "golden items", "predicted items")

    assert prompt.count("shared-long-document-marker") == 1
    assert prompt.count("second-section-marker") == 1
    assert len(table) == 2
    assert items == [{**relation, "golden_index": i} for i, relation in enumerate(relations)]
    assert all("from_entity_context" not in item and "to_entity_context" not in item for item in items)
    assert "短标签" in messages[0]["content"]
    assert "不是已判对的实体映射" in messages[0]["content"]
    assert enriched == before


def test_context_is_limited_to_this_window_and_keeps_golden_predicted_sides_separate():
    golden_entities = [_entity("OfficialDocument", "通知", 文件标题="gold-document"),
                       _entity("DocumentUnit", "排查要求二", 原文内容="gold-second"),
                       _entity("DocumentUnit", "其他条", 原文内容="outside-window")]
    predicted_entities = [_entity("OfficialDocument", "通知", 文件标题="pred-document"),
                          _entity("DocumentUnit", "排查要求二", 原文内容="pred-second")]
    golden = run._relations_with_entity_context([_relation(), _relation(target="其他条")], golden_entities)
    predicted = run._relations_with_entity_context([_relation()], predicted_entities)

    prompt = run._build_judge_messages("relation_matches", golden[:1], predicted)[1]["content"]
    golden_table = _section(prompt, "golden endpoint context", "predicted endpoint context")
    predicted_table = _section(prompt, "predicted endpoint context", "golden items")

    assert "outside-window" not in prompt
    assert "gold-second" in json.dumps(golden_table)
    assert "pred-second" not in json.dumps(golden_table)
    assert "pred-second" in json.dumps(predicted_table)
    assert "gold-second" not in json.dumps(predicted_table)


def test_absent_endpoint_entities_are_not_invented():
    enriched = run._relations_with_entity_context([_relation()], [])
    assert enriched[0]["from_entity_context"] == []
    assert enriched[0]["to_entity_context"] == []
    table = run._relation_endpoint_table(enriched)
    assert len(table) == 2
    assert all(item["candidates"] == [] for item in table)


@pytest.mark.parametrize("judge_accepts", [False, True])
def test_short_endpoint_context_reaches_judge_without_automatic_tp(monkeypatch, judge_accepts):
    text = "二、各企业要严格执行内部办公网络与互联网有效隔离。"
    golden_entities = [_entity("OfficialDocument", "通知"),
                       _entity("DocumentUnit", "排查要求二", 原文内容=text, 顺序号="二")]
    predicted_entities = [_entity("OfficialDocument", "通知"),
                          _entity("DocumentUnit", text, 原文内容=text, 顺序号="2")]
    golden_relations = [_relation()]
    predicted_relations = [_relation(target=text)]
    before = deepcopy((golden_entities, predicted_entities, golden_relations, predicted_relations))
    relation_requests = []

    def request(_url, **kwargs):
        payload = json.loads(kwargs["data"])
        collection = next(iter(payload["response_format"]["json_schema"]["schema"]["properties"]))
        pairs = []
        if collection == "relation_matches":
            prompt = payload["messages"][1]["content"]
            relation_requests.append(prompt)
            golden_table = _section(prompt, "golden endpoint context", "predicted endpoint context")
            predicted_table = _section(prompt, "predicted endpoint context", "golden items")
            assert next(item for item in golden_table if item["name"] == "排查要求二")["candidates"][0]["properties"]["原文内容"] == text
            assert next(item for item in predicted_table if item["name"] == text)["candidates"][0]["properties"]["原文内容"] == text
            if judge_accepts:
                pairs = [{"golden_index": 0, "predicted_index": 0}]
        body = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({collection: pairs})}}]}
        return 200, {}, json.dumps(body).encode()

    monkeypatch.setattr(run, "http_request", request)
    client = run.JudgeClient(base_url="https://judge.example.test/v1", api_key="fixture", model="fixture", timeout=30, max_tokens=2048)
    metrics = run.llm_judge_score_case(golden_entities, golden_relations, predicted_entities, predicted_relations, client)

    assert len(relation_requests) == 1
    assert metrics["relations"]["tp"] == int(judge_accepts)
    # Entity judging returned no alias candidate; relation context is still
    # available independently, and does not manufacture an entity match.
    assert metrics["entities"]["tp"] == 1
    assert metrics["aligned_relations"]["tp"] == 0
    assert (golden_entities, predicted_entities, golden_relations, predicted_relations) == before


@pytest.mark.parametrize("endpoint", ["from", "to"])
@pytest.mark.parametrize("judge_accepts", [False, True])
def test_same_label_endpoint_conflicts_require_relation_judging(monkeypatch, endpoint, judge_accepts):
    golden_entities = [_entity("OfficialDocument", "通知"), _entity("DocumentUnit", "排查要求二")]
    predicted_entities = deepcopy(golden_entities)
    index = 0 if endpoint == "from" else 1
    golden_entities[index]["properties"] = {"原文内容": "允许连接外部网络"}
    predicted_entities[index]["properties"] = {"原文内容": "禁止连接外部网络"}
    requests = []

    def request(_url, **kwargs):
        payload = json.loads(kwargs["data"])
        collection = next(iter(payload["response_format"]["json_schema"]["schema"]["properties"]))
        requests.append(collection)
        pairs = [{"golden_index": 0, "predicted_index": 0}] if collection == "relation_matches" and judge_accepts else []
        body = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({collection: pairs})}}]}
        return 200, {}, json.dumps(body).encode()

    monkeypatch.setattr(run, "http_request", request)
    client = run.JudgeClient(base_url="https://judge.example.test/v1", api_key="fixture", model="fixture", timeout=30, max_tokens=2048)
    metrics = run.llm_judge_score_case(golden_entities, [_relation()], predicted_entities, [_relation()], client)

    assert requests.count("relation_matches") == 1
    assert metrics["details"]["judge_coverage"]["relations"]["auto_candidate_pairs"] == 0
    assert metrics["relations"]["tp"] == int(judge_accepts)


def test_all_endpoint_candidate_combinations_must_agree_for_local_relation_match():
    golden_entities = [_entity("DocumentUnit", "排查要求二", 原文内容="允许联网"),
                       _entity("DocumentUnit", "排查要求二", 原文内容="禁止联网")]
    predicted_entities = [_entity("DocumentUnit", "排查要求二", 原文内容="允许联网")]
    golden = run._relations_with_entity_context([_relation()], golden_entities)
    predicted = run._relations_with_entity_context([_relation()], predicted_entities)
    assert run._auto_candidate_pairs(golden, predicted, relation=True) == []
    # Identical candidate sets are still ambiguous; checking only the first or
    # one compatible pair would bypass the conflicting interpretation.
    assert run._auto_candidate_pairs(golden, golden, relation=True) == []
    assert run._auto_candidate_pairs(predicted, predicted, relation=True) == [(0, 0)]


@pytest.mark.parametrize("predicted_entities", [[], [_entity("DocumentUnit", "排查要求二")],
    [_entity("DocumentUnit", "排查要求二", 顺序号="二")],
    [_entity("DocumentUnit", "排查要求二", 原文内容="允许联网")],
])
def test_missing_or_compatible_endpoint_properties_do_not_require_full_context(predicted_entities):
    golden = run._relations_with_entity_context([_relation()], [_entity("DocumentUnit", "排查要求二", 原文内容="允许联网")])
    predicted = run._relations_with_entity_context([_relation()], predicted_entities)
    assert run._auto_candidate_pairs(golden, predicted, relation=True) == [(0, 0)]
    assert run._auto_candidate_pairs(predicted, golden, relation=True) == [(0, 0)]
