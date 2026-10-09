"""Independent review regression: expanded judgement scope belongs to identity."""
import time

from algorithm.knowledge_extract.v2.chunk_context import ExtractionChunk
from algorithm.knowledge_extract.v2.ontology_schema import OntologySchema
from algorithm.knowledge_extract.v3_1.controller import Controller
from algorithm.knowledge_extract.v3_1.harness import Harness
from algorithm.knowledge_extract.v3_1.tests.test_semantic_contracts import (
    ScriptedLLM, SemanticDecider, _entity, _empty,
)


def test_llm_decision_may_not_cross_anchor_with_different_expanded_scope():
    ontology = '@startuml\nclass A\nclass B\nA --> B : serves\n@enduml'
    chunks = [
        ExtractionChunk('c', '甲协助乙。', document_id='doc'),
        ExtractionChunk('n', '第一节说明。', document_id='doc'),
        ExtractionChunk('m', '第二节另有否定条件。', document_id='doc'),
        ExtractionChunk('d', '第二节该事项不成立。', document_id='doc'),
    ]

    def model(stage, payload, context):
        if stage == 'resolve':
            return {'decisions': [
                {'task_id': task['task_id'],
                 'selected': 'supported' if context['chunk_id'] == 'c' else 'contradicted',
                 'context_complete': True,
                 'evidence': [{'chunk_id': 'c', 'quote': '甲协助乙。'}]}
                for task in payload['tasks']]}
        return _empty(stage)

    llm = ScriptedLLM(model)
    schema = OntologySchema.parse(ontology)
    harness = Harness(
        ontology=ontology, chunks=chunks, schema=schema, allowed={'A', 'B'},
        controller=Controller(llm, deadline=time.monotonic() + 10, timeout=2, max_calls=10),
        decider=SemanticDecider(lambda task: 'insufficient' if task.kind == 'relation_support' else 'supported'),
        mode='strict',
    )
    harness.entities([
        _entity('a', 'A', '甲', '甲协助乙。', chunk='c'),
        _entity('b', 'B', '乙', '甲协助乙。', chunk='c'),
    ], 'c', 'entities')
    harness.relation_items([
        {'from_id': 'A::甲', 'to_id': 'B::乙', 'rel_type': 'serves', 'fact': '甲协助乙',
         'evidence': [{'chunk_id': 'c', 'quote': '甲协助乙。'}]},
    ], 'c')
    previous = harness.tasks[-1]
    assert [source['chunk_id'] for source in llm.calls[-1]['input']['sources']] == ['c', 'n']
    next_task = harness.task(previous['kind'], previous['claim'], previous['original_evidence'],
                             'd', previous['criteria'])
    count = len(llm.calls)
    harness.decide([next_task])
    assert [source['chunk_id'] for source in llm.calls[-1]['input']['sources']] == ['m', 'd', 'c']
    assert len(llm.calls) == count + 1
    assert 'reused_from' not in next_task
    assert next_task['selected'] == 'contradicted'
