from types import SimpleNamespace

from algorithm.knowledge_extract.v3.table_evidence import rule_candidate_in_scope, table_source_span


def _decision(*rows, reasons=()):
    return SimpleNamespace(rule_types=("Rule",), rule_rows=rows, reasons=reasons)


def _entity(start, end, kind="Rule"):
    return SimpleNamespace(
        canonical_type=kind,
        proposed_type=kind,
        evidence=[SimpleNamespace(start_char=start, end_char=end)],
    )


def _chunk(text):
    return SimpleNamespace(text=text)


def test_mixed_table_rejects_ordinary_row_and_keeps_rule_row():
    text = '''```json
{"columns":["设备","状态","处理措施"],"rows":[
  {"设备":"泵","状态":"正常","处理措施":"巡检"},
  {"设备":"阀门","状态":"如果压力过高","处理措施":"必须停机"}
]}
```'''
    ordinary = text.index("巡检")
    rule = text.index("如果压力过高")
    decision = _decision((1, 2))
    assert not rule_candidate_in_scope(_chunk(text), decision, _entity(ordinary, ordinary + 2))
    assert rule_candidate_in_scope(_chunk(text), decision, _entity(rule, rule + len("如果压力过高")))


def test_header_and_column_ranges_are_not_rule_evidence():
    text = '{"columns":["condition","action"],"rows":[{"condition":"温度高","action":"停机"}]}'
    decision = _decision((1, 1))
    header = text.index("condition")
    assert not rule_candidate_in_scope(_chunk(text), decision, _entity(header, header + len("condition")))


def test_complete_selected_record_quote_is_allowed_when_it_contains_a_value():
    text = '{"rows":[{"condition":"温度高","action":"停机"}]}'
    start = text.index('{"condition"')
    end = text.index('}', start) + 1
    assert rule_candidate_in_scope(_chunk(text), _decision((1, 1)), _entity(start, end))


def test_multiple_tables_bind_by_table_and_row_number():
    text = '''```json
{"rows":[{"name":"普通记录"}]}
```
```json
{"rows":[{"condition":"低压","action":"报警"}]}
```'''
    start = text.index("低压")
    assert rule_candidate_in_scope(_chunk(text), _decision((2, 1)), _entity(start, start + 2))
    first = text.index("普通记录")
    assert not rule_candidate_in_scope(_chunk(text), _decision((2, 1)), _entity(first, first + 4))


def test_duplicate_values_use_offsets_instead_of_first_text_match():
    text = '{"rows":[{"condition":"重复","action":"普通"},{"condition":"重复","action":"规则"}]}'
    second = text.rindex("重复")
    assert rule_candidate_in_scope(_chunk(text), _decision((1, 2)), _entity(second, second + 2))
    first = text.index("重复")
    assert not rule_candidate_in_scope(_chunk(text), _decision((1, 2)), _entity(first, first + 2))


def test_rule_in_json_outside_selected_row_is_rejected_but_prose_is_kept():
    text = '正文：如果温度过高则停机。\n```json\n{"rows":[{"condition":"正常","action":"巡检"},{"condition":"低压","action":"报警"}]}\n```'
    prose = text.index("如果温度")
    table = text.index("正常")
    decision = _decision((1, 2))
    assert rule_candidate_in_scope(_chunk(text), decision, _entity(prose, prose + len("如果温度过高则停机")))
    assert not rule_candidate_in_scope(_chunk(text), decision, _entity(table, table + 2))


def test_non_rule_candidate_and_non_table_are_not_gated():
    text = '{"rows":[{"condition":"异常","action":"停机"}]}'
    start = text.index("异常")
    assert rule_candidate_in_scope(_chunk(text), _decision((1, 1)), _entity(start, start + 2, "Equipment"))
    assert rule_candidate_in_scope(_chunk("正文规则"), _decision(reasons=("source.not_table",)), _entity(0, 2))


def test_unparseable_table_decision_fails_closed():
    decision = _decision((1, 1))
    assert not rule_candidate_in_scope(_chunk("```json\n{broken\n```"), decision, _entity(10, 16))


def test_table_source_span_uses_duplicate_occurrence_offset():
    text = '{"rows":[{"condition":"重复","action":"一"},{"condition":"重复","action":"二"}]}'
    start = text.rindex("重复")
    entity = _entity(start, start + 2)
    entity.evidence[0].quote = "重复"
    entity.mention = "重复"
    row_start = text.index('{"condition":"重复","action":"二"}')
    assert table_source_span(_chunk(text), _chunk(text), entity) == (
        start,
        start + 2,
    )
    assert table_source_span(_chunk(text), _chunk(text), _entity(row_start, row_start + 30)) is not None


def test_table_source_span_maps_window_row_after_json_equality_check():
    original = '{"rows":[' + ','.join('{"condition":"条件%d","action":"动作%d"}' % (i, i) for i in range(1, 25)) + ']}'
    source = '{"rows":[' + ','.join('{"condition":"条件%d","action":"动作%d"}' % (i, i) for i in range(13, 17)) + ']}'
    start = source.index("条件15")
    entity = _entity(start, start + 3)
    entity.evidence[0].quote = "条件15"
    source_chunk = SimpleNamespace(text=source, metadata={"table_source_block_index": 1, "table_data_row_start": 13})
    mapped = table_source_span(source_chunk, _chunk(original), entity)
    expected_start = original.index('{"condition":"条件15"')
    expected_end = original.index('}', expected_start) + 1
    assert mapped == (expected_start, expected_end)


def test_table_source_span_maps_selected_block_and_rejects_mismatch():
    original = '```json\n{"rows":[{"condition":"一","action":"A"}]}\n```\n```json\n{"rows":[{"condition":"二","action":"B"}]}\n```'
    source = '{"rows":[{"condition":"二","action":"B"}]}'
    start = source.index("二")
    entity = _entity(start, start + 1)
    entity.evidence[0].quote = "二"
    source_chunk = SimpleNamespace(text=source, metadata={"table_source_block_index": 2, "table_data_row_start": 1})
    mapped = table_source_span(source_chunk, _chunk(original), entity)
    expected_start = original.index('{"condition":"二"')
    expected_end = original.index('}', expected_start) + 1
    assert mapped == (expected_start, expected_end)
    bad = SimpleNamespace(text=source.replace('"B"', '"C"'), metadata={"table_source_block_index": 2, "table_data_row_start": 1})
    assert table_source_span(bad, _chunk(original), entity) is None
