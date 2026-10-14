"""Document aliases and OCR form evidence retain independent source authority."""
from copy import deepcopy
import json

import pytest

from ..chunk_context import ChunkContextManager, ExtractionChunk
from ..official_identity import reconcile_official_identities
from ..ontology_compiler import compile_plan
from ..property_grounding import admit_entity_names, ground_properties, locate_in_sources, locate_property_quote
from .stub_llm import StubLLM

ONTOLOGY = '''@startuml
class OfficialDocument {
 + 文件标题: String
 + 来文字号: String
 + 收文号: String
 + 来文单位: String
 + 收文日期: String
 + 领导批示: String
}
note right of OfficialDocument
类定义：一份具体公文，文件抬头、标题和对应收文处理单指向同一文书。
end note
@enduml'''
TITLE = '关于印发《研发管理办法》的通知'
PLAN = compile_plan(ONTOLOGY)


def _row(source, name, properties, quotes=None):
    row = {'type': 'OfficialDocument', 'name': name, 'properties': deepcopy(properties),
           'evidence': {'quote': source.text, 'property_quotes': quotes or {
               prop: [source.text] for prop in properties}}}
    llm = StubLLM(verification_handler=lambda item, chunk: 'yes')
    admit_entity_names([row], [source], llm, attempts=[], warnings=[], plan=PLAN)
    ground_properties([row], [source], llm, attempts=[], warnings=[], plan=PLAN)
    assert row['properties'] == properties
    return row


def _reconcile(sources, rows, *, llm=None, title=TITLE):
    llm = llm or StubLLM(identity_handler=lambda payload: {
        item['item_id']: {'mention': title} for item in payload['task']['items']},
        verification_handler=lambda item, chunk: 'yes')
    result = reconcile_official_identities(rows, ChunkContextManager(sources), PLAN, llm,
        route=None, assembly_planner=None, attempts=[], warnings=[], call_metrics=[],
        use_scheduler=False, max_attempts=1, retry_base_seconds=0, max_workers=1)
    return [row for _, items in result for row in items], llm


def test_repeated_title_and_labelled_company_date_are_located_before_semantic_review():
    text = (f'# 集团办公室\n# {TITLE}\n\n文件标题：*\n{TITLE}\n\n'
            '来文单位：\n集团\n\n收文日期：\n2030-05-25\n\n'
            '办公室主任 拟办意见：\n张某2030-05-2516:11\n')
    source = ExtractionChunk('a', text)
    row = {'type': 'OfficialDocument', 'name': TITLE,
           'properties': {'文件标题': TITLE, '来文单位': '集团', '收文日期': '2030-05-25'},
           'evidence': {'quote': TITLE, 'property_quotes': {
               '文件标题': [TITLE], '来文单位': ['集团'], '收文日期': ['2030-05-25']}}}
    seen = []
    def verify(item, context):
        seen.append(item)
        assert '文件标题' in context['text'] and '收文日期' in context['text']
        return 'yes'
    ground_properties([row], [source], StubLLM(verification_handler=verify),
                      attempts=[], warnings=[], plan=PLAN)
    assert set(row['properties']) == {'文件标题', '来文单位', '收文日期'}
    assert len(seen) == 3
    assert len(row['property_evidence']['文件标题']['sources']) == 2
    assert row['property_evidence']['来文单位']['sources'][0]['start_char'] == text.index('集团\n\n收文日期')
    assert len(row['property_evidence']['收文日期']['sources']) == 1
    for receipt in row['property_evidence'].values():
        for span in receipt['sources']:
            assert text[span['start_char']:span['end_char']] == span['quote']


def test_labelled_occurrence_is_still_not_a_semantic_receipt():
    source = ExtractionChunk('a', f'{TITLE}\n来文单位：\n集团甲\n其他文件来文单位：\n集团甲\n')
    row = {'type': 'OfficialDocument', 'name': TITLE, 'properties': {'来文单位': '集团甲'},
           'evidence': {'quote': TITLE, 'property_quotes': {'来文单位': ['集团甲']}}}
    ground_properties([row], [source], StubLLM(verification_handler=lambda item, chunk: 'no'),
                      attempts=[], warnings=[], plan=PLAN)
    assert row['properties'] == {} and row['property_evidence'] == {}


