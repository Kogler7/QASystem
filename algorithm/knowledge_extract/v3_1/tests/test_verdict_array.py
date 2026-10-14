"""Only complete, schema-authorized live verdict arrays are transport-normalized."""
import json

import pytest

from .. import candidate_generation as cg
from ..llm_client import StructuredLLMResponse
from ..output_contracts import verification_schema


ROWS = [
    {'item_id': 'a', 'supported': 'yes', 'reason': '原文明确支持甲'},
    {'item_id': 'b', 'supported': 'uncertain', 'reason': '原文缺少乙的必要条件'},
]
SCHEMA = verification_schema(ROWS)


def generate(responses, schema=SCHEMA):
    attempts, calls, seen = [], [], []
    sequence = iter(responses)
    items = [{'item_id': key} for key in schema['properties']['verdicts']['items']['properties']['item_id']['enum']]
    def llm(messages):
        seen.append(messages)
        return next(sequence)
    result = cg.generate_structured(llm,
        [{'role': 'system', 'content': 'JSONL'},
         {'role': 'user', 'content': json.dumps({'task': {'operation': 'evidence_verification',
                                                        'items': items}})}],
        schema, use_scheduler=False, attempts=attempts, call_metrics=calls,
        pass_name='entity_verification', chunk_id='c', max_attempts=len(responses),
        retry_base_seconds=0)
    return result, attempts, calls, seen


@pytest.mark.parametrize('indent', [None, 2, 4])
@pytest.mark.parametrize('fence', [False, True])
def test_complete_live_array_preserves_values_and_completes_once(indent, fence):
    text = json.dumps(ROWS, ensure_ascii=False, indent=indent)
    if fence:
        text = '```json\n' + text + '\n```'
    result, attempts, calls, seen = generate([' \n' + text + '\t '])
    assert result == {'verdicts': ROWS} and not attempts and len(seen) == 1
    assert calls[0]['response_normalizations'] == (
        ['complete_markdown_fence'] if fence else []) + ['complete_verdicts_array']


@pytest.mark.parametrize('text', [
    '[]',
    json.dumps(ROWS[:1]),
    json.dumps([ROWS[0], {**ROWS[1], 'item_id': 'foreign'}]),
    json.dumps([ROWS[0], {**ROWS[1], 'item_id': 'a'}]),
    json.dumps([ROWS[0], {**ROWS[1], 'extra': 'forbidden'}]),
    json.dumps([ROWS[0], {**ROWS[1], 'supported': 'maybe'}]),
    json.dumps([ROWS[0], {**ROWS[1], 'reason': None}]),
    json.dumps([ROWS[0], {'item_id': 'b', 'supported': 'yes'}]),
    json.dumps([ROWS[0], 'not a verdict']),
    json.dumps([ROWS[0], [ROWS[1]]]),
    json.dumps([{'verdicts': ROWS}]),
    json.dumps({'verdicts': ROWS, 'extra': 'forbidden'}),
    json.dumps(ROWS) + '\n说明',
    '说明\n' + json.dumps(ROWS),
    json.dumps(ROWS) + json.dumps(ROWS),
    json.dumps(ROWS) + '\n' + json.dumps(ROWS[0]),
    '[' + json.dumps(ROWS[0]) + ',\n',
    '[\n' + json.dumps(ROWS[0]) + '\n,]',
    '[{"item_id":"a","supported":"yes","supported":"no","reason":"x"},'
    + json.dumps(ROWS[1]) + ']',
    '[{"item_id":"a","\\u0069tem_id":"b","supported":"yes","reason":"x"},'
    + json.dumps(ROWS[1]) + ']',
    '[' + json.dumps(ROWS[0]) + ',{"item_id":"b","supported":"yes","reason":NaN}]',
    '[' + json.dumps(ROWS[0]) + ',{"item_id":"b","supported":"yes","reason":Infinity}]',
    '[' + json.dumps(ROWS[0]) + ',{"item_id":"b","supported":"yes","reason":1e999}]',
])
def test_bad_array_is_rejected_atomically_without_salvaging_inner_rows(text):
    normalizations = []
    rows, errors = cg._jsonl_rows(text, 'verdicts', normalizations=normalizations,
                                 verdict_schema=SCHEMA)
    assert rows == [] and errors
    assert 'complete_verdicts_array' not in normalizations
    result, attempts, _, _ = generate([text])
    assert result is None and attempts[-1]['terminal']


