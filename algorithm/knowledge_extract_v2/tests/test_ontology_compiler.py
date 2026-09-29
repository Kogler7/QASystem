"""Phase 1 Step 2 单元测试：Ontology Compiler / TargetScope / Schema QA。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract_v2/tests/test_ontology_compiler.py

覆盖：
- class hierarchy / properties（含继承）/ relations / identity / cardinality 编译；
- TargetScope：单类、多类、父类含子类、全文、未知类型、retain_unclassified（AC-2）；
- Schema QA：§13 全部检查类型，strict / permissive 两种模式，不崩溃（AC-9）；
- 基准集 4 个运行时本体可编译且无未解析端点。
"""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]
FIXTURES_DIR = PACKAGE_DIR / "tests" / "fixtures"
ONTOLOGIES_DIR = FIXTURES_DIR / "ontologies"
CASES_DIR = FIXTURES_DIR / "cases"


def _load_package_modules(package_name: str, directory: Path, stems: list[str]) -> dict[str, types.ModuleType]:
    """绕过包 __init__（避免 openai 依赖）加载子模块，与 benchmark/eval.py 同模式。"""
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
    "knowledge_extract_v2",
    PACKAGE_DIR,
    ["graph_utils", "ontology_schema", "ontology_compiler"],
)
COMPILER = MODULES["ontology_compiler"]


RULE_ONTOLOGY = """@startuml rule-ontology
class "规则" as Rule <<KnowledgeUnit>> {
  + 规则编号 (ruleId): String // 规则唯一编号
  + 规则内容 (ruleText): String // 规则内容原文
}
class "简单规则" as SimpleRule <<KnowledgeUnit>> {
  + 规则编号 (ruleId): String // 规则唯一编号
  + 规则内容 (ruleText): String // 规则内容原文
  + 触发条件 (triggerCondition): String // 触发条件
  + 动作 (action): String // 执行动作
}
class "复合规则" as CompositeRule <<KnowledgeUnit>> {
  + 规则编号 (ruleId): String // 规则唯一编号
  + 规则内容 (ruleText): String // 规则内容原文
  + 组合类型 (combinationType): String // AND / OR / SEQUENCE
}
class "对象" as Object <<KnowledgeUnit>> {
  + 对象名称 (objectName): String // 对象名称
}
class "特性" as Property <<KnowledgeUnit>> {
  + 特性名称 (propertyName): String // 特性名称
}
Rule <|-- SimpleRule
Rule <|-- CompositeRule
CompositeRule "1" --> "1..*" SimpleRule : hasSubRule
SimpleRule "1" --> "0..*" Object : constrainsObject
Object "1" --> "0..*" Property : hasCharacteristic
' @identity Rule: ruleId
' @identity SimpleRule: ruleId
' @identity CompositeRule: ruleId
' @identity Object: objectName
' @identity Property: propertyName
@enduml
"""


UNRESOLVED_ONTOLOGY = """@startuml unresolved
class "公文管理单元" as OfficialDocument {
  + 文件标题 (documentTitle): String
}
class "公文结构单元" as DocumentUnit {
  + 单元标识 (unitId): String
}
OfficialDocument "1" --> "1..*" DocumentUnit : HAS_PART
OfficialDocument "1" --> "0..*" 公文关系 : LINKS_TO
' @identity OfficialDocument: documentTitle
' @identity DocumentUnit: unitId
@enduml
"""


QA_ONTOLOGY = """@startuml qa
abstract class "抽象规则" as AbstractRule <<KnowledgeUnit>> {
  + 规则编号 (ruleId): String // 规则唯一编号
  + 重复属性 (dupProp): String // 第一次声明
  + 重复属性 (dupProp): Integer // 第二次声明，类型不一致
  + 枚举状态 (status): String // 枚举：待办/进行中/完成
  + 对象描述 (description): String // 对象名称
  + 对象名称 (objectName): String // 不是对象名称
}
class "规则" as Rule {
}
Rule --|> AbstractRule
Rule --> Object : constrainsObject
AbstractRule --> Rule : relatesTo
' 注意：Object 未声明，触发 UNRESOLVED_RELATION_ENDPOINT_TYPE
class "循环A" as CycleA {
}
class "循环B" as CycleB {
}
CycleA --|> CycleB
CycleB --|> CycleA
' @identity Rule: ruleId
@enduml
"""


def ontology_file(name: str) -> str:
    return (ONTOLOGIES_DIR / name).read_text(encoding="utf-8")


