"""P0 止血回归：证据 fail-open 准入、修复循环默认 2 轮、LLM 主调用多重试恢复。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract/v3/tests/test_p0_failopen.py

覆盖：
- ``KG_EXTRACT_EVIDENCE_ADMISSION=warn``（默认）：mention/quote 无法逐字定位的实体
  保留并标记 ``evidence_status=unverified``；终验 ``EVIDENCE_INVALID`` 降为 warning，
  不计入 ``hard_issue_count``；``reject`` 恢复旧的拒收/硬错误行为；
- ``extract_split`` 默认 ``max_repair_rounds=2``：首轮 verification 有可修复 issue 时
  repair 循环真实执行并修复，持续可修复时两轮封顶；
- 实体/关系主调用恢复多重试（默认 3 次）：瞬时 JSON/timeout 失败重试后结果正常入池；
  ``max_attempts=1`` 时维持旧的单尝试行为；关系批全部失败仍保留 UNDECIDED 兜底。
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PACKAGE_DIR = Path(__file__).resolve().parents[1]


def _load_package_modules(package_name: str, directory: Path, stems: list[str]) -> dict[str, types.ModuleType]:
    # pytest 全量运行时多个测试文件共享 sys.modules：已加载的模块直接复用，
    # 避免同名模块被重复 exec 出多个类对象（isinstance/类型一致性污染）。
    parent = sys.modules.get(package_name)
    if parent is None:
        parent = types.ModuleType(package_name)
        parent.__path__ = [str(directory)]
        parent.__package__ = package_name
        sys.modules[package_name] = parent
    loaded: dict[str, types.ModuleType] = {}
    for stem in stems:
        module_name = f"{package_name}.{stem}"
        if module_name in sys.modules:
            loaded[stem] = sys.modules[module_name]
            continue
        spec = importlib.util.spec_from_file_location(module_name, directory / f"{stem}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        loaded[stem] = module
    return loaded


MODULES = _load_package_modules(
    "knowledge_extract_v3",
    PACKAGE_DIR,
    [
        "config",
        "chunk_context",
        "evidence",
        "verification",
        "candidate_generation",
        "split_extractor",
        "harness_extractor",
    ],
)
CFG = MODULES["config"]
CC = MODULES["chunk_context"]
EV = MODULES["evidence"]
V = MODULES["verification"]
SE = MODULES["split_extractor"]
HE = MODULES["harness_extractor"]

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))  # 允许独立运行（python tests/test_p0_failopen.py）

from algorithm.knowledge_extract.v3.tests.stub_llm import (  # noqa: E402
    StubLLM,
    span_item,
    task_of,
    user_prompt,
)


TERM_ONTOLOGY = "@startuml\nclass Term\n@enduml"
TERM_TEXT = "制动系统是关键子系统。"

REL_ONTOLOGY = (
    "@startuml\n"
    "class System\n"
    "class Part\n"
    'System "1" --> "0..*" Part : CONTAINS\n'
    "@enduml"
)
REL_TEXT = "制动系统包含制动泵与液压泵。"


def _chunk(text: str, chunk_id: str = "chunk-0") -> "CC.ExtractionChunk":
    return CC.ExtractionChunk(chunk_id=chunk_id, text=text)


class _FlakyLLM(StubLLM):
    """指定 task 的前 fail_times 次调用返回无效 JSON 或抛 timeout，随后恢复正常。"""

    def __init__(
        self,
        *,
        fail_task: str,
        fail_times: int,
        fail_mode: str = "json",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.fail_task = fail_task
        self.fail_times = fail_times
        self.fail_mode = fail_mode
        self.failed = 0

    def __call__(self, messages):
        prompt = user_prompt(messages)
        task = task_of(prompt)
        if task == self.fail_task and self.failed < self.fail_times:
            self.failed += 1
            self.calls.append((task, prompt))
            if self.fail_mode == "timeout":
                raise TimeoutError("simulated transient timeout")
            return "{not valid json"
        return super().__call__(messages)


class _FakeSchema:
    """validate_extraction 直通，不增删事实、不产生 schema 告警。"""

    @staticmethod
    def validate_extraction(raw, target_types=None, mode="strict"):
        return {
            "entities": list(raw.get("entities") or []),
            "relations": list(raw.get("relations") or []),
        }, []


class _SequencedVerifier:
    """按调用顺序返回预置的 VerificationReport（最后一份之后重复返回）。"""

    def __init__(self, reports: list["V.VerificationReport"]) -> None:
        self.reports = list(reports)
        self.calls = 0

    def verify(self, *args, **kwargs) -> "V.VerificationReport":
        index = min(self.calls, len(self.reports) - 1)
        self.calls += 1
        return self.reports[index]


class EvidenceAdmissionConfigTest(unittest.TestCase):
    """证据准入开关的解析与默认值。"""

    def test_default_is_warn_and_only_exactreject_restores_closed_gate(self) -> None:
        self.assertEqual(CFG.get_evidence_admission(), "warn")
        self.assertEqual(CFG.get_llm_config()["evidence_admission"], "warn")
        with patch.object(CFG, "KG_EXTRACT_EVIDENCE_ADMISSION", "reject"):
            self.assertEqual(CFG.get_evidence_admission(), "reject")
            self.assertEqual(CFG.get_llm_config()["evidence_admission"], "reject")
        with patch.object(CFG, "KG_EXTRACT_EVIDENCE_ADMISSION", "garbage"):
            self.assertEqual(CFG.get_evidence_admission(), "warn")

    def test_repair_and_attempt_config_defaults_and_clamps(self) -> None:
        config = CFG.get_llm_config()
        self.assertEqual(config["max_repair_rounds"], 2)
        self.assertEqual(config["max_attempts"], 3)
        with patch.object(CFG, "KG_EXTRACT_MAX_REPAIR_ROUNDS", 0), patch.object(
            CFG, "KG_EXTRACT_LLM_MAX_ATTEMPTS", 7
        ):
            config = CFG.get_llm_config()
            self.assertEqual(config["max_repair_rounds"], 0)
            self.assertEqual(config["max_attempts"], 7)
        with patch.object(CFG, "KG_EXTRACT_MAX_REPAIR_ROUNDS", 9):
            self.assertEqual(CFG.get_llm_config()["max_repair_rounds"], 2)

    def test_entrypoint_defaults(self) -> None:
        split_params = inspect.signature(SE.extract_split).parameters
        self.assertEqual(split_params["max_repair_rounds"].default, 2)
        self.assertEqual(split_params["max_attempts"].default, 3)
        harness_params = inspect.signature(HE.extract_harness).parameters
        # harness 不传时由 KG_EXTRACT_MAX_REPAIR_ROUNDS / KG_EXTRACT_LLM_MAX_ATTEMPTS 解析
        self.assertIsNone(harness_params["max_repair_rounds"].default)
        self.assertIsNone(harness_params["max_attempts"].default)


class EvidenceAdmissionPipelineTest(unittest.TestCase):
    """warn 模式实体保留 + 标记；reject 模式恢复拒收。"""

    @staticmethod
    def _entity_handler(_prompt: str, _chunk: dict) -> list[dict]:
        return [
            span_item("Term", "制动系统"),
            {
                "type": "Term",
                "mention": "归纳概念",
                "ungrounded": True,
                "evidence": {"quote": "原文中不存在的引文"},
            },
        ]

    def _run(self):
        llm = StubLLM(entity_handler=self._entity_handler)
        return SE.extract_split(
            TERM_ONTOLOGY,
            chunks=[_chunk(TERM_TEXT)],
            llm=llm,
            retry_base_seconds=0,
        )

    def test_warn_mode_keeps_unlocatable_entity_with_unverified_marker(self) -> None:
        result = self._run()
        entities = {entity["name"]: entity for entity in result["raw"]["entities"]}
        self.assertIn("制动系统", entities)
        # fail-open：不可定位候选不再被拒收
        self.assertIn("归纳概念", entities)
        unverified = entities["归纳概念"]
        self.assertEqual(unverified["evidence"]["evidence_status"], "unverified")
        # 正常实体的证据不带标记
        self.assertNotIn("evidence_status", entities["制动系统"].get("evidence") or {})
        codes = {warning["code"] for warning in result["warnings"]}
        self.assertIn("ENTITY_EVIDENCE_UNVERIFIED", codes)
        self.assertNotIn("ENTITY_EVIDENCE_REJECTED", codes)
        # 终验 EVIDENCE_INVALID 降为 warning，不再计入 hard_issue_count
        evidence_issues = [
            issue
            for issue in result["verification"]["issues"]
            if issue["code"] == "EVIDENCE_INVALID"
        ]
        self.assertTrue(evidence_issues)
        self.assertTrue(all(issue["severity"] == "warning" for issue in evidence_issues))
        self.assertEqual(result["verification"]["hard_issue_count"], 0)
        self.assertEqual(result["status"], "succeeded")
        self.assertIsNone(result["error_code"])
        quality = result["profile"]["resolution_quality"]
        self.assertGreaterEqual(quality["evidence_unverified_count"], 1)
        # EVIDENCE_L1/L2_FAIL 留痕仍计入 evidence_rejection_count（既有口径），
        # 关键是 ENTITY_EVIDENCE_REJECTED 拒收不再发生（上面 codes 断言覆盖）。

    def test_reject_mode_restores_old_rejection(self) -> None:
        with patch.object(CFG, "KG_EXTRACT_EVIDENCE_ADMISSION", "reject"):
            result = self._run()
        names = [entity["name"] for entity in result["raw"]["entities"]]
        self.assertIn("制动系统", names)
        self.assertNotIn("归纳概念", names)
        codes = {warning["code"] for warning in result["warnings"]}
        self.assertIn("ENTITY_EVIDENCE_REJECTED", codes)
        self.assertNotIn("ENTITY_EVIDENCE_UNVERIFIED", codes)
        self.assertGreaterEqual(
            result["profile"]["resolution_quality"]["evidence_rejection_count"], 1
        )


class EvidenceAdmissionVerifierTest(unittest.TestCase):
    """终验环节 EVIDENCE_INVALID 的 severity 随准入模式切换。"""

    @staticmethod
    def _verify():
        verdict = EV.EvidenceVerdict(
            ref="entities[0]",
            span_id="chunk-0:0:4",
            level="L2",
            status="fail",
            message="quote 与声明 span 不一致（L2 Literal Match 失败）",
        )
        auditor = SimpleNamespace(audit=lambda *args, **kwargs: ())
        with patch.object(V, "verify_evidence", return_value=[verdict]), patch.object(
            V, "CoverageAuditor", lambda: auditor
        ):
            return V.ExtractionVerifier().verify(
                SimpleNamespace(),
                _FakeSchema(),
                object(),
                SimpleNamespace(target_types=None),
                [{"type": "Term", "name": "制动系统"}],
                [],
            )

    def test_warn_mode_downgrades_evidence_invalid_to_warning(self) -> None:
        report = self._verify()
        issues = [issue for issue in report.issues if issue.code == "EVIDENCE_INVALID"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, "warning")
        self.assertTrue(report.valid)
        self.assertEqual(len(report.hard_issues), 0)
        self.assertEqual(report.to_dict()["hard_issue_count"], 0)

    def test_reject_mode_keeps_evidence_invalid_hard(self) -> None:
        with patch.object(CFG, "KG_EXTRACT_EVIDENCE_ADMISSION", "reject"):
            report = self._verify()
        issues = [issue for issue in report.issues if issue.code == "EVIDENCE_INVALID"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, "error")
        self.assertFalse(report.valid)
        self.assertEqual(report.to_dict()["hard_issue_count"], 1)


class RepairLoopTest(unittest.TestCase):
    """默认 max_repair_rounds=2：可修复 issue 触发 repair 并真实落地关系。"""

    @staticmethod
    def _entities(_prompt: str, _chunk: dict) -> list[dict]:
        return [
            span_item("System", "制动系统"),
            span_item("Part", "制动泵"),
            span_item("Part", "液压泵"),
        ]

    @staticmethod
    def _repairable_report() -> "V.VerificationReport":
        return V.VerificationReport(
            (
                V.VerificationIssue(
                    code="REQUIRED_RELATION_MISSING",
                    message="test: 必需的 CONTAINS 关系缺失",
                    path="chunk-0",
                    severity="warning",
                    repair_type="required_relation",
                    context={"source_block_ids": ["chunk-0"]},
                ),
            )
        )

    @staticmethod
    def _repairer(payload: dict) -> dict:
        target = "制动泵" if payload["round"] == 1 else "液压泵"
        quote = "制动系统包含制动泵" if target == "制动泵" else "与液压泵"
        return {
            "relations": [
                {
                    "from_type": "System",
                    "from_name": "制动系统",
                    "to_type": "Part",
                    "to_name": target,
                    "rel_type": "CONTAINS",
                    "evidence": {"quote": quote},
                }
            ]
        }

    def _run(self, reports: list["V.VerificationReport"]):
        llm = StubLLM(entity_handler=self._entities)
        verifier = _SequencedVerifier(reports)
        with patch.object(SE, "ExtractionVerifier", lambda: verifier):
            return SE.extract_split(
                REL_ONTOLOGY,
                chunks=[_chunk(REL_TEXT)],
                llm=llm,
                repairer=self._repairer,
                retry_base_seconds=0,
            )

    def test_repairable_issue_repaired_by_default_loop(self) -> None:
        result = self._run(
            [self._repairable_report(), V.VerificationReport(()), V.VerificationReport(())]
        )
        # 不传 max_repair_rounds：默认 2 轮生效，首轮修复后第二轮验证通过
        self.assertEqual(result["repair"]["stop_reason"], "validation_complete")
        self.assertEqual(len(result["repair"]["rounds"]), 1)
        pairs = {
            (relation["from_name"], relation["to_name"])
            for relation in result["raw"]["relations"]
        }
        self.assertIn(("制动系统", "制动泵"), pairs)
        self.assertEqual(result["status"], "succeeded")
        self.assertIsNone(result["error_code"])

    def test_repair_loop_caps_at_two_rounds_by_default(self) -> None:
        result = self._run([self._repairable_report()])
        self.assertEqual(result["repair"]["stop_reason"], "repair_round_limit")
        self.assertEqual(len(result["repair"]["rounds"]), 2)
        targets = {relation["to_name"] for relation in result["raw"]["relations"]}
        self.assertIn("制动泵", targets)
        self.assertIn("液压泵", targets)
        # 未修复完的 repairable issue 必须可观测
        self.assertEqual(result["error_code"], "REPAIR_INCOMPLETE")


class LlmRetryTest(unittest.TestCase):
    """实体/关系主调用多重试：瞬时失败重试后正常入池。"""

    def test_entity_pass_retries_transient_json_failure(self) -> None:
        llm = _FlakyLLM(
            fail_task="entity",
            fail_times=2,
            entity_handler=lambda _p, _c: [span_item("Term", "制动系统")],
        )
        result = SE.extract_split(
            TERM_ONTOLOGY,
            chunks=[_chunk(TERM_TEXT)],
            llm=llm,
            retry_base_seconds=0,
        )
        entity_calls = [task for task, _ in llm.calls if task == "entity"]
        self.assertEqual(len(entity_calls), 3)
        names = [entity["name"] for entity in result["raw"]["entities"]]
        self.assertIn("制动系统", names)
        self.assertIsNone(result["error_code"])
        self.assertEqual(result["status"], "succeeded")

    def test_entity_pass_retries_transient_timeout(self) -> None:
        llm = _FlakyLLM(
            fail_task="entity",
            fail_times=2,
            fail_mode="timeout",
            entity_handler=lambda _p, _c: [span_item("Term", "制动系统")],
        )
        result = SE.extract_split(
            TERM_ONTOLOGY,
            chunks=[_chunk(TERM_TEXT)],
            llm=llm,
            retry_base_seconds=0,
        )
        entity_calls = [task for task, _ in llm.calls if task == "entity"]
        self.assertEqual(len(entity_calls), 3)
        names = [entity["name"] for entity in result["raw"]["entities"]]
        self.assertIn("制动系统", names)
        self.assertIsNone(result["error_code"])

    def test_entity_primary_attempts_honor_max_attempts(self) -> None:
        # 永久失败 + 默认重试：3 次主调用 + 2 次前缀恢复尝试
        always_fail = _FlakyLLM(fail_task="entity", fail_times=99)
        result = SE.extract_split(
            TERM_ONTOLOGY,
            chunks=[_chunk(TERM_TEXT)],
            llm=always_fail,
            retry_base_seconds=0,
        )
        self.assertEqual(len([task for task, _ in always_fail.calls if task == "entity"]), 5)
        primary_attempts = [
            attempt
            for attempt in result["attempts"]
            if attempt["pass"] == "entity" and attempt["chunk"] == "chunk-0"
        ]
        self.assertEqual(len(primary_attempts), 3)
        self.assertEqual(result["raw"], {"entities": [], "relations": []})
        self.assertEqual(result["error_code"], "LLM_JSON_PARSE_FAILED")

        # max_attempts=1 维持旧的单尝试行为（恢复兜底仍保留 2 次）
        single = _FlakyLLM(fail_task="entity", fail_times=99)
        result = SE.extract_split(
            TERM_ONTOLOGY,
            chunks=[_chunk(TERM_TEXT)],
            llm=single,
            max_attempts=1,
            retry_base_seconds=0,
        )
        self.assertEqual(len([task for task, _ in single.calls if task == "entity"]), 3)
        primary_attempts = [
            attempt
            for attempt in result["attempts"]
            if attempt["pass"] == "entity" and attempt["chunk"] == "chunk-0"
        ]
        self.assertEqual(len(primary_attempts), 1)
        self.assertEqual(result["error_code"], "LLM_JSON_PARSE_FAILED")

    @staticmethod
    def _relation_entities(_prompt: str, _chunk: dict) -> list[dict]:
        return [span_item("System", "制动系统"), span_item("Part", "制动泵")]

    @staticmethod
    def _relation_decisions(_prompt: str, pairs: list[dict], _chunk: dict) -> list[dict]:
        return [
            {
                "pair_id": pairs[0]["pair_id"],
                "label": pairs[0]["relation_id"],
                "evidence": {"quote": "制动系统包含制动泵"},
            }
        ]

    def test_relation_batch_retries_transient_json_failure(self) -> None:
        llm = _FlakyLLM(
            fail_task="relation",
            fail_times=2,
            entity_handler=self._relation_entities,
            relation_handler=self._relation_decisions,
        )
        result = SE.extract_split(
            REL_ONTOLOGY,
            chunks=[_chunk("制动系统包含制动泵。")],
            llm=llm,
            retry_base_seconds=0,
        )
        relation_calls = [task for task, _ in llm.calls if task == "relation"]
        self.assertEqual(len(relation_calls), 3)
        rel_types = [relation["rel_type"] for relation in result["raw"]["relations"]]
        self.assertIn("CONTAINS", rel_types)
        codes = {warning["code"] for warning in result["warnings"]}
        self.assertNotIn("RELATION_PAIR_UNDECIDED", codes)
        self.assertNotIn("RELATION_BATCH_UNDECIDED", codes)
        self.assertIsNone(result["error_code"])

    def test_relation_batch_exhaustion_keeps_undecided_fallback(self) -> None:
        llm = _FlakyLLM(
            fail_task="relation",
            fail_times=99,
            entity_handler=self._relation_entities,
            relation_handler=self._relation_decisions,
        )
        result = SE.extract_split(
            REL_ONTOLOGY,
            chunks=[_chunk("制动系统包含制动泵。")],
            llm=llm,
            retry_base_seconds=0,
        )
        # 单 pair 批不可二分：3 次尝试后保留 UNDECIDED 兜底，不解释为 NONE
        relation_calls = [task for task, _ in llm.calls if task == "relation"]
        self.assertEqual(len(relation_calls), 3)
        codes = {warning["code"] for warning in result["warnings"]}
        self.assertIn("RELATION_PAIR_UNDECIDED", codes)
        self.assertEqual(result["raw"]["relations"], [])


if __name__ == "__main__":
    unittest.main()
