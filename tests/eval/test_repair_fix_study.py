import hashlib
import json
from pathlib import Path

from eval.tools.repair_fix_study import build, scope_case
from eval.tools.repetition_causal import catalog, section


def test_fix_experiment_freezes_skip_and_real_positive_endpoints(tmp_path):
    cases, plan = build(tmp_path)
    by_id = {c['id']: c for c in cases}
    assert len(plan) == 10
    skipped = by_id['repair-fixed-skip']
    assert not skipped['scope_diagnostics']['production_would_call_model']
    assert len(skipped['scope_diagnostics']['skipped']) == 4
    assert all(r[0] != skipped['id'] for r in plan)
    positive = by_id['repair-positive-fixed']
    assert positive['scope_diagnostics']['production_would_call_model']
    assert positive['scope_diagnostics']['executable_issues'] == 1
    assert catalog(positive['call']['messages'])['entity-23']['type'] == 'OperationalConstraint'
    source = json.loads(section(positive['call']['messages'][1]['content'], 'current chunk').group(1))['text']
    assert positive['positive_control']['quote'] in source
    assert positive['positive_control']['label_status'].startswith('agent-reviewed')
    assert scope_case(positive)['call'] == positive['call']
    manifest = json.loads((tmp_path/'manifest.json').read_text(encoding='utf8'))
    for case in manifest['cases']:
        assert hashlib.sha256((tmp_path/(case['id']+'.json')).read_bytes()).hexdigest() == case['sha256']