def test_bare_array_requires_live_normalization_and_active_schema():
    text = json.dumps(ROWS)
    for kwargs in ({}, {'verdict_schema': SCHEMA}, {'normalizations': []}):
        rows, errors = cg._jsonl_rows(text, 'verdicts', **kwargs)
        assert not rows and errors


def test_unrecognized_object_wrapper_is_not_unwrapped_and_still_fails_schema():
    text = json.dumps({'items': ROWS})
    normalizations = []
    rows, errors = cg._jsonl_rows(text, 'verdicts', normalizations=normalizations,
                                 verdict_schema=SCHEMA)
    assert rows == [{'items': ROWS}] and not errors and not normalizations
    result, attempts, _, _ = generate([text])
    assert result is None and attempts[-1]['terminal']


def test_entity_arrays_remain_rejected_with_a_supplied_verdict_schema():
    row = {'type': 'Person', 'mention': '甲', 'evidence': {'quote': '甲'}}
    text = json.dumps([row])
    rows, errors = cg._jsonl_rows(text, 'entities', normalizations=[], verdict_schema=SCHEMA)
    assert not rows and errors
    parsed, error = cg.decode_jsonl_entities(text)
    assert parsed is None and error


def test_quote_punctuation_is_data_and_existing_exact_envelope_is_unchanged():
    data = [{**ROWS[0], 'reason': '引用 ] [ {"verdicts": []} 和 \\"item_id\\"'}, ROWS[1]]
    result, attempts, calls, _ = generate([json.dumps(data, ensure_ascii=False)])
    assert result == {'verdicts': data} and not attempts
    result, attempts, calls, _ = generate([json.dumps({'verdicts': data})])
    assert result == {'verdicts': data} and not attempts
    assert calls[0]['response_normalizations'] == ['complete_verdicts_envelope']


def test_complete_array_does_not_override_provider_incomplete_status():
    response = StructuredLLMResponse(text=json.dumps(ROWS), api='chat.completions',
        response_mode='plain', status='incomplete', finish_reason='length')
    result, attempts, _, _ = generate([response])
    assert result == {'verdicts': ROWS}
    assert attempts[-1]['terminal'] and attempts[-1]['type'] == 'incomplete'


def test_jsonl_partial_recovery_can_be_followed_by_a_complete_remaining_array():
    result, attempts, calls, seen = generate([
        json.dumps(ROWS[0]), json.dumps(ROWS[1:])])
    assert result == {'verdicts': ROWS} and len(attempts) == 1 and len(seen) == 2
    assert [item['item_id'] for item in json.loads(seen[1][1]['content'])['task']['items']] == ['b']
    assert calls[-1]['response_normalizations'] == ['complete_verdicts_array']


