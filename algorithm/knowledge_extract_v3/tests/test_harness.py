import json
import threading
import time
import unittest

from knowledge_extract_v2.chunk_context import ExtractionChunk
from knowledge_extract_v3 import extract_detailed
from knowledge_extract_v3.context import plan_observations
from knowledge_extract_v3.client import ProviderError
from knowledge_extract_v2.llm_client import StructuredLLMResponse


ONTOLOGY = '''@startuml
class Person {
  age: integer
  active: boolean
}
class Company
Person --> Company : worksFor
@enduml'''


class FakeLLM:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.messages = []

    def __call__(self, messages):
        self.messages.append(messages)
        value = next(self.outputs)
        if isinstance(value, Exception):
            raise value
        return json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value


class HarnessTests(unittest.TestCase):
    def test_documents_run_in_parallel_and_results_keep_input_order(self):
        class ParallelLLM:
            def __init__(self): self.active = 0; self.maximum = 0; self.lock = threading.Lock()
            def __call__(self, messages):
                data = json.loads(messages[1]['content'])
                doc = data['source_windows'][0]['document_id']
                with self.lock:
                    self.active += 1; self.maximum = max(self.maximum, self.active)
                time.sleep(.03 if doc == 'slow' else .005)
                with self.lock: self.active -= 1
                return json.dumps({'entities':[{'type':'Person','name':doc,'properties':{}}], 'relations':[]})
        llm = ParallelLLM()
        chunks = [ExtractionChunk('a', 'slow', 'slow'), ExtractionChunk('b', 'fast', 'fast')]
        result = extract_detailed(chunks, ONTOLOGY, llm=llm, audit=False, max_workers=2)
        self.assertGreaterEqual(llm.maximum, 2)
        self.assertEqual([e['name'] for e in result['entities']], ['slow', 'fast'])

    def test_max_workers_one_is_serial(self):
        class SerialLLM(FakeLLM):
            def __init__(self, outputs): super().__init__(outputs); self.active = 0; self.maximum = 0
            def __call__(self, messages):
                self.active += 1; self.maximum = max(self.maximum, self.active)
                value = super().__call__(messages); self.active -= 1; return value
        llm = SerialLLM([{'entities': [], 'relations': []}] * 2)
        extract_detailed([ExtractionChunk('a','a','a'), ExtractionChunk('b','b','b')], ONTOLOGY, llm=llm, audit=False, max_workers=1)
        self.assertEqual(llm.maximum, 1)

    def test_global_token_budget_is_reserved_across_workers(self):
        class CountingLLM:
            config = type('Config', (), {'max_tokens': 100})()
            def __init__(self): self.calls = 0; self.lock = threading.Lock()
            def __call__(self, messages):
                with self.lock: self.calls += 1
                return json.dumps({'entities': [], 'relations': []})
        llm = CountingLLM()
        result = extract_detailed([ExtractionChunk('a','a','a'), ExtractionChunk('b','b','b')], ONTOLOGY, llm=llm, audit=False, max_workers=2, max_total_tokens=150)
        self.assertLessEqual(result['stats']['reserved_tokens'], 150)
        self.assertLessEqual(llm.calls, 1)
    def test_empty_document_is_valid_not_forced_facts(self):
        llm = FakeLLM([{'entities': [], 'relations': []}])
        result = extract_detailed([ExtractionChunk('c', '无相关事实', 'doc')], ONTOLOGY, llm=llm, audit=False)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['entities'], [])
        self.assertEqual(result['stats']['calls'], 1)

    def test_repair_keeps_real_assistant_turn(self):
        llm = FakeLLM(['{bad json', {'entities': [], 'relations': []}])
        result = extract_detailed([ExtractionChunk('c', '原文', 'doc')], ONTOLOGY, llm=llm, audit=False)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(llm.messages[1][-2], {'role': 'assistant', 'content': '{bad json'})
        self.assertIn('INVALID_JSON', llm.messages[1][-1]['content'])

    def test_budget_exhaustion_is_not_success(self):
        llm = FakeLLM(['invalid'])
        result = extract_detailed([ExtractionChunk('c', '原文', 'doc')], ONTOLOGY, llm=llm, audit=False, max_calls=1)
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(len(llm.messages), 1)

    def test_missing_document_scope_does_not_share_registry(self):
        llm = FakeLLM([{'entities': [{'type':'Person', 'name':'甲', 'properties':{}}], 'relations':[]}, {'entities':[], 'relations':[]}])
        result = extract_detailed([ExtractionChunk('a','甲'), ExtractionChunk('b','乙')], ONTOLOGY, llm=llm, audit=False)
        self.assertEqual(json.loads(llm.messages[1][1]['content'])['known_entities'], [])
        self.assertTrue(any(x['code'] == 'DOCUMENT_SCOPE_MISSING' for x in result['diagnostics']))

    def test_cross_window_known_entities_have_names(self):
        llm = FakeLLM([{'entities': [{'type':'Person', 'name':'甲', 'properties':{}}], 'relations':[]}, {'entities':[], 'relations':[]}])
        extract_detailed([ExtractionChunk('a','甲','doc'), ExtractionChunk('b','他','doc')], ONTOLOGY, llm=llm, audit=False)
        known = json.loads(llm.messages[1][1]['content'])['known_entities']
        self.assertEqual(known, [{'type': 'Person', 'name': '甲', 'properties': {}}])

    def test_source_coverage_and_metadata(self):
        text = '表头\n' + '一行原文0123456789\n' * 1000
        chunk = ExtractionChunk('table', text, 'doc', metadata={'sheet_name':'Sheet1', 'Authorization':'secret'})
        observations, _ = plan_observations([chunk], window_chars=500)
        covered = set()
        for observation in observations:
            for window in observation['source_windows']:
                self.assertEqual(text[window['start_offset']:window['end_offset']], window['text'])
                self.assertNotIn('Authorization', window['metadata'])
                covered.update(range(window['start_offset'], window['end_offset']))
        self.assertEqual(len(covered), len(text))

    def test_empty_proposal_can_be_recovered_by_audit(self):
        llm=FakeLLM([{'entities':[],'relations':[]}, {'entities':[{'type':'Person','name':'甲'}],'relations':[]}])
        result=extract_detailed([ExtractionChunk('c','甲','doc')],ONTOLOGY,llm=llm)
        self.assertEqual(len(result['entities']),1)
        self.assertEqual(result['stats']['audit_calls'],1)

    def test_retry_repair_budget_and_diagnostics(self):
        llm=FakeLLM([ProviderError('HTTP_503',retryable=True), 'invalid', 'invalid', 'invalid'])
        result=extract_detailed([ExtractionChunk('c','甲','doc')],ONTOLOGY,llm=llm,audit=False)
        self.assertLessEqual(result['stats']['repair_calls'],2)
        self.assertLessEqual(result['stats']['calls'],5)
        self.assertTrue(any(d['code']=='HTTP_503' for d in result['diagnostics']))

    def test_nonretryable_error_is_failed_not_budget_incomplete(self):
        llm=FakeLLM([ProviderError('HTTP_400')])
        result=extract_detailed([ExtractionChunk('c','甲','doc')],ONTOLOGY,llm=llm,audit=False,max_calls=1)
        self.assertEqual(result['status'],'failed')

    def test_input_budget_is_incomplete_before_request(self):
        llm=FakeLLM([])
        result=extract_detailed([ExtractionChunk('c','甲','doc')],ONTOLOGY,llm=llm,audit=False,max_input_bytes=1)
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(llm.messages,[])

    def test_nested_auth_metadata_is_excluded(self):
        observations,_=plan_observations([ExtractionChunk('c','table','doc',metadata={'headers':{'Authorization':'Bearer SECRET'},'columns':['name','value']})])
        self.assertNotIn('SECRET',json.dumps(observations))
        self.assertIn('columns',observations[0]['source_windows'][0]['metadata'])

    def test_truncation_uses_two_bounded_subobservations(self):
        class Truncated(FakeLLM):
            def __call__(self,messages):
                self.messages.append(messages)
                if len(self.messages)==1:
                    return StructuredLLMResponse(text='{',api='test',response_mode='json_object',status='incomplete',finish_reason='length')
                return '{"entities":[],"relations":[]}'
        llm=Truncated([])
        result=extract_detailed([ExtractionChunk('c','a'*1000,'doc')],ONTOLOGY,llm=llm,audit=False)
        self.assertEqual(result['status'],'success')
        self.assertEqual(result['stats']['repair_calls'],2)
        self.assertEqual(result['stats']['calls'],3)

    def test_json_decimal_precision_is_preserved(self):
        llm=FakeLLM(['{"entities":[{"type":"Item","name":"A","properties":{"measure":0.123456789123456789}}],"relations":[]}'])
        result=extract_detailed([ExtractionChunk('c','A','doc')],'class Item {\n measure: decimal\n}',llm=llm,audit=False)
        self.assertEqual(result['entities'][0]['properties']['measure'],'0.123456789123456789')

    def test_semantic_review_receives_full_properties_and_corresponding_text(self):
        description='详细描述'*180
        ontology='class Person {\n description: string\n}\nclass Company\nPerson --> Company : worksFor'
        llm=FakeLLM([
            {'entities':[{'type':'Person','name':name,'properties':{'description':description},'confidence':.9} for name in ['张三','张工']]+[{'type':'Company','name':'甲公司','confidence':.9}],
             'relations':[{'from_type':'Person','from_name':'张工','rel_type':'worksFor','to_type':'Company','to_name':'甲公司','confidence':.9}]},
            {'entities':[],'relations':[],'merges':[{'canonical':{'type':'Person','name':'张三'},'members':[{'type':'Person','name':'张工'}],'confidence':.95,'rationale':'原文明确说明张工即张三，属性一致'}]},
        ])
        source='张三在甲公司工作，同事称他为张工。'
        result=extract_detailed([ExtractionChunk('c',source,'d')],ontology,llm=llm,audit=False,review_min_entities=2)
        review=json.loads(llm.messages[1][1]['content'])
        self.assertEqual(review['task'],'review')
        candidates=[e for e in review['known_entities'] if e['type']=='Person']
        self.assertTrue(all(e['properties']['description']==description for e in candidates))
        self.assertTrue(all(e['source_texts'][0]['text']==source for e in candidates))
        self.assertEqual(result['stats']['review_calls'],1)
        self.assertEqual(result['stats']['merged_entities'],1)
        self.assertEqual(result['relations'][0]['from_name'],'张三')
        self.assertTrue(all('id' not in e for e in result['entities']+result['relations']))

    def test_review_text_follows_late_source_ref(self):
        from knowledge_extract_v3.harness import _review_context
        planned,_=plan_observations([ExtractionChunk('early','无关的前言','d'),ExtractionChunk('late','小张与张三指同一位工程师。','d')])
        node={'type':'Person','name':'小张','properties':{'age':30},'source_refs':[{'chunk_id':'late','start_offset':0}]}
        context=_review_context([node],planned,limit=10)
        self.assertEqual(context[0]['source_texts'][0]['chunk_id'],'late')
        self.assertIn('同一位工程师',context[0]['source_texts'][0]['text'])


if __name__ == '__main__':
    unittest.main()