def test_repeated_quotes_do_not_import_an_unknown_or_other_document():
    own = ExtractionChunk('a', f'{TITLE}\n收文日期：\n2030-05-25\n', document_id='one')
    other = ExtractionChunk('b', '其他通知\n收文日期：\n2030-05-25\n', document_id='two')
    unknown = ExtractionChunk('c', '另份通知\n收文日期：\n2030-05-25\n')
    row = {'name': TITLE, 'evidence': locate_in_sources(TITLE, [own])}
    spans = locate_property_quote('2030-05-25', [own, other, unknown],
                                  subject=row, property_labels=['收文日期'])
    assert [span['source_block_ids'] for span in spans] == [['a']]


def test_ocr_interrupted_label_yields_original_fragments_not_a_fabricated_quote():
    quote = '请技术中心阅办，明确职责，按期督促，确保按期高质量完成工作。'
    text = f'{TITLE}\n请技术中⼼阅办， 明确职责，按期督\n\n领导批⽰：\n\n促，确保按期⾼质量完成⼯作。'
    source = ExtractionChunk('a', text)
    row = {'type': 'OfficialDocument', 'name': TITLE, 'properties': {'领导批示': quote},
           'evidence': {'quote': TITLE, 'property_quotes': {'领导批示': [quote]}}}
    seen = []
    def verify(item, context):
        seen.append(item)
        assert '领导批⽰：' in context['text']
        assert len(item['evidence_quotes']) == 2
        return 'yes'
    ground_properties([row], [source], StubLLM(verification_handler=verify),
                      attempts=[], warnings=[], plan=PLAN)
    assert row['properties']['领导批示'] == quote and len(seen) == 1
    spans = row['property_evidence']['领导批示']['sources']
    assert len(spans) == 2
    for span in spans:
        assert text[span['start_char']:span['end_char']] == span['quote']
        assert '领导批' not in span['quote']


@pytest.mark.parametrize('text,quote', [
    ('请确保按 计划实施。', '请确保按计划实施。'),
    ('请各事业 部落实。', '请各事业部落实。'),
])
def test_ocr_cjk_layout_whitespace_preserves_source_offsets(text, quote):
    source = ExtractionChunk('a', f'{TITLE}\n领导批示：\n{text}')
    row = {'name': TITLE, 'evidence': locate_in_sources(TITLE, [source])}
    spans = locate_property_quote(quote, [source], subject=row, property_labels=['领导批示'])
    assert len(spans) == 1 and spans[0]['quote'] == text
    assert source.text[spans[0]['start_char']:spans[0]['end_char']] == text


@pytest.mark.parametrize('text,quote', [
    ('请按期督\n附件说明：\n促，确保完成。', '请按期督促，确保完成。'),
    ('请按期督\n领导批示：暂缓\n促，确保完成。', '请按期督促，确保完成。'),
    ('请不执行方案。', '请执行方案。'),
    ('编号AB CD', '编号ABCD'),
    ('请按期督\n领导批示：\n促，确保完成。', '请按期督促，确保高质量完成。'),
])
def test_layout_recovery_never_discards_content_or_changes_ocr_characters(text, quote):
    source = ExtractionChunk('a', f'{TITLE}\n{text}')
    row = {'name': TITLE, 'evidence': locate_in_sources(TITLE, [source])}
    assert locate_property_quote(quote, [source], subject=row, property_labels=['领导批示']) == []


def test_header_title_and_receipt_aliases_without_doc_id_use_proven_number_bridge():
    body = ExtractionChunk('body', f'集团文件\n集办［2030］42号\n{TITLE}\n来文单位：集团')
    form = ExtractionChunk('form', f'收文号：集团收〔2030〕7号\n文件标题：{TITLE}\n来文字号：集办(2030)42号\n收文日期：2030-06-01')
    header = _row(body, '集团文件', {'文件标题': TITLE, '来文字号': '集办［2030］42号', '来文单位': '集团'})
    title = _row(form, TITLE, {'来文字号': '集办(2030)42号', '收文日期': '2030-06-01'})
    receipt = _row(form, '集团收〔2030〕7号', {})
    rows, llm = _reconcile([body, form], [('body', [header]), ('form', [title, receipt])])
    assert {row['name'] for row in rows} == {TITLE}
    assert len({row['properties']['来文字号'] for row in rows if '来文字号' in row['properties']}) == 1
    assert rows[0]['properties']['来文单位'] == '集团'
    for row in rows:
        assert row['entity_evidence']['name'] == TITLE
        for prop, value in row['properties'].items():
            assert row['property_evidence'][prop]['value'] == value
    identity_calls = [json.loads(prompt) for task, prompt in llm.calls
                      if task == 'evidence_verification' and 'original_name' in prompt]
    assert identity_calls and all('original_name' in item for item in identity_calls[0]['task']['items'])


