import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_extract_v3.benchmark import (
    aggregate, atomic_json_write, cache_key, discover_cases, freeze_case, property_metrics, snapshot_code,
    score_case, run_case,
)


class BenchmarkTests(unittest.TestCase):
    def test_failed_extraction_preserves_diagnostics(self):
        result = {'status':'failed','entities':[],'relations':[],
                  'diagnostics':[{'code':'HTTP_402'}], 'stats':{'calls':1}}
        with patch('knowledge_extract_v3.benchmark._chunks',return_value=[]), \
             patch('knowledge_extract_v3.benchmark.ontology_files',return_value=[]), \
             patch('algorithm.knowledge_extract_v3.extract_detailed',return_value=result):
            run = run_case({}, 'v3', object())
        self.assertEqual(run['status'],'failed')
        self.assertEqual(run['diagnostics'],result['diagnostics'])
        self.assertEqual(run['stats']['calls'],1)

    def test_identity_and_failed_case_denominator(self):
        golden = {"entities": [{"type": "A", "name": "one", "properties": {"p": "1"}}], "relations": []}
        predicted = {"entities": [{"type": "A", "name": "one", "properties": {"p": "2"}}], "relations": []}
        score = score_case(golden, predicted)
        self.assertEqual(score["entities"]["tp"], 1)
        self.assertEqual(score["property_values"]["values"]["fn"], 1)
        failed = score_case(golden, predicted, failed=True)
        self.assertEqual(failed["entities"]["fn"], 1)
        self.assertEqual(failed["entities"]["predicted"], 0)

    def test_property_values_are_exact_and_one_to_one(self):
        m = property_metrics(
            [{"type": "A", "name": "x", "properties": {"a": "1", "b": "2"}}],
            [{"type": "A", "name": "x", "properties": {"a": "1", "b": "3", "c": "4"}}],
        )
        self.assertEqual((m["values"]["tp"], m["values"]["fp"], m["values"]["fn"]), (1, 2, 1))
        self.assertEqual((m["keys"]["tp"], m["keys"]["fp"], m["keys"]["fn"]), (2, 1, 0))

    def test_freeze_records_hashes_and_missing_golden(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "benchmark" / "g" / "c"
            (root / "docs").mkdir(parents=True)
            (root / "ontology").mkdir()
            (root / "docs" / "a.md").write_text("source", encoding="utf-8")
            (root / "ontology" / "o.puml").write_text("@startuml\n@enduml", encoding="utf-8")
            case = discover_cases(Path(tmp) / "benchmark", ["g"])[0]
            manifest = freeze_case(case, Path(tmp) / "frozen")
            self.assertTrue(manifest["has_markdown"])
            self.assertFalse(manifest["has_golden"])
            self.assertEqual(len(manifest["source_hash"]), 64)
            self.assertTrue((Path(tmp) / "frozen" / "g" / "c" / "docs" / "a.md").is_file())

    def test_cache_key_changes_with_version_source(self):
        common = {"model": "m", "temperature": 0}
        self.assertNotEqual(cache_key(version="v2", source_hash="s", config=common, version_source_hash="a"),
                            cache_key(version="v2", source_hash="s", config=common, version_source_hash="b"))

    def test_code_snapshot_is_deduplicated_and_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.py"
            source.write_text("one", encoding="utf-8")
            out = Path(tmp) / "out"
            # Use a repository path so snapshot_code can preserve its relative name.
            import knowledge_extract_v3.benchmark as benchmark
            original_root = benchmark.ROOT
            benchmark.ROOT = Path(tmp)
            try:
                self.assertEqual(snapshot_code([source, source], out, "v3"), snapshot_code([source], out, "v3"))
                source.write_text("two", encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    snapshot_code([source], out, "v3")
            finally:
                benchmark.ROOT = original_root

    def test_atomic_json_write_is_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            atomic_json_write(path, {"status": "ok"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "ok")

    def test_aggregate_case_macro(self):
        s1 = score_case({"entities": [{"type": "A", "name": "x"}], "relations": []}, {"entities": [{"type": "A", "name": "x"}], "relations": []})
        s2 = score_case({"entities": [{"type": "A", "name": "y"}], "relations": []}, {"entities": [], "relations": []})
        self.assertEqual(aggregate([s1, s2])["entities"]["tp"], 1)
        self.assertEqual(aggregate([s1, s2])["case_macro"]["entities"], 0.5)


if __name__ == "__main__":
    unittest.main()
