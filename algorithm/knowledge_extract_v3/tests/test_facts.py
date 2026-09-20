import unittest
from knowledge_extract_v3.facts import Registry, coalesce_results, display_property_names, _value
from knowledge_extract_v3.schema import compile_ontology
ONTOLOGY="""class Person { age: integer }
class Company
Person --> Company : employs
"""
class FactsTests(unittest.TestCase):
 def setUp(self): self.c=compile_ontology(ONTOLOGY); self.w=[{"chunk_id":"c1","document_id":"d"}]
 def add(self,r,t,n,p=None): return r.apply({"entities":[{"type":t,"name":n,"properties":p or {},"source_refs":[{"chunk_id":"c1"}]}],"relations":[]},self.w)
 def test_name_protocol_and_sparse_values(self):
  r=Registry(self.c,"d"); self.assertFalse(self.add(r,"Person","Ada",{"age":0})); self.assertEqual(r.known_entities()[0]["name"],"Ada"); self.assertEqual(r.entities[0]["properties"]["age"],0)
 def test_relation_uses_five_name_keys(self):
  r=Registry(self.c,"d"); self.add(r,"Person","Ada"); self.add(r,"Company","Acme"); e=r.apply({"entities":[],"relations":[{"from_type":"Person","from_name":"Ada","rel_type":"employs","to_type":"Company","to_name":"Acme","source_refs":[{"chunk_id":"c1"}]}]},self.w); self.assertFalse(e); self.assertEqual(set(r.public_result()["relations"][0]),{"from_type","from_name","rel_type","to_type","to_name"})
 def test_ids_and_unknown_endpoints_rejected(self):
  r=Registry(self.c,"d"); self.assertTrue(r.apply({"entities":[{"id":"local:x","type":"Person","name":"A"}]},self.w)); self.assertTrue(r.apply({"entities":[],"relations":[{"from_type":"Person","from_name":"A","rel_type":"employs","to_type":"Company","to_name":"B","source_refs":[{"chunk_id":"c1"}]}]},self.w))
 def test_merge_requires_confidence_and_updates_relations(self):
  r=Registry(self.c,"d"); self.add(r,"Person","Ada"); self.add(r,"Person","A Ada"); self.assertTrue(r.apply_merges([{"confidence":.7,"canonical":{"type":"Person","name":"Ada"},"members":[{"type":"Person","name":"A Ada"}]}])); self.assertEqual(len(r.entities),2); self.assertFalse(r.apply_merges([{"confidence":.9,"canonical":{"type":"Person","name":"Ada"},"members":[{"type":"Person","name":"A Ada"}]}])); self.assertEqual(len(r.entities),1)
 def test_coalesce_by_type_name(self):
  a={"entities":[{"type":"Person","name":"A","properties":{"x":1},"source_refs":[{"chunk_id":"a"}]}],"relations":[]}; b={"entities":[{"type":"Person","name":"A","properties":{"y":2}}],"relations":[]}; out=coalesce_results([a,b]); self.assertEqual(out["entities"][0]["properties"],{"x":1,"y":2})
 def test_exact_numeric_conversion(self): self.assertEqual(_value("9007199254740993","integer"),9007199254740993); self.assertEqual(_value("0.123456789123456789","decimal"),"0.123456789123456789")

 def test_sparse_arrays_objects_and_false(self):
  compiled=compile_ontology('class Item {\n count: integer\n active: boolean\n labels: string[]\n info: object\n title: string\n}')
  r=Registry(compiled,'d')
  r.apply({'entities':[{'type':'Item','name':'x','properties':{'count':0,'active':False,'labels':['',None],'info':{'empty':''},'title':'未知'}}]},self.w)
  self.assertEqual(r.entities[0]['properties'],{'count':0,'active':False})

 def test_invalid_types_and_properties_are_diagnosed(self):
  r=Registry(self.c,'d')
  errors=r.apply({'entities':[{'type':'Person','name':'A','properties':{'age':'12 kg','invented':4}}]},self.w)
  self.assertEqual({x['code'] for x in errors},{'INVALID_PROPERTY_VALUE','UNKNOWN_PROPERTY'})
  self.assertEqual(r.entities[0]['properties'],{})

 def test_same_batch_duplicate_keeps_first_scalar_and_records_conflict(self):
  r=Registry(self.c,'d')
  errors=r.apply({'entities':[{'type':'Person','name':'A','properties':{'age':2}}, {'type':'Person','name':'A','properties':{'age':3}}]},self.w)
  self.assertEqual(len(r.entities),1)
  self.assertEqual(r.entities[0]['properties']['age'],2)
  self.assertEqual(r.entities[0]['alternatives'][0]['value'],3)
  self.assertIn('PROPERTY_CONFLICT',{x['code'] for x in errors})

 def test_different_types_same_name_are_distinct(self):
  r=Registry(self.c,'d'); self.add(r,'Person','same'); self.add(r,'Company','same')
  self.assertEqual(len(r.entities),2)

 def test_merge_rejects_conflicting_alternatives_atomically(self):
  r=Registry(self.c,'d')
  for name,alternative in [('A',3),('B',4)]:
   self.add(r,'Person',name,{'age':2})
   self.add(r,'Person',name,{'age':alternative})
  before=r.facts()
  group={'confidence':.9,'canonical':{'type':'Person','name':'A'},'members':[{'type':'Person','name':'B'}]}
  self.assertEqual(r.apply_merges([group])[0]['code'],'MERGE_PROPERTY_CONFLICT')
  self.assertEqual(r.facts(),before)

 def test_source_document_spoof_is_rejected(self):
  r=Registry(self.c,'d')
  errors=r.apply({'entities':[{'type':'Person','name':'A','source_refs':[{'chunk_id':'c1','document_id':'other'}]}]},self.w)
  self.assertEqual(r.entities,[])
  self.assertEqual(errors[0]['code'],'UNKNOWN_SOURCE_REF')

 def test_locator_does_not_change_acceptance_or_score(self):
  outcomes=[]
  for quote in ['Ada','not in document']:
   r=Registry(self.c,'d')
   r.apply({'entities':[{'type':'Person','name':'Ada','confidence':.95,'source_refs':[{'chunk_id':'c1','quote':quote}]}]},[dict(self.w[0],text='Ada')])
   outcomes.append((len(r.entities),r.entities[0]['confidence']))
  self.assertEqual(outcomes,[(1,.95),(1,.95)])

 def test_support_caps_and_unsupported_rejection(self):
  r=Registry(self.c,'d')
  r.apply({'entities':[{'type':'Person','name':s,'support':s,'confidence':1} for s in ['direct','contextual','uncertain','unsupported']]},self.w)
  self.assertEqual([e['confidence'] for e in r.entities],[1,.79,.49])

 def test_no_identity_property_automerge_and_no_delete(self):
  r=Registry(self.c,'d'); self.add(r,'Person','A',{'age':2});self.add(r,'Person','B',{'age':2})
  self.assertEqual(len(r.entities),2)
  self.assertEqual(r.apply({'delete':['A']},self.w)[0]['code'],'UNAUTHORIZED_EDIT')

 def test_merge_is_atomic_on_conflict_or_unknown_member(self):
  r=Registry(self.c,'d');self.add(r,'Person','A',{'age':0});self.add(r,'Person','B',{'age':1})
  group={'confidence':.9,'canonical':{'type':'Person','name':'A'},'members':[{'type':'Person','name':'B'}]}
  self.assertEqual(r.apply_merges([group])[0]['code'],'MERGE_PROPERTY_CONFLICT')
  group['members'].append({'type':'Person','name':'missing'})
  self.assertEqual(r.apply_merges([group])[0]['code'],'INVALID_MERGE')
  self.assertEqual(len(r.entities),2)

 def test_merge_rewrites_and_deduplicates_relations(self):
  r=Registry(self.c,'d');self.add(r,'Person','Ada');self.add(r,'Person','A Ada');self.add(r,'Company','Acme')
  r.apply({'relations':[{'from_type':'Person','from_name':n,'rel_type':'employs','to_type':'Company','to_name':'Acme','confidence':.9} for n in ['Ada','A Ada']]},self.w)
  errors=r.apply_merges([{'confidence':.9,'canonical':{'type':'Person','name':'Ada'},'members':[{'type':'Person','name':'A Ada'}]}])
  self.assertEqual(errors,[]);self.assertEqual(len(r.relations),1)
  self.assertEqual(r.relations[0]['from_name'],'Ada')
  self.assertTrue(all('id' not in e for e in r.entities+r.relations))

 def test_merge_cannot_create_self_loop(self):
  r=Registry(compile_ontology('class Item\nItem --> Item : follows'),'d')
  r.apply({'entities':[{'type':'Item','name':n} for n in ['A','B']], 'relations':[{'from_type':'Item','from_name':'A','rel_type':'follows','to_type':'Item','to_name':'B'}]},self.w)
  errors=r.apply_merges([{'confidence':.9,'canonical':{'type':'Item','name':'A'},'members':[{'type':'Item','name':'B'}]}])
  self.assertEqual(errors[0]['code'],'MERGE_NEW_SELF_LOOP')
  self.assertEqual(len(r.entities),2)

 def test_relation_id_fields_and_wrong_direction_rejected(self):
  r=Registry(self.c,'d');self.add(r,'Person','Ada');self.add(r,'Company','Acme')
  relation={'from_type':'Person','from_name':'Ada','rel_type':'employs','to_type':'Company','to_name':'Acme','id':'invented'}
  self.assertEqual(r.apply({'relations':[relation]},self.w)[0]['code'],'UNSUPPORTED_ID')
  reverse={'from_type':'Company','from_name':'Acme','rel_type':'employs','to_type':'Person','to_name':'Ada'}
  self.assertEqual(r.apply({'relations':[reverse]},self.w)[0]['code'],'INVALID_RELATION_DIRECTION')

 def test_public_properties_use_ontology_display_names(self):
  compiled=compile_ontology('class "术语" <<Term>> {\n- 术语标准名称 (TermName)\n}')
  r=Registry(compiled,'d')
  r.apply({'entities':[{'type':'Term','name':'容量','properties':{'TermName':'容量'},'confidence':.9}]},self.w)
  public=display_property_names(r.facts(),compiled.schema)
  self.assertEqual(public['entities'][0]['properties'],{'术语标准名称':'容量'})
  self.assertEqual(public['entities'][0]['property_confidence'],{'术语标准名称':.9})
  self.assertEqual(r.entities[0]['properties'],{'TermName':'容量'})
if __name__=="__main__": unittest.main()