def case_target_types(case_id: str) -> list[str]:
    payload = (CASES_DIR / f"{case_id}.json").read_text(encoding="utf-8")
    import json

    return json.loads(payload).get("target_types") or []


def relations_in_scope(plan) -> tuple:
    """当前 ExtractionPlan 不再提供 relations_in_scope()；按 expanded_types 过滤签名。"""
    expanded = plan.target_scope.expanded_types
    return tuple(
        spec
        for specs in plan.relation_specs.values()
        for spec in specs
        if spec.source in expanded and spec.target in expanded
    )


class CompileBasicsTest(unittest.TestCase):
    def test_rule_ontology_hierarchy_properties_identity(self) -> None:
        plan = COMPILER.compile_plan(RULE_ONTOLOGY)
        self.assertEqual(plan.ontology_id, "rule-ontology")
        self.assertEqual(set(plan.class_specs), {"Rule", "SimpleRule", "CompositeRule", "Object", "Property"})
        # 类层次
        rule_spec = plan.class_specs["Rule"]
        self.assertEqual(rule_spec.children, ("CompositeRule", "SimpleRule"))
        self.assertEqual(plan.class_specs["SimpleRule"].parents, ("Rule",))
        self.assertEqual(plan.class_specs["SimpleRule"].is_abstract, False)
        # identity
        self.assertEqual(plan.identity_specs["Rule"].properties, ("ruleId",))
        self.assertEqual(plan.identity_specs["Rule"].inherited, False)
        # 属性（含继承顺序：自身在前）
        simple_props = [prop.identifier for prop in plan.class_specs["SimpleRule"].properties]
        self.assertEqual(simple_props, ["ruleId", "ruleText", "triggerCondition", "action"])
        self.assertEqual(plan.class_specs["SimpleRule"].properties[0].source_class, "SimpleRule")

    def test_rule_ontology_relations_and_cardinality(self) -> None:
        plan = COMPILER.compile_plan(RULE_ONTOLOGY)
        has_sub_rule = plan.resolve_relation("hasSubRule")
        self.assertEqual(len(has_sub_rule), 1)
        self.assertEqual(has_sub_rule[0].source, "CompositeRule")
        self.assertEqual(has_sub_rule[0].target, "SimpleRule")
        self.assertEqual(has_sub_rule[0].source_cardinality, "1")
        self.assertEqual(has_sub_rule[0].target_cardinality, "1..*")
        self.assertEqual(has_sub_rule[0].signature, "CompositeRule -[hasSubRule]-> SimpleRule")
        constrains_object = plan.resolve_relation("CONSTRAINSOBJECT")
        self.assertEqual(len(constrains_object), 1)
        self.assertEqual(constrains_object[0].source, "SimpleRule")
        self.assertEqual(constrains_object[0].target, "Object")

    def test_rule_ontology_clean_qa(self) -> None:
        plan = COMPILER.compile_plan(RULE_ONTOLOGY)
        self.assertEqual(plan.schema_warnings, ())

    def test_feature_specs_derived_deterministically(self) -> None:
        plan = COMPILER.compile_plan(RULE_ONTOLOGY)
        rule_features = {feature.feature_id: feature for feature in plan.feature_specs["Rule"]}
        intrinsic = rule_features["Rule.ruleId"]
        self.assertEqual(intrinsic.kind, "intrinsic")
        self.assertEqual(intrinsic.identifier, "ruleId")
        self.assertIsNone(intrinsic.required)
        self.assertIsNone(intrinsic.multi_value)
        self.assertIsNone(intrinsic.context_inferable)
        composite_features = {feature.feature_id: feature for feature in plan.feature_specs["CompositeRule"]}
        self.assertEqual(composite_features["CompositeRule.hasSubRule"].kind, "relation")
        self.assertEqual(
            composite_features["CompositeRule.hasSubRule"].relation_signature,
            "CompositeRule -[hasSubRule]-> SimpleRule",
        )


