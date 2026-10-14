"""CLI contract for --relation-only-from-golden (no Backend extract POST)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
import pytest

from eval import run


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    case = workspace / "trouble" / "one"
    (case / "docs").mkdir(parents=True)
    (case / "docs" / "sample.md").write_text("张三就职于示例单位。", encoding="utf-8")
    ontology = workspace / "trouble" / "ontology"
    ontology.mkdir()
    (ontology / "schema.puml").write_text(
        "@startuml\nclass Person { name: String }\nclass Organization { name: String }\n"
        "Person --> Organization : worksAt\n@enduml\n",
        encoding="utf-8",
    )
    (case / "extract").mkdir()
    golden = {
        "entities": [
            {"type": "Person", "name": "张三", "properties": {}},
            {"type": "Organization", "name": "示例单位", "properties": {}},
        ],
        "relations": [
            {
                "from_type": "Person",
                "from_name": "张三",
                "rel_type": "worksAt",
                "to_type": "Organization",
                "to_name": "示例单位",
            }
        ],
    }
    (case / "extract" / "golden.json").write_text(json.dumps(golden), encoding="utf-8")
    return workspace


def test_relation_only_requires_from_run(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    argv = [
        "--workspace",
        str(workspace),
        "--mode",
        "extract",
        "--extract-version",
        "v2.2-v31",
        "--match-mode",
        "identity",
        "--relation-only-from-golden",
        "--runs-retention-days",
        "0",
        "trouble/one",
    ]
    with patch("sys.argv", ["eval/run.py", *argv]), patch.object(run, "log"):
        assert run.main() == 1


@pytest.mark.parametrize("variant,expected", [("ok", 0), ("empty", 2), ("wrong", 4)])
def test_relation_only_offline_skips_backend_extract(tmp_path: Path, monkeypatch, variant, expected) -> None:
    workspace = _workspace(tmp_path)
    frozen = tmp_path / "frozen"
    case_dir = frozen / "trouble" / "one"
    case_dir.mkdir(parents=True)
    chunks = {
        "code": 0,
        "data": {
            "items": [{"text": "张三就职于示例单位。", "ranges": [0, 10], "metadata": {}}],
            "count": 1,
        },
    }
    (case_dir / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")

    fake_envelope = {
        "code": 0,
        "info": "fixture",
        "data": {
            "graph": {
                "nodes": [
                    {"uid": "Person::张三", "label": "Person", "name": "张三", "properties": {"name": "张三"}},
                    {
                        "uid": "Organization::示例单位",
                        "label": "Organization",
                        "name": "示例单位",
                        "properties": {"name": "示例单位"},
                    },
                ],
                "relationships": [
                    {
                        "from_uid": "Person::张三",
                        "to_uid": "Organization::示例单位",
                        "type": "worksAt",
                    }
                ],
            },
            "quality": {
                "extract_version": "v2.2-v31",
                "status": "succeeded",
                "relation_only_from_golden": True,
                "deterministic_enrichment": "disabled",
            },
        },
    }

    if variant == "empty":
        fake_envelope["data"]["graph"]["relationships"] = []
    elif variant == "wrong":
        fake_envelope["data"]["graph"]["relationships"][0]["type"] = "wrong"
    posts: list[str] = []

    def boom_extract(*_args, **_kwargs):
        posts.append("extract")
        raise AssertionError("Backend extract must not be called in relation-only mode")

    monkeypatch.setattr(run, "_relation_only_offline_envelope", lambda *_a, **_k: fake_envelope)
    monkeypatch.setattr(run, "extract_graphs", boom_extract)
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    (tmp_path / "runs").mkdir()

    argv = [
        "--workspace",
        str(workspace),
        "--backend-url",
        "http://127.0.0.1:9",
        "--mode",
        "extract",
        "--from-run",
        str(frozen),
        "--extract-version",
        "v2.2-v31",
        "--relation-only-from-golden",
        "--min-relation-f1",
        "0.5",
        "--match-mode",
        "identity",
        "--runs-retention-days",
        "0",
        "trouble/one",
    ]
    with patch("sys.argv", ["eval/run.py", *argv]), patch.object(run, "log"):
        code = run.main()
    assert code == expected
    assert posts == []
    summaries = list((tmp_path / 'runs').glob('*/summary.json'))
    summary = json.loads(summaries[0].read_text(encoding='utf-8'))
    metrics = summary['cases'][0]['backend']['modules']['extract']['metrics']
    assert metrics['metric_scope'] == 'relations'
    assert metrics['entities']['evaluated'] is False and metrics['entities']['tp'] == 0
    assert metrics['relations']['f1'] == (1.0 if variant == 'ok' else 0.0)
    assert summary['overall']['backend_extract']['entities']['evaluated'] is False



def test_relation_only_uses_committed_service_and_shared_request_client(monkeypatch):
    from backend.app.services import extraction_config
    from algorithm.knowledge_extract import request_runtime, v2_2_v31
    chosen = {'service_snapshot': {'service_id': 'registered'}, 'model':'registered-model', 'max_concurrency':2}
    sentinel = object()
    seen = {}
    monkeypatch.setattr(extraction_config, 'resolve_config', lambda options: chosen)
    monkeypatch.setattr(request_runtime, 'create_request_llm', lambda config: sentinel if config is chosen else None)
    def harness(**kwargs):
        seen.update(kwargs)
        return {'status':'succeeded', 'graph':{'nodes':[], 'relationships':[]}}
    monkeypatch.setattr(v2_2_v31, 'extract_harness', harness)
    run._relation_only_offline_envelope('ontology', [{'text':'source'}], [{'type':'A','name':'n'}], {})
    assert seen['llm'] is sentinel and seen['relation_only'] is True
    assert 'api_key' not in seen and 'base_url' not in seen


def test_relation_only_rejects_bare_connection_overrides():
    import pytest
    with pytest.raises(ValueError, match='ops registered service'):
        run._relation_only_llm_credentials({'base_url':'https://unregistered', 'api_key':'test-only'})


def test_relation_only_identity_and_overall_cannot_count_seed_entities():
    from eval.quality import build_quality_gate
    metric = run.empty_metrics()
    metric['entities'].update(evaluated=False, source='golden_seed')
    metric['metric_scope'] = 'relations'
    total = run.empty_metrics()
    run.add_metrics(total, metric)
    final = run.finalize_metrics(total)
    assert final['entities']['evaluated'] is False and final['entities']['f1'] is None
    summary = {'metric_scope':'relations', 'match_mode':'identity', 'modes':['extract'],
        'cases':[{'group':'x', 'case':'y', 'scored':True, 'backend':{'modules':{'extract':{'metrics':metric}}}}]}
    assert build_quality_gate(summary)['passed']
    metric['identity'] = {'entities':{'tp':2, 'f1':1}, 'relations':metric['relations']}
    assert not build_quality_gate(summary)['passed']


def test_relation_only_provenance_is_distinct_from_end_to_end():
    from eval.provenance import _scoring
    base={'stage':'backend', 'modes':['extract'], 'match_mode':'llm'}
    assert _scoring(base) != _scoring({**base, 'relation_only_from_golden':True})
    assert _scoring({**base, 'relation_only_from_golden':True})['metric_scope']=='relations'