def test_live_official01_complete_sixteen_verdict_array_replay():
    # Recorded 2026-10-14 official_01 response 000049: complete bare array.
    # Keep exact verdict text; no reasoning_content or candidate facts are consumed.
    text = '[\n{"item_id":"entity:0","supported":"yes","reason":"原文标题及正文首部均明确载有《关于开展OpenClaw（“龙虾”）等开源智能体安全风险排查工作的通知》及文号“中车集团办科信〔2026〕18号”，指向同一份可确认的公文实例，符合OfficialDocument定义。"},\n{"item_id":"entity:1","supported":"yes","reason":"原文以“一、”明确列出“全面组织开展排查，梳理本企业使用开源智能体情况，摸清安全风险底数。”，构成可辨的编号结构单元，名称忠实归纳该条内容，类型DocumentUnit成立。"},\n{"item_id":"entity:2","supported":"yes","reason":"原文以“二、”明确列出“各企业要严格执行内部办公网络与互联网有效隔离，禁止计算机等办公设备内、外网混用。”，构成可辨的编号结构单元，名称忠实归纳该条内容，类型DocumentUnit成立。"},\n{"item_id":"entity:3","supported":"yes","reason":"原文以“三、”明确列出对三级及以上网络系统或存储重要、核心数据网络系统的部署限制，名称完整保留对象、条件和禁止要求，构成可辨的编号结构单元，类型DocumentUnit成立。"},\n{"item_id":"entity:4","supported":"yes","reason":"原文以“四、”明确列出“对于其他网络系统，建议谨慎使用，并参照……等规范，做好针对性风险防范”，名称忠实归纳该条内容，构成可辨的编号结构单元，类型DocumentUnit成立。"},\n{"item_id":"entity:5","supported":"yes","reason":"原文以“五、”明确列出“加强网络和数据安全监测，一旦发现或获知开源智能体引发的本企业安全事件，第一时间处置，并向集团报告”，名称完整保留动作、触发条件和对象，类型DocumentUnit成立。"},\n{"item_id":"entity:6","supported":"yes","reason":"原文在通知缘由部分明确“决定组织开展专项排查工作”，后续第一条亦要求“全面组织开展排查”，存在由集团推动的组织排查活动；名称忠实归纳该活动，类型OperationalActivity成立。"},\n{"item_id":"entity:7","supported":"yes","reason":"原文第二条明确“各企业要严格执行内部办公网络与互联网有效隔离，禁止计算机等办公设备内、外网混用”，具备独立动作和对象，名称忠实保留义务主体与要求，类型OperationalActivity成立。"},\n{"item_id":"entity:8","supported":"yes","reason":"原文第五条明确“加强网络和数据安全监测”，并规定发现或获知安全事件后的处置与报告义务，名称与活动内容一致，具备独立动作和对象，类型OperationalActivity成立。"},\n{"item_id":"entity:9","supported":"yes","reason":"原文第五条明确“一旦发现或获知开源智能体引发的本企业安全事件，第一时间处置，并向集团报告”，其中“向集团报告”是独立报送动作，具备主体、对象和条件，类型OperationalActivity成立。"},\n{"item_id":"entity:10","supported":"yes","reason":"原文明确“各企业要认真落实通知要求，于2026年4月10日前将开源智能体安全风险排查情况（附件2）通过内网办公邮箱报集团科信部”，具备报送主体、对象、时限和途径，名称忠实概括该报送活动，类型OperationalActivity成立。"},\n{"item_id":"entity:11","supported":"yes","reason":"原文文首主送行和落款印章均出现“中国中车集团有限公司”，其作为发文和主送主体有明确原文语义角色，类型OperationalAgent成立。"},\n{"item_id":"entity:12","supported":"yes","reason":"原文主送行明确列出“中国中车股份有限公司各一级子公司”，说明中国中车股份有限公司作为下级单位归属的上级主体被提及并有接收关系依据，类型OperationalAgent成立。"},\n{"item_id":"entity:13","supported":"yes","reason":"原文主送行明确列出“中国中车集团有限公司各一级子公司，中国中车股份有限公司各一级子公司”，各一级子公司作为通知接收主体有明确原文语义角色，类型OperationalAgent成立。"},\n{"item_id":"entity:14","supported":"yes","reason":"原文主送行明确列出“总部各部门”，作为通知接收主体有明确原文语义角色，类型OperationalAgent成立。"},\n{"item_id":"entity:15","supported":"yes","reason":"原文第二条等条款明确“各企业要严格执行……”“各企业要认真落实通知要求”，各企业是义务与报送责任的承担主体，有明确原文语义角色，类型OperationalAgent成立。"}\n]'
    schema = verification_schema([{'item_id': f'entity:{index}'} for index in range(16)])
    result, attempts, calls, seen = generate([text], schema)
    assert result == {'verdicts': json.loads(text)} and not attempts and len(seen) == 1
    assert calls[0]['response_normalizations'] == ['complete_verdicts_array']
