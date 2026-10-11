"""回归：本地无法确认的候选对必须单独补判，不能被整窗空数组静默吞成漏抽。

真实故障（88 硅基流动 v3.3 mini，official_02）：关系窗口一次 40×8=320 对，judge 返回
HTTP 200 + `{"relation_matches": []}`，能过 schema 校验，于是两条五元组逐字一致的候选
被判成漏抽、关系 F1 归零；同模型同数据换成 2×2 小窗口后立刻判出这两对。
"""

import json

import pytest

from eval import run


def _client(**overrides):
    options = {
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "test-secret-key",
        "model": "test-model",
        "timeout": 30,
        "max_tokens": 8192,
    }
    options.update(overrides)
    return run.JudgeClient(**options)


def _reply(content):
    return {
        "id": "chatcmpl-test",
        "choices": [{"finish_reason": "stop", "message": {"content": content, "reasoning_content": None}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


def _prompt_items(messages):
    prompt = messages[1]["content"]
    golden_json = prompt.split("## golden items\n", 1)[1].split("\n\n## predicted items", 1)[0]
    predicted_json = prompt.split("## predicted items\n", 1)[1].split("\n\n请只输出", 1)[0]
    return json.loads(golden_json), json.loads(predicted_json)


def _stub_http_responder(monkeypatch, responder):
    import threading

    requests = []
    lock = threading.Lock()

    def request(url, **kwargs):
        payload = json.loads(kwargs["data"])
        with lock:
            requests.append(payload)
        return 200, {}, json.dumps(responder(payload)).encode()

    monkeypatch.setattr(run, "http_request", request)
    return requests


def _entity(etype, name, **properties):
    return {"type": etype, "name": name, "properties": properties}


def _relation(source, target, rel_type="REQUIRES_ACTIVITY"):
    return {"from_type": "OfficialDocument", "from_name": source, "rel_type": rel_type,
            "to_type": "OperationalActivity", "to_name": target}


DOC = "关于印发《轨道交通装备行业高质量数据集链主任务工作方案》的通知"


def test_entity_identity_with_conflicting_properties_is_deferred():
    golden = [_entity("OfficialDocument", DOC, 密级="核心商密保密期限3年"),
              _entity("OperationalAgent", "信息公司", 全称="中车信息技术有限公司")]
    predicted = [_entity("OfficialDocument", DOC, 密级="中国中车核心商密3年"),
                 _entity("OperationalAgent", "信息公司", 全称="中车信息技术有限公司")]

    assert run._deferred_identity_pairs(golden, predicted, relation=False) == [(0, 0)]
    assert run._auto_candidate_pairs(golden, predicted, relation=False) == [(1, 1)]


def test_relation_identity_with_conflicting_endpoint_context_is_deferred():
    golden_entities = [_entity("OfficialDocument", DOC, 密级="核心商密保密期限3年"),
                       _entity("OperationalActivity", "加强资源投入", 活动类型="管理")]
    predicted_entities = [_entity("OfficialDocument", DOC, 密级="中国中车核心商密3年"),
                          _entity("OperationalActivity", "加强资源投入", 活动类型="实施")]
    golden = run._relations_with_entity_context([_relation(DOC, "加强资源投入")], golden_entities)
    predicted = run._relations_with_entity_context([_relation(DOC, "加强资源投入")], predicted_entities)

    assert run._auto_candidate_pairs(golden, predicted, relation=True) == []
    assert run._deferred_identity_pairs(golden, predicted, relation=True) == [(0, 0)]


def test_type_conflict_and_different_identity_are_not_deferred():
    golden = [{"type": "Device", "name": "设备甲"}]
    predicted = [{"type": "OperationalActivity", "name": "设备甲"}, {"type": "Device", "name": "别的设备"}]

    assert run._deferred_identity_pairs(golden, predicted, relation=False) == []


def test_oversized_window_empty_reply_is_recovered_by_focused_judging(monkeypatch):
    """故障复现：整窗回空数组时，聚焦补判仍须把那两条候选判出来。"""
    golden = [_relation(DOC, f"活动{i}") for i in range(6)] + [_relation(DOC, "加强资源投入"), _relation(DOC, "健全项目管理机制")]
    predicted = [_relation(DOC, "加强资源投入"), _relation(DOC, "健全项目管理机制")]
    # 端点上下文冲突：只有 judge 能确认指代，本地自动匹配必须回避
    golden_entities = [_entity("OfficialDocument", DOC, 密级="核心商密保密期限3年")] + [
        _entity("OperationalActivity", name, 活动类型="管理") for name in
        ["加强资源投入", "健全项目管理机制"] + [f"活动{i}" for i in range(6)]
    ]
    predicted_entities = [_entity("OfficialDocument", DOC, 密级="中国中车核心商密3年"),
                          _entity("OperationalActivity", "加强资源投入", 活动类型="实施"),
                          _entity("OperationalActivity", "健全项目管理机制", 活动类型="管理")]
    sizes: list[tuple[int, int]] = []

    windows: list[tuple[str, int, int]] = []

    def responder(payload):
        prompt = payload["messages"][1]["content"]
        collection = "relation_matches" if '"relation_matches"' in prompt else "entity_matches"
        golden_items, predicted_items = _prompt_items(payload["messages"])
        windows.append((collection, len(golden_items), len(predicted_items)))
        if collection == "entity_matches":
            return _reply('{"entity_matches": []}')
        # 真实模型行为：候选对一多就整窗回空数组（能过 schema 校验）
        if len(golden_items) * len(predicted_items) > 8:
            return _reply('{"relation_matches": []}')
        matches = [{"golden_index": g["golden_index"], "predicted_index": p["predicted_index"]}
                   for g in golden_items for p in predicted_items if g["to_name"] == p["to_name"]]
        return _reply(json.dumps({"relation_matches": matches}))

    _stub_http_responder(monkeypatch, responder)
    metrics = run.llm_judge_score_case(
        golden_entities, golden, predicted_entities, predicted,
        judge_client=_client(), batch_size=20, golden_batch_size=40,
    )

    relation_windows = [(g, p) for collection, g, p in windows if collection == "relation_matches"]
    coverage = metrics["details"]["judge_coverage"]["relations"]
    assert coverage["pairs_total"] == 16  # 8 golden × 2 predicted 一次全图扫描
    assert coverage["empty_windows"] == 1  # 整窗空数组（原缺陷现场）
    assert coverage["deferred_candidates"] == 2
    assert coverage["focused_windows"] == 1
    assert coverage["focused_candidate_pairs"] == 2
    assert coverage["focused_empty_windows"] == 0
    assert coverage["llm_matched"] == 2
    assert (8, 2) in relation_windows and (2, 2) in relation_windows  # 补判窗口只含相关条目
    assert metrics["relations"]["tp"] == 2
    assert metrics["relations"]["fp"] == 0 and metrics["relations"]["fn"] == 6
    focused = [batch for batch in metrics["details"]["judge_batches"]
               if batch.get("focused") and batch["collection"] == "relation_matches"]
    assert len(focused) == 1  # 实体侧另有自己的补判窗口，不混在一起
    assert focused[0]["golden_indices"] == [6, 7] and focused[0]["predicted_indices"] == [0, 1]


def test_saturated_local_matching_skips_judge_and_focused_judging(monkeypatch):
    """本地已确认全部较小侧时不再调用 judge，也不做补判（分数不可能再提高）。"""
    golden = [{"type": "Device", "name": "设备甲", "properties": {"型号": "A"}}]
    predicted = [{"type": "Device", "name": "设备甲", "properties": {"型号": "A"}}]

    def responder(payload):  # pragma: no cover - 不应被调用
        raise AssertionError("本地匹配饱和时不应调用 judge")

    _stub_http_responder(monkeypatch, responder)
    metrics = run.llm_judge_score_case(
        golden, [], predicted, [], judge_client=_client(), batch_size=20, golden_batch_size=40
    )
    coverage = metrics["details"]["judge_coverage"]["entities"]
    assert coverage["auto_matched"] == 1
    assert coverage["full_sweep"] is False
    assert coverage["deferred_candidates"] == 0
    assert coverage["focused_windows"] == 0
    assert metrics["entities"]["tp"] == 1


def test_focused_window_empty_reply_still_counts_as_no_match(monkeypatch):
    """补判窗口是小窗口：模型仍回空数组就是有依据的“不匹配”，不得伪造 TP。"""
    golden = [{"type": "Device", "name": "设备甲", "properties": {"型号": "A"}}]
    predicted = [{"type": "Device", "name": "设备甲", "properties": {"型号": "B"}}]
    _stub_http_responder(monkeypatch, lambda payload: _reply('{"entity_matches": []}'))

    metrics = run.llm_judge_score_case(
        golden, [], predicted, [], judge_client=_client(), batch_size=20, golden_batch_size=40
    )

    coverage = metrics["details"]["judge_coverage"]["entities"]
    assert coverage["deferred_candidates"] == 1
    assert coverage["focused_windows"] == 1
    assert coverage["focused_empty_windows"] == 1
    assert coverage["focused_candidate_pairs"] == 0
    assert metrics["entities"]["tp"] == 0