class TargetScopeTest(unittest.TestCase):
    def test_single_type(self) -> None:
        official = ontology_file("official_document_ontology.puml")
        plan = COMPILER.compile_plan(official, target_types=["OperationalActivity"])
        self.assertEqual(plan.target_scope.requested_types, ("OperationalActivity",))
        self.assertEqual(plan.target_scope.expanded_types, frozenset({"OperationalActivity"}))
        self.assertTrue(plan.allows("OperationalActivity"))
        self.assertFalse(plan.allows("OperationalAgent"))
        self.assertEqual(
            {spec.signature for spec in relations_in_scope(plan)},
            {"OperationalActivity -[PRECEDES]-> OperationalActivity"},
        )

    def test_parent_includes_subclasses(self) -> None:
        plan = COMPILER.compile_plan(RULE_ONTOLOGY, target_types=["Rule"], include_subclasses=True)
        self.assertEqual(
            plan.target_scope.expanded_types,
            frozenset({"Rule", "SimpleRule", "CompositeRule"}),
        )

    def test_parent_without_subclasses(self) -> None:
        plan = COMPILER.compile_plan(RULE_ONTOLOGY, target_types=["Rule"], include_subclasses=False)
        self.assertEqual(plan.target_scope.expanded_types, frozenset({"Rule"}))

    def test_multiple_types(self) -> None:
        official = ontology_file("official_document_ontology.puml")
        plan = COMPILER.compile_plan(
            official,
            target_types=["OperationalActivity", "OperationalConstraint"],
        )
        self.assertEqual(
            plan.target_scope.expanded_types,
            frozenset({"OperationalActivity", "OperationalConstraint"}),
        )
        in_scope = relations_in_scope(plan)
        self.assertEqual(
            {spec.signature for spec in in_scope},
            {
                "OperationalConstraint -[CONSTRAINS_ACTIVITY]-> OperationalActivity",
                "OperationalActivity -[PRECEDES]-> OperationalActivity",
            },
        )

    def test_full_coverage_when_no_target(self) -> None:
        plan = COMPILER.compile_plan(RULE_ONTOLOGY)
        self.assertEqual(
            plan.target_scope.expanded_types,
            frozenset({"Rule", "SimpleRule", "CompositeRule", "Object", "Property"}),
        )
        self.assertEqual(plan.target_scope.requested_types, ())

    def test_unknown_target_type_warns_and_excludes(self) -> None:
        plan = COMPILER.compile_plan(RULE_ONTOLOGY, target_types=["DoesNotExist"])
        codes = [warning.code for warning in plan.schema_warnings]
        self.assertIn("UNKNOWN_TARGET_TYPE", codes)
        self.assertEqual(plan.target_scope.expanded_types, frozenset())

    def test_retain_unclassified_with_element_class(self) -> None:
        source = RULE_ONTOLOGY.replace(
            "class \"特性\" as Property <<KnowledgeUnit>> {",
            "class \"元素\" as Element <<KnowledgeUnit>> {\n}\nclass \"特性\" as Property <<KnowledgeUnit>> {",
        )
        plan = COMPILER.compile_plan(
            source,
            target_types=["Rule"],
            unknown_content_policy="retain_unclassified",
        )
        self.assertEqual(plan.target_scope.unknown_content_policy, "retain_unclassified")
        self.assertIn("Element", plan.target_scope.expanded_types)

    def test_retain_unclassified_falls_back_without_element(self) -> None:
        plan = COMPILER.compile_plan(
            RULE_ONTOLOGY,
            target_types=["Rule"],
            unknown_content_policy="retain_unclassified",
        )
        codes = [warning.code for warning in plan.schema_warnings]
        self.assertIn("UNCLASSIFIED_RETENTION_UNAVAILABLE", codes)
        self.assertEqual(plan.target_scope.unknown_content_policy, "ignore")
        self.assertNotIn("Element", plan.target_scope.expanded_types)

    def test_invalid_policy_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            COMPILER.compile_plan(RULE_ONTOLOGY, unknown_content_policy="explode")


