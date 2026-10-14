"""Returned-output repair stays available after bounded transport outages."""
import json
import pytest

from ..candidate_generation import generate_structured
from ..ontology_compiler import compile_plan
from ..output_contracts import property_entity_schema, validate_property_proposals
from ..task_runtime import document_tasks

PLAN = compile_plan('@startuml\nclass Person {\n + birthplace: String\n}\n@enduml')
ROW = {'type': 'Person', 'name': '甲', 'properties': {'birthplace': '北京'},
       'evidence': {'property_quotes': {'birthplace': ['甲出生于北京']}}}
BAD = {**ROW, 'evidence': {}}


def run(responses, *, budget=None, monkeypatch=None):
    if budget is not None:
        monkeypatch.setenv('KG_EXTRACT_V31_MAX_CALLS', str(budget))
    iterator = iter(responses)
    seen, attempts = [], []
    def llm(messages):
        seen.append(messages)
        response = next(iterator)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response)
    schema = property_entity_schema([ROW], PLAN)
    @document_tasks
    def request():
        return generate_structured(llm, [{'role': 'system', 'content': 'contract'},
            {'role': 'user', 'content': json.dumps({'task': {'operation': 'property'},
                'output_contract': {'success_schema': schema['properties']['entities']['items']}})}],
            schema, use_scheduler=False, attempts=attempts, pass_name='property',
            chunk_id='c', max_attempts=3, retry_base_seconds=0,
            validator=validate_property_proposals)
    return request(), attempts, seen


def test_transport_outages_do_not_exhaust_quote_repair():
    result, attempts, seen = run([RuntimeError(), RuntimeError(), BAD, BAD, ROW])
    assert result == {'entities': [ROW]} and len(seen) == 5
    assert not any(a['terminal'] for a in attempts)
    assert [a['transport_failures'] for a in attempts] == [1, 2, 2, 2]
    assert [a['output_failures'] for a in attempts] == [0, 0, 1, 2]
    assert 'support quotes required' in json.loads(seen[-1][1]['content'])['retry_feedback']


@pytest.mark.parametrize('responses', [
    [RuntimeError(), RuntimeError(), RuntimeError()],
    [BAD, BAD, BAD],
    [RuntimeError(), RuntimeError(), BAD, BAD, BAD],
])
def test_each_error_category_and_the_task_ceiling_remain_bounded(responses):
    result, attempts, seen = run(responses)
    assert result is None and len(seen) == len(responses)
    assert attempts[-1]['terminal']
    assert max(a['transport_failures'] for a in attempts) <= 3
    assert max(a['output_failures'] for a in attempts) <= 3


def test_mixed_retries_cannot_exceed_document_call_budget(monkeypatch):
    result, attempts, seen = run([RuntimeError(), RuntimeError(), BAD, BAD, ROW],
                                budget=4, monkeypatch=monkeypatch)
    assert result is None and len(seen) == 4
    assert attempts[-1]['type'] == 'call_budget' and attempts[-1]['terminal']