def test_nfkc_title_variants_merge_only_with_supported_source_scope():
    title = '关于开展三年行动方案的通知'
    variant = title.replace('行动方案', '⾏动⽅案')
    sources = [ExtractionChunk('a', f'{variant}\n集办〔2030〕42号'),
               ExtractionChunk('b', f'{title}\n集办［2030］42号')]
    rows = [(s.chunk_id, [_row(s, name, {'文件标题': name, '来文字号': number})])
            for s, name, number in zip(sources, [variant, title], ['集办〔2030〕42号', '集办［2030］42号'])]
    output, _ = _reconcile(sources, rows, title=title)
    assert {row['name'] for row in output} == {title}
    assert len({row['properties']['文件标题'] for row in output}) == 1
    assert len({row['properties']['来文字号'] for row in output}) == 1


@pytest.mark.parametrize('known_ids', [False, True])
def test_same_short_title_without_proven_shared_identifier_does_not_cross_documents(known_ids):
    sources = [ExtractionChunk('a', '行动通知', document_id='one' if known_ids else None),
               ExtractionChunk('b', '⾏动通知', document_id='two' if known_ids else None)]
    rows = [(s.chunk_id, [_row(s, s.text, {})]) for s in sources]
    output, llm = _reconcile(sources, rows, title='行动通知')
    assert [row['name'] for row in output] == ['行动通知', '⾏动通知']
    assert not llm.calls


def test_conflicting_proven_numbers_block_alias_even_inside_same_chunk():
    source = ExtractionChunk('a', '甲通知\n集办〔2030〕42号\n乙通知\n集办〔2030〕43号')
    rows = [_row(source, name, {'来文字号': number}) for name, number in
            [('甲通知', '集办〔2030〕42号'), ('乙通知', '集办〔2030〕43号')]]
    output, llm = _reconcile([source], [('a', rows)], title='甲通知')
    assert [row['name'] for row in output] == ['甲通知', '乙通知']
    assert not llm.calls


@pytest.mark.parametrize('refuse', ['identity', 'property'])
def test_alias_needs_independent_identity_and_field_ownership_proofs(refuse):
    source = ExtractionChunk('a', f'集团文件\n{TITLE}\n来文单位：集团')
    header = _row(source, '集团文件', {'文件标题': TITLE, '来文单位': '集团'})
    title = _row(source, TITLE, {})
    def verify(item, chunk):
        if refuse == 'identity' and item.get('original_name'):
            return 'uncertain'
        if refuse == 'property' and item.get('property') == '来文单位' and item['name'] == TITLE:
            return 'no'
        return 'yes'
    llm = StubLLM(identity_handler=lambda payload: {item['item_id']: {'mention': TITLE}
        for item in payload['task']['items']}, verification_handler=verify)
    output, _ = _reconcile([source], [('a', [header, title])], llm=llm)
    assert [row['name'] for row in output] == ['集团文件', TITLE]
    assert output[0]['properties'] == header['properties']


def test_alias_does_not_overwrite_genuine_property_conflicts():
    source = ExtractionChunk('a', f'集团文件\n{TITLE}\n收文日期：2030-01-01\n另记录收文日期：2030-01-02')
    header = _row(source, '集团文件', {'文件标题': TITLE, '收文日期': '2030-01-01'})
    title = _row(source, TITLE, {'收文日期': '2030-01-02'})
    output, _ = _reconcile([source], [('a', [header, title])])
    assert {row['name'] for row in output} == {TITLE}
    assert [row['properties']['收文日期'] for row in output] == ['2030-01-01', '2030-01-02']