class SchemaQATest(unittest.TestCase):
    def test_unresolved_relation_endpoint_strict(self) -> None:
        plan = COMPILER.compile_plan(UNRESOLVED_ONTOLOGY, mode="strict")
        warning = next(w for w in plan.schema_warnings if w.code == "UNRESOLVED_RELATION_ENDPOINT_TYPE")
        self.assertEqual(warning.severity, "error")
        self.assertEqual(warning.value["relation"], "LINKS_TO")
        self.assertEqual(warning.value["unresolved_endpoints"], ["公文关系"])
        # strict 模式：该关系不可用
        self.assertEqual(plan.resolve_relation("LINKS_TO"), ())
        # 已解析关系仍可用
        self.assertEqual(len(plan.resolve_relation("HAS_PART")), 1)

    def test_unresolved_relation_endpoint_permissive(self) -> None:
        plan = COMPILER.compile_plan(UNRESOLVED_ONTOLOGY, mode="permissive")
        warning = next(w for w in plan.schema_warnings if w.code == "UNRESOLVED_RELATION_ENDPOINT_TYPE")
        self.assertEqual(warning.severity, "warning")
        self.assertEqual(plan.resolve_relation("LINKS_TO"), ())

    def test_duplicate_class_and_property_datatype(self) -> None:
        source = RULE_ONTOLOGY + "\nclass \"重复规则\" as Rule <<KnowledgeUnit>> {\n}\n"
        plan = COMPILER.compile_plan(source)
        codes = [warning.code for warning in plan.schema_warnings]
        self.assertIn("DUPLICATE_CLASS", codes)

    def test_qa_checks(self) -> None:
        plan = COMPILER.compile_plan(QA_ONTOLOGY, mode="strict")
        by_code: dict[str, list[COMPILER.SchemaWarning]] = {}
        for warning in plan.schema_warnings:
            by_code.setdefault(warning.code, []).append(warning)
        self.assertIn("DUPLICATE_PROPERTY_DATATYPE_MISMATCH", by_code)
        self.assertIn("INVALID_ENUM_DESCRIPTION", by_code)
        self.assertIn("MISSING_IDENTITY", by_code)
        self.assertIn("MISSING_CARDINALITY", by_code)
        self.assertIn("UNRESOLVED_RELATION_ENDPOINT_TYPE", by_code)
        self.assertIn("INHERITANCE_CYCLE", by_code)
        # 交叉描述注释 + 否定表达注释
        annotation_conflicts = {
            warning.value.get("property"): warning
            for warning in by_code.get("ANNOTATION_CONFLICT", [])
            if warning.value.get("property") is not None
        }
        self.assertIn("description", annotation_conflicts)
        self.assertIn("objectName", annotation_conflicts)
        # 继承环两个类都报告
        self.assertEqual(
            {warning.value for warning in by_code["INHERITANCE_CYCLE"]},
            {"CycleA", "CycleB"},
        )

    def test_abstract_class_compiled(self) -> None:
        plan = COMPILER.compile_plan(QA_ONTOLOGY)
        self.assertTrue(plan.class_specs["AbstractRule"].is_abstract)
        self.assertFalse(plan.class_specs["Rule"].is_abstract)


class BenchmarkOntologyTest(unittest.TestCase):
    def test_benchmark_ontologies_compile_cleanly(self) -> None:
        cases = [
            ("official_document_ontology.puml", "04_ontology_class_extraction"),
            ("rule_ontology.puml", "06_composite_rule"),
            ("standard_ontology.puml", "01_standard"),
            ("terminology_ontology.puml", "03_terminology"),
        ]
        for ontology_name, case_id in cases:
            with self.subTest(ontology=ontology_name):
                source = ontology_file(ontology_name)
                plan = COMPILER.compile_plan(
                    source,
                    target_types=case_target_types(case_id),
                )
                # 无未解析端点；扩展范围必须是已声明类
                codes = [warning.code for warning in plan.schema_warnings]
                self.assertNotIn("UNRESOLVED_RELATION_ENDPOINT_TYPE", codes)
                self.assertNotIn("UNRESOLVED_INHERITANCE_ENDPOINT", codes)
                self.assertLessEqual(set(plan.allowed_types), set(plan.class_specs))
                for specs in plan.relation_specs.values():
                    for spec in specs:
                        self.assertIn(spec.source, plan.class_specs)
                        self.assertIn(spec.target, plan.class_specs)

    def test_official_document_annotation_conflict_detected(self) -> None:
        plan = COMPILER.compile_plan(ontology_file("official_document_ontology.puml"))
        conflicts = [
            warning
            for warning in plan.schema_warnings
            if warning.code == "ANNOTATION_CONFLICT"
            and warning.value.get("property") == "domain"
            and warning.value.get("referenced") == "description"
        ]
        self.assertEqual(len(conflicts), 1)

    def test_standard_ontology_missing_identity_reported(self) -> None:
        plan = COMPILER.compile_plan(ontology_file("standard_ontology.puml"))
        missing = {warning.value for warning in plan.schema_warnings if warning.code == "MISSING_IDENTITY"}
        self.assertIn("Stakeholder", missing)
        self.assertIn("ExternalResource", missing)
        self.assertNotIn("Standard", missing)

    def test_terminology_ontology_clean(self) -> None:
        plan = COMPILER.compile_plan(ontology_file("terminology_ontology.puml"))
        self.assertEqual(plan.schema_warnings, ())

    def test_ac2_gold_entities_inside_expanded_scope(self) -> None:
        import json

        case = json.loads((CASES_DIR / "04_ontology_class_extraction.json").read_text(encoding="utf-8"))
        plan = COMPILER.compile_plan(
            ontology_file("official_document_ontology.puml"),
            target_types=case["target_types"],
            include_subclasses=case["include_subclasses"],
        )
        for entity in case["expected"]["entities"]:
            self.assertIn(entity["type"], plan.target_scope.expanded_types)


