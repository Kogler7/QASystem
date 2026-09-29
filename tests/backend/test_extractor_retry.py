from types import SimpleNamespace

from algorithm.knowledge_extract.v1 import extractor
from algorithm.knowledge_extract.v1.ontology_schema import OntologySchema


def _stream(text):
    midpoint = max(1, len(text) // 2)
    return iter(
        [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text[:midpoint]))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text[midpoint:]))]),
        ]
    )


def test_extract_v2_streams_and_retries_invalid_json(monkeypatch):
    responses = iter(
        [
            _stream("not-json"),
            _stream('{"entities":[{"type":"Term","name":"A","properties":{}}],"relations":[]}'),
        ]
    )
    create = lambda **_kwargs: next(responses)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=lambda: None,
    )
    monkeypatch.setattr(extractor, "_build_openai_client", lambda **_kwargs: client)
    monkeypatch.setattr(extractor.time, "sleep", lambda _seconds: None)
    result = extractor.extract_v2("class Term", "text", json_retries=1)
    assert result.get("_error") is None
    assert result["graph"]["nodes"][0]["uid"] == "term::a"
    assert result["attempts"][0]["type"] == "json"


def test_extract_v2_reports_json_retry_details(monkeypatch):
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: _stream("invalid"))),
        close=lambda: None,
    )
    monkeypatch.setattr(extractor, "_build_openai_client", lambda **_kwargs: client)
    result = extractor.extract_v2("class Term", "text", json_retries=1)
    assert result["error_code"] == "LLM_JSON_PARSE_FAILED"
    assert len(result["attempts"]) == 2


def test_extract_v2_uses_stereotype_as_graph_label(monkeypatch):
    response = (
        '{"entities":[{"type":"术语","name":"采购标段数量","properties":{}}],'
        '"relations":[]}'
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: _stream(response))),
        close=lambda: None,
    )
    monkeypatch.setattr(extractor, "_build_openai_client", lambda **_kwargs: client)

    ontology = """@startuml
class 术语 <<Terminology>> {
- 术语标准名称 (TermName): String
}
@enduml"""
    result = extractor.extract_v2(ontology, "采购标段数量", json_retries=0)

    assert result["raw"]["entities"][0]["type"] == "Terminology"
    assert result["graph"]["nodes"][0]["label"] == "Terminology"
    assert result["graph"]["nodes"][0]["uid"] == "terminology::采购标段数量"


def test_extract_v2_discards_relationships_whose_endpoints_are_missing(monkeypatch):
    response = (
        '{"entities":[{"type":"Term","name":"A","properties":{}}],'
        '"relations":[{"from_type":"Term","from_name":"A","rel_type":"RELATED",'
        '"to_type":"Term","to_name":"B"}]}'
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: _stream(response))),
        close=lambda: None,
    )
    monkeypatch.setattr(extractor, "_build_openai_client", lambda **_kwargs: client)

    result = extractor.extract_v2("class Term", "text", json_retries=0)

    assert len(result["graph"]["nodes"]) == 1
    assert result["graph"]["relationships"] == []


def test_extract_v2_keeps_non_empty_entity_name_when_properties_name_is_empty(monkeypatch):
    response = (
        '{"entities":[{"type":"Term","name":"A","properties":{"name":""}}],"relations":[]}'
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: _stream(response))),
        close=lambda: None,
    )
    monkeypatch.setattr(extractor, "_build_openai_client", lambda **_kwargs: client)

    result = extractor.extract_v2("class Term", "text", json_retries=0)

    assert result["graph"]["nodes"][0]["properties"]["name"] == "A"


def test_build_extraction_prompt_uses_v3_base_prompt_and_keeps_output_contract():
    prompt = extractor.build_extraction_prompt("class Term", "A relates to B")

    # 通用规则改为引用 v3 的 BASE_SYSTEM_PROMPT（v5）
    assert "KNOWLEDGE_EXTRACT_V2_BASE_SYSTEM_PROMPT v5" in prompt
    assert "[核心原则]" in prompt
    assert "[关系提取规则]" in prompt
    assert "关系必须符合 ontology 声明的签名" in prompt
    assert "避免推测性关系，不得虚构端点" in prompt
    # V1 的文档画像 / 本体 / JSON 输出结构保持不变
    assert "## 文档画像" in prompt
    assert "## 本体定义（唯一 schema）" in prompt
    assert "JSON 输出结构" in prompt
    assert '"entities"' in prompt and '"relations"' in prompt


def test_schema_normalizes_relationship_endpoint_types():
    ontology = 'class 术语 <<Terminology, KnowledgeUnit>>\nclass 业务域 <<BusinessDomain>>'
    extracted = {
        "entities": [],
        "relations": [{"from_type": "术语", "to_type": "业务域", "rel_type": "BELONGS_TO"}],
    }

    normalized = OntologySchema.parse(ontology).normalize_extraction(extracted)

    assert normalized["relations"][0]["from_type"] == "Terminology"
    assert normalized["relations"][0]["to_type"] == "BusinessDomain"


def test_schema_backfills_entity_name_from_identity_property():
    ontology = """@startuml
class 术语 <<Terminology>> {
- 术语标准名称 (TermName): String
}
' @identity Terminology: TermName
@enduml"""
    normalized = OntologySchema.parse(ontology).normalize_extraction({
        "entities": [{
            "type": "术语",
            "name": "",
            "properties": {"术语标准名称": "采购标段数量"},
        }],
        "relations": [],
    })

    assert normalized["entities"][0]["name"] == "采购标段数量"
    assert normalized["entities"][0]["properties"]["name"] == "采购标段数量"


def test_schema_prefers_standard_plantuml_alias_as_identifier():
    schema = OntologySchema.parse('class "术语" as Terminology <<KnowledgeUnit>>')

    assert schema.classes[0].display_name == "术语"
    assert schema.classes[0].alias == "Terminology"
    assert schema.classes[0].identifier == "Terminology"
    assert schema.classes[0].stereotypes == ("KnowledgeUnit",)


def test_schema_separates_display_names_identifiers_and_secondary_stereotypes():
    ontology = """class 术语 <<Terminology, KnowledgeUnit>> {
- 术语标准名称 (TermName): String
- 同义词 (Synonyms): String[]
}"""
    schema = OntologySchema.parse(ontology)
    ontology_class = schema.classes[0]

    assert ontology_class.display_name == "术语"
    assert ontology_class.identifier == "Terminology"
    assert ontology_class.stereotypes == ("Terminology", "KnowledgeUnit")
    assert ontology_class.properties[0].display_name == "术语标准名称"
    assert ontology_class.properties[0].identifier == "TermName"

    normalized = schema.normalize_extraction({
        "entities": [{
            "type": "术语",
            "name": "采购标段数量",
            "properties": {"术语标准名称": "采购标段数量", "Synonyms": ["标段数量"]},
        }],
        "relations": [],
    })
    assert normalized["entities"][0]["type"] == "Terminology"
    assert normalized["entities"][0]["properties"] == {
        "TermName": "采购标段数量",
        "Synonyms": ["标段数量"],
        "name": "采购标段数量",
    }
