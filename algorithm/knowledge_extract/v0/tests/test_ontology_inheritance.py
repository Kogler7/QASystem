"""继承解析：``rdfs:subClassOf`` 方言归一化（基线评审 §3.8 H1）。

修之前：用 ``rdfs_subClassOf`` 表达继承的本体，``parents`` 恒为空 → 继承链整条丢失、
``include_subclasses`` 退化为空操作、``is_assignable`` 退化为「类型必须全等」、
子类拿不到父类属性，且全部静默。旧 v3.1 副本（V2 完整拷贝）已在自己的解析器里修过；
当前 v3.1 语义 harness 复用 V2 解析器，因此这里补齐同一契约（参考实现：``v3_1/ontology_schema.py``）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from algorithm.knowledge_extract.v0.ontology_schema import OntologySchema

_RELATION_TEMPLATE = """@startuml
class "文档" as Document {{
+ 文档编号 (docNo): String
}}
class "章" as Chapter {{
+ 章编号 (chapterNo): String
}}
class "条" as Clause {{
+ 条编号 (clauseNo): String
}}
"Clause" "1" --> "1" "Chapter" : {label}
@enduml
"""

_CHAIN_ONTOLOGY = """@startuml
class "信息单元" as InformationUnit {{
+ 单元编号 (unitNo): String
}}
' @identity InformationUnit: unitNo
class "章" as Chapter
class "条" as Clause {{
+ 条编号 (clauseNo): String
}}
"Chapter" "1" --> "1" "InformationUnit" : rdfs_subClassOf
"Clause" "1" --> "1" "Chapter" : rdfs_subClassOf
@enduml
"""


@pytest.mark.parametrize(
    "label",
    ["rdfs_subClassOf", "rdfs:subClassOf", "rdfs:SubClassOf", "rdfs:subclassof", "rdfs:sub_class_of"],
)
def test_subclassof_spellings_are_inheritance(label):
    schema = OntologySchema.parse(_RELATION_TEMPLATE.format(label=label))
    assert schema.parents == {"Clause": ("Chapter",)}
    assert schema.relations == []  # 继承不是关系：不能同时进关系集合
    assert schema.is_assignable("Clause", "Chapter") is True


def test_stereotype_only_spelling_is_inheritance_too():
    schema = OntologySchema.parse(_RELATION_TEMPLATE.format(label="<<rdfs_subClassOf>>"))
    assert schema.parents == {"Clause": ("Chapter",)}
    assert schema.relations == []


def test_plain_relation_is_not_treated_as_inheritance():
    """普通关系（写法相近但不是 subClassOf）必须保持关系语义。"""
    schema = OntologySchema.parse(_RELATION_TEMPLATE.format(label="hasSubClause"))
    assert schema.parents == {}
    assert [rel.identifier for rel in schema.relations] == ["hasSubClause"]


def test_descendants_and_ancestors_follow_the_restored_chain():
    """include_subclasses 不再退化为空操作（基线 §3.8 H1 一号后果）。"""
    schema = OntologySchema.parse(_CHAIN_ONTOLOGY)
    assert schema.ancestors("Clause") == {"Chapter", "InformationUnit"}
    assert schema.descendants("InformationUnit") == {"Chapter", "Clause"}
    assert schema.is_assignable("Clause", "InformationUnit") is True


def test_inherited_properties_and_identity_reach_the_subclass():
    schema = OntologySchema.parse(_CHAIN_ONTOLOGY)
    assert schema.resolve_property("Clause", "clauseNo") is not None
    assert schema.resolve_property("Clause", "unitNo") is not None  # 祖父属性
    assert schema.identity_properties_for("Clause") == ("unitNo",)


def test_inheritance_cycle_is_safe():
    schema = OntologySchema.parse(
        '@startuml\nclass "甲" as A\nclass "乙" as B\n'
        '"A" "1" --> "1" "B" : rdfs_subClassOf\n'
        '"B" "1" --> "1" "A" : rdfs_subClassOf\n@enduml'
    )
    assert schema.parents == {"A": ("B",), "B": ("A",)}
    assert schema.ancestors("A") == {"A", "B"}
    assert schema.descendants("A") == {"A", "B"}
    assert schema.relations == []


def test_real_mini_ontology_inheritance_and_inherited_fields():
    """真实 mini 本体回归：修前 ``parents`` 为 0，且子类上的继承字段被判为未声明。

    ``Drafter.姓名`` / ``Drafting Organization.名称`` 是上一轮 mini（standard_01）里
    71 条 ``INVALID_PROPERTY_SCHEMA`` 阻断项中的 67 条，根因就是这条继承链缺失。
    """
    ontology_path = (Path(__file__).resolve().parents[4]
                     / "test_data/eval/standard/ontology/Standard_Ontology.puml")
    if not ontology_path.is_file():  # pragma: no cover - 数据缺失时不误报
        pytest.skip(f"缺少评测本体: {ontology_path}")
    schema = OntologySchema.parse(ontology_path.read_text(encoding="utf-8"))
    assert sum(len(parents) for parents in schema.parents.values()) > 0
    assert schema.is_assignable("Clause", "InformationUnit") is True
    assert schema.is_assignable("Drafter", "Individual") is True
    assert schema.resolve_property("Drafting Organization", "名称") is not None
    assert schema.resolve_property("Drafter", "姓名") is not None
    assert not [rel for rel in schema.relations
                if rel.identifier.casefold().replace("_", "").replace(":", "") == "rdfssubclassof"]