class PlanSerializationTest(unittest.TestCase):
    def test_to_dict(self) -> None:
        plan = COMPILER.compile_plan(RULE_ONTOLOGY, target_types=["Rule"])
        payload = plan.to_dict()
        self.assertEqual(payload["ontology_id"], "rule-ontology")
        self.assertEqual(
            set(payload["allowed_types"]),
            {"Rule", "SimpleRule", "CompositeRule"},
        )
        self.assertEqual(payload["target_scope"]["expanded_types"], ["CompositeRule", "Rule", "SimpleRule"])
        self.assertEqual(payload["relations"], 3)
        self.assertEqual(payload["classes"], 5)
        warning_dict = COMPILER.compile_plan(UNRESOLVED_ONTOLOGY).to_dict()["warnings"][0]
        self.assertEqual(warning_dict["code"], "UNRESOLVED_RELATION_ENDPOINT_TYPE")
        self.assertEqual(warning_dict["severity"], "error")


class OntologyHardeningTest(unittest.TestCase):
    def test_identity_unknown_property_and_unsupported_declaration_are_diagnostics(self) -> None:
        source = '''@startuml hardening
interface IgnoredInterface
class Base {
  + id (id): String
}
class Child {
  + own (own): String
}
Child --|> Base
' @identity Child: missing
' @identity Missing: id
@enduml'''
        plan = COMPILER.compile_plan(source)
        codes = {warning.code for warning in plan.schema_warnings}
        self.assertIn("IDENTITY_UNKNOWN_PROPERTY", codes)
        self.assertIn("IDENTITY_UNKNOWN_CLASS", codes)
        self.assertIn("UNSUPPORTED_DECLARATION", codes)

    def test_deep_inheritance_materializes_all_ancestor_properties(self) -> None:
        source = '''@startuml deep
class Grand {
  + grand (grand): String
}
class Parent {
  + parent (parent): Integer
}
class Child {
  + child (child): Boolean
}
Parent --|> Grand
Child --|> Parent
' @identity Grand: grand
@enduml'''
        plan = COMPILER.compile_plan(source)
        self.assertEqual(
            [prop.identifier for prop in plan.class_specs["Child"].properties],
            ["child", "grand", "parent"],
        )

    def test_multiple_parent_identity_and_override_type_conflicts_are_errors(self) -> None:
        source = '''@startuml conflicts
class Left {
  + left (left): String
}
class Right {
  + right (right): String
}
class Child {
  + left (left): Integer
}
Child --|> Left
Child --|> Right
' @identity Left: left
' @identity Right: right
@enduml'''
        plan = COMPILER.compile_plan(source)
        codes = {warning.code for warning in plan.schema_warnings}
        self.assertIn("MULTIPLE_INHERITED_IDENTITY_CONFLICT", codes)
        self.assertIn("INHERITED_PROPERTY_DATATYPE_MISMATCH", codes)
        self.assertEqual(plan.identity_specs["Child"].properties, ())

    def test_preprocessor_is_rejected_by_restricted_dialect(self) -> None:
        source = '''@startuml unsafe
!include https://example.invalid/ontology.puml
class Safe {
  + id (id): String
}
' @identity Safe: id
@enduml'''
        plan = COMPILER.compile_plan(source)
        warning = next(
            warning for warning in plan.schema_warnings
            if warning.code == "UNSUPPORTED_PREPROCESSOR"
        )
        self.assertEqual(warning.severity, "error")
        self.assertIn("!include", warning.value)

    def test_duplicate_class_has_deterministic_error(self) -> None:
        source = '''@startuml duplicate
class Item {
  + id (id): String
}
class Item {
  + other (other): String
}
@enduml'''
        plan = COMPILER.compile_plan(source)
        self.assertEqual(
            sum(warning.code == "DUPLICATE_CLASS" for warning in plan.schema_warnings),
            1,
        )
        self.assertEqual(plan.class_specs["Item"].properties[0].identifier, "other")


if __name__ == "__main__":
    unittest.main(verbosity=2)