def test_prior_receipt_coordinates_resolve_repeated_quotes_but_never_replace_review():
    source = ExtractionChunk('a', f'{TITLE}\n收文号：7\n另一份公文收文号：7')
    start = source.text.index('7')
    span = {'span_id': f'a:{start}:{start + 1}', 'source_block_ids': ['a'],
            'quote': '7', 'start_char': start, 'end_char': start + 1}
    assert locate_property_quote('7', [source]) == []
    assert locate_property_quote('7', [source], preferred_spans=[span])[0]['start_char'] == start
    for forged in ({**span, 'start_char': start - 1}, {**span, 'quote': '8'}):
        assert locate_property_quote('7', [source], preferred_spans=[forged]) == []
    row = {'type': 'OfficialDocument', 'name': TITLE, 'properties': {'收文号': '7'},
           'evidence': {'quote': TITLE, 'property_quotes': {'收文号': ['7']}}}
    seen = []
    def reject(item, context):
        seen.append(item)
        return 'no'
    ground_properties([row], [source], StubLLM(verification_handler=reject),
        attempts=[], warnings=[], plan=PLAN,
        source_receipts={('OfficialDocument', TITLE): {'收文号': {'value': '7', 'support': 'llm', 'sources': [span]}}})
    assert seen and row['properties'] == {} and row['property_evidence'] == {}


def test_split_pipeline_consolidates_proved_header_title_and_receipt_aliases():
    from ..split_extractor import extract_split
    text = f'集团文件\n{TITLE}\n来文字号：集办〔2030〕42号\n收文号：集团收〔2030〕7号'
    names = ['集团文件', TITLE, '集团收〔2030〕7号']
    llm = StubLLM(entity_handler=lambda prompt, chunk: [
        {'type': 'OfficialDocument', 'mention': name, 'properties': {}, 'evidence': {'quote': text}}
        for name in names], identity_handler=lambda payload: {
            item['item_id']: {'mention': TITLE} for item in payload['task']['items']},
        verification_handler=lambda item, context: 'yes')
    result = extract_split(ONTOLOGY, [ExtractionChunk('a', text)], llm=llm, skill_refs=[],
                           max_attempts=1, max_repair_rounds=0)
    assert result['status'] == 'succeeded'
    assert [row['name'] for row in result['raw']['entities']] == [TITLE]


def test_document_admission_is_separate_from_missing_management_fields():
    source = ExtractionChunk('a', f'{TITLE}\n集办〔2030〕42号')
    row = {'type': 'OfficialDocument', 'name': TITLE,
           'properties': {'收文号': '收〔2030〕7号', '收文日期': '2030-06-01'},
           'evidence': {'quote': TITLE}}
    stub = StubLLM(verification_handler=lambda item, context: 'yes' if 'property' not in item else 'no')
    def llm(messages):
        user = json.loads(messages[1]['content'])
        if 'property' not in user['task']['items'][0]:
            assert '实体准入与属性完整性分别判断' in messages[0]['content']
            assert '管理字段尚未获证不构成拒绝该公文的理由' in messages[0]['content']
            assert user['task']['items'][0]['candidate_properties'] == row['properties']
        return stub(messages)
    accepted = admit_entity_names([row], [source], llm, attempts=[], warnings=[], plan=PLAN)
    assert accepted == [row] and row['entity_evidence']['name'] == TITLE
    ground_properties(accepted, [source], llm, attempts=[], warnings=[], plan=PLAN)
    assert row['properties'] == {} and row['property_evidence'] == {}


def test_admission_still_exposes_rule_conditions_and_rejects_mixed_subjects():
    source = ExtractionChunk('a', '泵甲温度过高时停机。泵乙温度正常。')
    plan = compile_plan('@startuml\nclass Rule {\n + premise: String\n + result: String\n}\n@enduml')
    row = {'type': 'Rule', 'name': '泵乙高温停机规则',
           'properties': {'premise': '泵乙温度过高', 'result': '泵乙停机'},
           'evidence': {'quote': source.text}}
    seen = []
    def llm(messages):
        item = json.loads(messages[1]['content'])['task']['items'][0]
        seen.append(item)
        assert '不能以属性另验为由忽略规则前提或混合主体' in messages[0]['content']
        assert item['candidate_properties'] == row['properties']
        return json.dumps({'item_id': item['item_id'], 'supported': 'no', 'reason': '原文高温停机属于泵甲'})
    assert admit_entity_names([row], [source], llm, attempts=[], warnings=[], plan=plan) == []
    assert seen and not row.get('entity_evidence')
