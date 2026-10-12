import json

import pytest

from eval.tools.adaptive_chunk_probe import plan_adaptive


def assert_lossless(source, chunks):
    covered = [False] * len(source)
    for chunk in chunks:
        start, end = chunk["range"]
        assert chunk["text"] == source[start:end]
        assert 0 <= start < end <= len(source)
        covered[start:end] = [True] * (end - start)
    assert all(covered)


@pytest.mark.parametrize("density", ["low", "medium", "high"])
def test_offsets_overlap_and_all_characters_preserved(density):
    source = "  前言。\n\n" + "甲乙丙丁！重复文字。\n" * 100 + "\n\t  "
    chunks = plan_adaptive(source, lambda _: density, base_size=100, overlap=15)
    assert_lossless(source, chunks)
    assert all(len(c["text"]) <= c["target_size"] for c in chunks)


def test_sizes_monotonic_and_classifier_probes_identical():
    source = "abcdefghij" * 100
    outputs = {}
    probes = {}
    for density in ["low", "medium", "high"]:
        probes[density] = []
        def classify(text):
            probes[density].append(text)
            return {"label": density, "confidence": 0.9}
        outputs[density] = plan_adaptive(source, classify, base_size=100, overlap=10)
    assert probes["low"] == probes["medium"] == probes["high"]
    assert all(len(p) == 100 for p in probes["low"])
    assert len(outputs["low"]) < len(outputs["medium"]) < len(outputs["high"])
    assert [outputs[d][0]["target_size"] for d in ["low", "medium", "high"]] == [200, 100, 50]


@pytest.mark.parametrize("fenced", [True, False])
def test_table_json_never_split_or_classified(fenced):
    payload = {"columns": ["a"], "rows": [{"a": str(i) * 30} for i in range(30)]}
    encoded = json.dumps(payload, ensure_ascii=False)
    block = "## 表格\n```json\n" + encoded + "\n```\n" if fenced else encoded
    source = "前言。" * 40 + "\n" + block + "\n\n" + "后记。" * 40
    seen = []
    def classify(text):
        seen.append(text)
        return "high"
    chunks = plan_adaptive(source, classify, base_size=80, overlap=10)
    assert_lossless(source, chunks)
    protected = [c for c in chunks if c["protected"]]
    assert len(protected) == 1
    assert encoded in protected[0]["text"]
    assert protected[0]["downstream_policy"] == "structured_row_segments"
    assert len(protected[0]["text"]) > protected[0]["target_size"]
    assert json.loads(protected[0]["text"].split("```json\n")[1].split("\n```")[0]
                      if fenced else protected[0]["text"]) == payload
    assert all(encoded not in probe for probe in seen)


@pytest.mark.parametrize("answer,reason", [
    ({"label": "high", "confidence": 0.2}, "low_confidence"),
    ({"label": "high", "confidence": float("nan")}, "invalid_confidence"),
    ({"label": "low", "confidence": 2}, "invalid_confidence"),
    ({"label": "low"}, "low_confidence"),
    ("discard", "invalid_label"),
    (None, "invalid_response"),
])
def test_uncertain_or_invalid_classification_falls_back(answer, reason):
    source = "稀疏正文。" * 100
    chunks = plan_adaptive(source, lambda _: answer, base_size=80, overlap=5)
    assert_lossless(source, chunks)
    assert all(c["density"] == "medium" and c["target_size"] == 80
               and c["fallback_reason"] == reason for c in chunks)


def test_classifier_exception_falls_back_without_recording_message():
    def broken(_):
        raise RuntimeError("sensitive request contents")
    source = "一二三四" * 100
    chunks = plan_adaptive(source, broken, base_size=80)
    assert_lossless(source, chunks)
    assert all(c["fallback_reason"] == "classifier_error:RuntimeError" for c in chunks)
    assert "sensitive" not in repr(chunks)


def test_whitespace_is_retained_and_empty_document_has_no_calls():
    assert_lossless(" \n\t ", plan_adaptive(" \n\t ", lambda _: "low"))
    assert plan_adaptive("", lambda _: pytest.fail("unexpected classifier call")) == []


def test_large_whitespace_never_exceeds_fixed_probe_budget():
    source = " \n\t" * 100
    probes = []
    def classify(text):
        probes.append(text)
        return "high"
    chunks = plan_adaptive(source, classify, base_size=80, overlap=5)
    assert_lossless(source, chunks)
    assert max(map(len, probes)) <= 80
    assert all(len(c["text"]) <= 40 for c in chunks)


def test_adaptive_groups_do_not_overlap_tables_or_change_policy_boundaries():
    source = "低" * 160 + "高" * 160
    chunks = plan_adaptive(source, lambda text: "low" if text[0] == "低" else "high",
                           base_size=80, overlap=10)
    assert_lossless(source, chunks)
    assert all(c["range"][1] <= 160 for c in chunks if c["density"] == "low")
    assert all(c["range"][0] >= 160 for c in chunks if c["density"] == "high")


@pytest.mark.parametrize("outer_pipes", [True, False])
def test_markdown_pipe_table_remains_atomic_with_heading(outer_pipes):
    def row(cells):
        body = " | ".join(cells)
        return ("| " + body + " |" if outer_pipes else body) + "\n"
    table = "## 费用明细\n\n" + row(["编号", "说明"])
    table += row([":---", "---:"])
    table += "".join(row([str(i), "长说明" * 20]) for i in range(20))
    source = "引言。" * 50 + "\n\n" + table + "\n末尾。" * 30
    probes = []
    def classify(text):
        probes.append(text)
        return "high"
    chunks = plan_adaptive(source, classify, base_size=80, overlap=10)
    assert_lossless(source, chunks)
    tables = [c for c in chunks if c["protected_kind"] == "pipe_table"]
    assert len(tables) == 1
    assert tables[0]["text"] == table
    assert tables[0]["density"] == "protected"
    assert tables[0]["downstream_policy"] == "preserve_pipe_table"
    assert all("费用明细" not in probe and "长说明" not in probe for probe in probes)
    start, end = tables[0]["range"]
    assert all(c["range"][1] <= start or c["range"][0] >= end
               for c in chunks if not c["protected"])


def test_prose_pipes_are_not_misclassified_as_tables():
    source = "条件甲 | 条件乙\n此处 | 是正文\n" * 20
    chunks = plan_adaptive(source, lambda _: "high", base_size=80, overlap=5)
    assert_lossless(source, chunks)
    assert not any(c["protected"] for c in chunks)


def test_pipe_syntax_inside_json_fence_keeps_one_fenced_block():
    source = '## 表格\n```json\n{"description":"a | b\\n--- | ---", "rows":[]}\n```\n'
    chunks = plan_adaptive(source, lambda _: pytest.fail("protected input classified"), base_size=30, overlap=5)
    assert_lossless(source, chunks)
    assert len(chunks) == 1
    assert chunks[0]["text"] == source
    assert chunks[0]["protected"] and chunks[0]["protected_kind"] is None
