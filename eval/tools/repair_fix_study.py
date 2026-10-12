"""Freeze production repair-scope changes for bounded API before/after trials."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

from algorithm.knowledge_extract.repair_scope import relation_repair_scope
from algorithm.knowledge_extract.jsonl_protocol import record_schema
from eval.tools.repetition_causal import catalog, section, replace_section


def scope_case(case):
    result = copy.deepcopy(case)
    user = result['call']['messages'][1]
    triples = re.findall(r'`([^`]+)` -\[`([^`]+)`\]-> `([^`]+)`',
                         section(user['content'], 'ontology relation signatures').group(1))
    plan = SimpleNamespace(allowed_types={v for s, _, t in triples for v in (s, t)},
                           relation_specs={'all': [SimpleNamespace(source=s, relation_id=r, target=t)
                                                   for s, r, t in triples]})
    endpoints = [{'id': key, **value} for key, value in catalog(result['call']['messages']).items()]
    issues = json.loads(section(user['content'], 'repair issues').group(1))
    scope = relation_repair_scope(plan, endpoints, issues, result['call']['schema'])
    result['call']['schema'] = scope.schema
    user['content'] = replace_section(user['content'], 'ontology relation signatures', scope.signatures)
    user['content'] = replace_section(user['content'], 'repair issues', json.dumps(scope.issues, ensure_ascii=False))
    user['content'] = replace_section(user['content'], 'record schema', json.dumps(record_schema(scope.schema), ensure_ascii=False, sort_keys=True))
    # The frozen baseline includes the shared JSONL instruction's inline schema.
    prefix = '每行记录的字段约束（仅供本地校验，不使用服务端结构化输出）：'
    system = result['call']['messages'][0]
    if prefix in system['content']:
        system['content'] = system['content'].split(prefix)[0] + prefix + json.dumps(record_schema(scope.schema), ensure_ascii=False)
    result['scope_diagnostics'] = {'skipped': scope.skipped, 'executable_issues': len(scope.issues),
                                   'production_would_call_model': bool(scope.issues)}
    return result


def build(out):
    root = Path('test_data/repetition/causal-v1')
    original = json.loads((root/'repair-clean-conflict.json').read_text(encoding='utf8'))
    original['id'] = 'repair-original'
    skipped = scope_case(original)
    skipped['id'] = 'repair-fixed-skip'
    # Positive control adds a provisional evidence-backed Constraint endpoint,
    # not just a new enum member. All changes are frozen and labeled.
    control = copy.deepcopy(original)
    control['id'] = 'repair-positive-original'
    user = control['call']['messages'][1]
    text = json.loads(section(user['content'], 'current chunk').group(1))['text']
    quote = next(v for v in re.findall(r'[^"\n]+', text) if '各事业 部要发挥' in v)
    quote = quote.split('陈凯')[0]
    known = section(user['content'], 'known entities').group(1)
    user['content'] = replace_section(user['content'], 'known entities',
                                      known + '\n| `entity-23` | `OperationalConstraint` | 发挥业务特长和聚焦研发协同的要求 | - |')
    issues = json.loads(section(user['content'], 'repair issues').group(1))[:1]
    user['content'] = replace_section(user['content'], 'repair issues', json.dumps(issues, ensure_ascii=False))
    triples = re.findall(r'`([^`]+)` -\[`([^`]+)`\]-> `([^`]+)`', section(user['content'], 'ontology relation signatures').group(1))
    types = {v['type'] for v in catalog(control['call']['messages']).values()}
    control['call']['schema']['properties']['relations']['items']['properties']['rel_type']['enum'] = sorted({r for s,r,t in triples if s in types and t in types})
    user['content'] = replace_section(user['content'], 'record schema', json.dumps(record_schema(control['call']['schema']), ensure_ascii=False, sort_keys=True))
    prefix = '每行记录的字段约束（仅供本地校验，不使用服务端结构化输出）：'
    control['call']['messages'][0]['content'] = control['call']['messages'][0]['content'].split(prefix)[0] + prefix + json.dumps(record_schema(control['call']['schema']), ensure_ascii=False)
    control['positive_control'] = {'new_endpoint': 'entity-23', 'target_endpoint': 'entity-14', 'quote': quote,
                                   'label_status': 'agent-reviewed / provisional; no independent human gold'}
    control['reference_anchors'] = []
    fixed = scope_case(control)
    fixed['id'] = 'repair-positive-fixed'
    cases = [original, skipped, control, fixed]
    for name in ['entity-segment-a', 'entity-segment-b']:
        cases.append(json.loads((root/(name+'.json')).read_text(encoding='utf8')))
    out.mkdir(parents=True, exist_ok=True)
    entries = []
    for case in cases:
        data = json.dumps(case, ensure_ascii=False, indent=2) + '\n'
        (out/(case['id']+'.json')).write_text(data, encoding='utf8', newline='\n')
        entries.append({'id': case['id'], 'sha256': hashlib.sha256(data.encode()).hexdigest()})
    # Impossible fixed arm is deliberately omitted: runtime skips, not an API null.
    sequence = ['repair-original', 'repair-positive-original', 'repair-positive-fixed',
                'entity-segment-a', 'entity-segment-b']
    plan = [[name,'baseline',repeat] for repeat in [1,2]
            for name in (sequence if repeat==1 else sequence[::-1])]
    (out/'plan.json').write_text(json.dumps(plan, indent=2), encoding='utf8', newline='\n')
    (out/'manifest.json').write_text(json.dumps({'cases': entries, 'max_tokens':16384,
                                              'label_status':'agent-reviewed / provisional',
                                              'skipped_arm_not_called':'repair-fixed-skip'},ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')
    return cases, plan


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,default=Path('test_data/repetition/repair-fix-v1'))
    args=parser.parse_args();cases,plan=build(args.out)
    print(json.dumps({'cases':len(cases),'api_calls':len(plan),'out':str(args.out)}))
