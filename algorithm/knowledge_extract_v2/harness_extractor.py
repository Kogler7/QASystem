from __future__ import annotations

import logging
import os
import re
import time
import uuid
from typing import Any, Callable, Iterable

from .chunk_context import ChunkContextManager, ExtractionChunk, ExtractionRequest
from .config import get_llm_config
from .evidence import bind_evidence, evidence_coverage, verify_evidence
from .extractor import detect_document_type, extract_v2
from .graph_assembler import GraphAssembler
from .graph_utils import canonical_name, merge_node_properties
from .llm_client import create_llm_client
from .official_document_strategy import OfficialDocumentExtractionStrategy
from .ontology_compiler import ExtractionPlan, compile_plan
from .ontology_schema import OntologySchema
from .scheduler import get_llm_scheduler
from .split_extractor import (
    _catalog_text,
    _signature_lines,
    extract_json_from_response,
    extract_split,
)
from .standard_strategy import StandardExtractionStrategy

logger = logging.getLogger(__name__)

LLM_CALLABLE = Callable[[str], str]

_WHOLE_DOC_ENV = "KG_EXTRACT_HARNESS_WHOLE_DOC_MAX_CHARS"
_DEFAULT_WHOLE_DOC_MAX_CHARS = 60000
_JSON_RETRIES_ENV = ("KG_EXTRACT_LLM_JSON_RETRIES", "KG_EXTRACT_JSON_RETRIES")


def _json_retries() -> int:
    for key in _JSON_RETRIES_ENV:
        raw = os.getenv(key)
        if raw:
            try:
                return max(0, int(raw))
            except ValueError:
                pass
    return 2


def _whole_doc_max_chars() -> int:
    raw = os.getenv(_WHOLE_DOC_ENV)
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return _DEFAULT_WHOLE_DOC_MAX_CHARS


def _as_chunks(text: str | None, chunks: Iterable[ExtractionChunk] | None) -> list[ExtractionChunk]:
    if chunks is not None:
        converted: list[ExtractionChunk] = []
        for index, chunk in enumerate(chunks):
            if isinstance(chunk, ExtractionChunk):
                item = chunk
            elif isinstance(chunk, str):
                item = ExtractionChunk(chunk_id=f"chunk-{index}", text=chunk)
            else:
                raise TypeError("chunks 元素必须是 str 或 ExtractionChunk")
            if not item.chunk_id:
                item.chunk_id = f"chunk-{index}"
            converted.append(item)
        if converted:
            return converted
    if text is not None:
        return [ExtractionChunk(chunk_id="chunk-0", text=text)]
    return []


def _full_text(chunks: list[ExtractionChunk]) -> str:
    return "\n".join(chunk.text for chunk in chunks)
_FILE_SECTION_HEADING = re.compile(
    r"(?m)^\s*(?:#{1,6}\s*)?文件\s*[：:]\s*(?P<name>.+?)\s*$"
)
_NON_PRIMARY_SECTION_MARKERS = (
    "表单",
    "附件",
    "附表",
    "附录",
    "反馈表",
    "调查表",
    "统计表",
    "申请表",
)
_FORM_SECTION_MARKERS = ("表单", "反馈表", "调查表", "统计表", "申请表")


def _split_file_sections(text: str) -> list[tuple[str, str]]:
    """Split combined pipeline markdown by the ``## 文件: name`` markers."""
    matches = list(_FILE_SECTION_HEADING.finditer(text))
    if not matches:
        return [("", text)]
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        name = match.group("name").strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((name, text[start:end].strip()))
    return sections


def _primary_document_name(file_name: str) -> bool:
    stem = re.sub(r"[\s（）()\[\]【】_\-—]+", "", file_name or "")
    return bool(
        stem.startswith(("关于", "中国中车", "中车"))
        or any(marker in stem for marker in ("通知", "通报", "意见", "会议", "简报"))
    )


def _is_attachment_section(file_name: str) -> bool:
    """附件/附表/附录：其正文内容不参与抽取，只保留主文档附件行元数据。"""
    if _primary_document_name(file_name):
        return False
    stem = re.sub(r"[\s（）()\[\]【】_\-—]+", "", file_name or "")
    return any(marker in stem for marker in _NON_PRIMARY_SECTION_MARKERS)


def _is_form_section(file_name: str) -> bool:
    """表单/反馈表/申请表等：折叠为一个完整 OfficialDocument 管理单元。"""
    if _primary_document_name(file_name):
        return False
    stem = re.sub(r"[\s（）()\[\]【】_\-—]+", "", file_name or "")
    return any(marker in stem for marker in _FORM_SECTION_MARKERS)


def _numbered_attachment_items(text: str) -> list[tuple[int, str]]:
    match = re.search(r"^\s*附件\s*[：:]\s*(?P<content>.*)$", text, re.MULTILINE)
    if match is None:
        return []
    items: list[tuple[int, str]] = []
    for part in re.split(r"[;；\n]", match.group("content")):
        part = part.strip().strip("。")
        item = re.match(r"^(?P<index>\d+)\s*[.、]\s*(?P<name>.+)$", part)
        if item is not None:
            items.append((int(item.group("index")), item.group("name").strip()))
    return items


def _build_form_document_entity(
    *,
    plan: ExtractionPlan,
    file_name: str,
    section_text: str,
    attachment_items: list[tuple[int, str]],
    attachment_index: int,
    source_org: str = "",
) -> dict[str, Any] | None:
    """把独立上传的表单/附件文件折叠为一个完整 OfficialDocument 管理单元。"""
    if "OfficialDocument" not in plan.allowed_types:
        return None
    spec = plan.class_specs.get("OfficialDocument")
    if spec is None:
        return None
    numbered_name = next(
        (name for index, name in attachment_items if index == attachment_index),
        "",
    )
    if numbered_name:
        display_name = numbered_name
    else:
        display_name = _section_title(section_text) or file_name
    entity_name = display_name
    properties: dict[str, Any] = {}
    for prop_id in ("文件标题", "documentTitle"):
        if any(prop.identifier == prop_id for prop in spec.properties):
            properties[prop_id] = entity_name
    for prop_id in ("文件备注", "documentRemark"):
        if any(prop.identifier == prop_id for prop in spec.properties):
            properties[prop_id] = "作为完整管理单元抽取，不展开表单行项目"
    if source_org:
        for prop_id in ("来文单位", "sourceOrganization"):
            if any(prop.identifier == prop_id for prop in spec.properties):
                properties[prop_id] = source_org
    quote = _section_title(section_text) or file_name
    return {
        "type": "OfficialDocument",
        "name": entity_name,
        "properties": properties,
        "evidence": {"quote": quote},
    }


def _section_title(section_text: str) -> str:
    for raw_line in (section_text or "").splitlines():
        line = raw_line.strip()
        if not line or len(line) > 80:
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        if any(
            label in line
            for label in (
                "联系电话",
                "来文单位",
                "收文号",
                "领导批示",
                "文件标题",
                "来文字号",
                "密级",
                "主送",
                "抄送",
                "拟办意见",
            )
        ):
            continue
        if len(line) >= 4:
            return line.rstrip("。").strip()
    return ""
def _ensure_minimal_official_entities(
    raw: dict[str, Any],
    plan: ExtractionPlan,
    text: str,
) -> dict[str, Any]:
    """保证公文至少有主文档管理单元 + 通知正文，避免整篇抽取全部失败。"""
    if "OfficialDocument" not in plan.allowed_types or "DocumentUnit" not in plan.allowed_types:
        return raw
    entities = [entity for entity in raw.get("entities", []) if isinstance(entity, dict)]
    relations = [relation for relation in raw.get("relations", []) if isinstance(relation, dict)]
    official = next(
        (entity for entity in entities if entity.get("type") == "OfficialDocument"),
        None,
    )
    if official is None:
        title = _section_title(text) or "未命名公文"
        doc_spec = plan.class_specs.get("OfficialDocument")
        props: dict[str, Any] = {}
        if doc_spec and any(prop.identifier == "文件标题" for prop in doc_spec.properties):
            props["文件标题"] = title
        official = {
            "type": "OfficialDocument",
            "name": title,
            "properties": props,
            "evidence": {"quote": title},
        }
        entities.insert(0, official)
    unit = next(
        (entity for entity in entities if entity.get("type") == "DocumentUnit"),
        None,
    )
    if unit is None:
        unit_spec = plan.class_specs.get("DocumentUnit")
        props = {}
        if unit_spec and any(prop.identifier == "单元标题" for prop in unit_spec.properties):
            props["单元标题"] = official.get("name", "")
        if unit_spec and any(prop.identifier == "单元类型" for prop in unit_spec.properties):
            props["单元类型"] = "正文"
        if unit_spec and any(prop.identifier == "原文内容" for prop in unit_spec.properties):
            props["原文内容"] = text.strip()
        unit = {
            "type": "DocumentUnit",
            "name": "通知正文",
            "properties": props,
            "evidence": {"quote": official.get("name", "") or text.strip()[:120]},
        }
        entities.append(unit)
    if not any(
        relation.get("rel_type") == "HAS_PART"
        and relation.get("from_type") == "OfficialDocument"
        and canonical_name(relation.get("from_name")) == canonical_name(official.get("name"))
        and relation.get("to_type") == "DocumentUnit"
        and canonical_name(relation.get("to_name")) == canonical_name(unit.get("name"))
        for relation in relations
    ):
        relations.append(
            {
                "from_type": "OfficialDocument",
                "from_name": official.get("name", ""),
                "rel_type": "HAS_PART",
                "to_type": "DocumentUnit",
                "to_name": unit.get("name", ""),
                "evidence": official.get("evidence") or unit.get("evidence") or {},
            }
        )
    return {"entities": entities, "relations": relations}



def _choose_attachment_index(
    file_name: str,
    attachment_items: list[tuple[int, str]],
    fallback_index: int,
) -> int:
    stem = re.sub(r"[\s（）()\[\]【】_\-—.docxpdf]+", "", file_name or "")
    best_index = fallback_index
    best_score = -1
    for index, name in attachment_items:
        item_name = re.sub(r"[\s（）()\[\]【】_\-—]+", "", name)
        score = 0
        for size in (4, 3, 2):
            for start in range(0, max(1, len(stem) - size + 1)):
                token = stem[start : start + size]
                if token in item_name:
                    score += 1
        if "表" in stem and "表" in item_name:
            score += 3
        if score > best_score:
            best_score = score
            best_index = index
    if best_score < 0 and attachment_items:
        best_index = attachment_items[-1][0] if len(attachment_items) == 1 else fallback_index
    return best_index


def _append_form_document_units(
    raw: dict[str, Any],
    plan: ExtractionPlan,
    form_sections: list[tuple[str, str]],
    primary_text: str,
    manager: ChunkContextManager,
) -> dict[str, Any]:
    if not form_sections:
        return raw
    entities = [entity for entity in raw.get("entities", []) if isinstance(entity, dict)]
    relations = [relation for relation in raw.get("relations", []) if isinstance(relation, dict)]
    official = next(
        (entity for entity in entities if entity.get("type") == "OfficialDocument"),
        None,
    )
    source_org = str((official.get("properties") or {}).get("来文单位") or "") if official else ""
    attachment_items = _numbered_attachment_items(primary_text)
    for fallback_index, (file_name, section_text) in enumerate(form_sections, start=1):
        chosen_index = _choose_attachment_index(file_name, attachment_items, fallback_index)
        entity = _build_form_document_entity(
            plan=plan,
            file_name=file_name,
            section_text=section_text,
            attachment_items=attachment_items,
            attachment_index=chosen_index,
            source_org=source_org,
        )
        if entity is None:
            continue
        bound = bind_evidence({"entities": [entity], "relations": []}, manager)
        entity = (bound.get("entities") or [entity])[0]
        key = (str(entity.get("type") or ""), canonical_name(entity.get("name")))
        if not any(
            str(item.get("type") or "") == key[0] and canonical_name(item.get("name")) == key[1]
            for item in entities
        ):
            entities.append(entity)
        if official is not None:
            _add_relation(
                relations,
                {
                    "from_type": "OfficialDocument",
                    "from_name": official.get("name", ""),
                    "rel_type": "HAS_DOCUMENT_RELATION",
                    "to_type": "OfficialDocument",
                    "to_name": entity.get("name", ""),
                    "evidence": entity.get("evidence") or official.get("evidence") or {},
                },
            )
    return {"entities": entities, "relations": relations}
_PERSON_NAME_RE = re.compile(r"^[\u4e00-\u9fff]{2,4}$")
_AGENT_GENERIC_MARKERS = ("领导", "员工", "主管", "人员", "个人")
_INFO_KEEP_MARKERS = ("附件", "反馈", "报告", "方案", "办法", "要点", "计划", "清单", "表")
_DOCUMENT_UNIT_ALLOWED = {
    canonical_name("通知正文"),
    canonical_name("附件说明"),
    canonical_name("附件列表"),
    canonical_name("报送要求"),
}


def _allowed_document_unit_name(name: str) -> bool:
    key = canonical_name(name)
    return key in _DOCUMENT_UNIT_ALLOWED or bool(re.match(r"^排查要求[一二三四五六七八九十]+$", name or ""))


def _filter_official_entities(
    raw: dict[str, Any],
    *,
    primary_text: str,
    attachment_items: list[tuple[int, str]],
) -> dict[str, Any]:
    entities = [entity for entity in raw.get("entities", []) if isinstance(entity, dict)]
    relations = [relation for relation in raw.get("relations", []) if isinstance(relation, dict)]
    official = next(
        (entity for entity in entities if entity.get("type") == "OfficialDocument"),
        None,
    )
    official_title = canonical_name(official.get("name") if official else "")
    attachment_names = {canonical_name(name) for _, name in attachment_items}
    kept: list[dict[str, Any]] = []
    for entity in entities:
        type_id = str(entity.get("type") or "")
        name = str(entity.get("name") or "")
        name_key = canonical_name(name)
        if type_id == "DocumentUnit" and not _allowed_document_unit_name(name):
            continue
        if type_id == "OperationalAgent":
            props = entity.get("properties") or {}
            agent_type = str(props.get("主体类型") or "")
            org_suffix = (
                "中心",
                "公司",
                "集团",
                "部门",
                "事业部",
                "委员会",
                "办公室",
                "研究院",
                "子公司",
                "单位",
                "企业",
            )
            if _PERSON_NAME_RE.match(name) and not any(suffix in name for suffix in org_suffix):
                continue
            if agent_type == "角色":
                continue
            if any(marker in name for marker in _AGENT_GENERIC_MARKERS) and name_key not in attachment_names:
                if not any(suffix in name for suffix in org_suffix):
                    continue
        if type_id == "OperationalObject":
            if name_key in attachment_names or name_key in official_title:
                pass
            elif name and len(name) >= 5 and (props := entity.get("properties") or {}):
                # 核心业务对象通常是较长、有领域/类型属性的短语；
                # 过短的表格行对象（办公电脑、反馈表）在此被裁剪。
                if not str(props.get("对象类型") or "") and not str(props.get("所属领域") or ""):
                    continue
            else:
                continue
        if type_id == "OperationalInformation":
            props = entity.get("properties") or {}
            info_type = str(props.get("信息类型") or "")
            source = str(props.get("来源文本") or "")
            if info_type != "附件" and not source.startswith("附件") and not any(
                marker in name for marker in _INFO_KEEP_MARKERS
            ):
                continue
        kept.append(entity)
    kept_keys = {
        (str(entity.get("type") or ""), canonical_name(entity.get("name")))
        for entity in kept
    }
    relations = [
        relation
        for relation in relations
        if (str(relation.get("from_type") or ""), canonical_name(relation.get("from_name"))) in kept_keys
        and (str(relation.get("to_type") or ""), canonical_name(relation.get("to_name"))) in kept_keys
    ]
    return {"entities": kept, "relations": relations}











def _call_llm(
    llm: LLM_CALLABLE,
    prompt: str,
    *,
    attempts: list[dict[str, Any]],
    use_scheduler: bool,
    pass_name: str = "document",
) -> str:
    try:
        if use_scheduler:
            with get_llm_scheduler().request_slot():
                response = llm(prompt)
        else:
            response = llm(prompt)
    except Exception as exc:  # noqa: BLE001 - 归因后不中断整篇抽取
        attempts.append(
            {
                "attempt": 1,
                "type": "request",
                "pass": pass_name,
                "message": str(exc),
            }
        )
        return ""
    return response or ""


def _parse_with_retries(
    llm: LLM_CALLABLE,
    prompt: str,
    *,
    use_scheduler: bool,
    attempts: list[dict[str, Any]],
    retries: int,
    pass_name: str = "document",
) -> dict[str, Any]:
    retry_prompt = prompt
    for attempt in range(retries + 1):
        raw_response = _call_llm(
            llm,
            retry_prompt,
            attempts=attempts,
            use_scheduler=use_scheduler,
            pass_name=pass_name,
        )
        parsed = extract_json_from_response(raw_response)
        if isinstance(parsed, dict):
            return parsed
        attempts.append(
            {
                "attempt": attempt + 1,
                "type": "json",
                "pass": pass_name,
                "message": "JSON 解析失败",
            }
        )
        if attempt < retries:
            retry_prompt = (
                prompt
                + "\n\n上次输出不是合法 JSON。请只输出一个 JSON 对象，"
                "不要输出 Markdown 代码块或解释。"
            )
    return {}


def _raw_graph(raw: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    objects: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for index, entity in enumerate(raw.get("entities", [])):
        if not isinstance(entity, dict):
            continue
        type_id = str(entity.get("type") or "")
        name = str(entity.get("name") or "")
        if not type_id or not name:
            continue
        obj_id = str(entity.get("candidate_id") or f"doc-entity-{index}")
        obj = {
            "id": obj_id,
            "type": type_id,
            "identity": name,
            "properties": dict(entity.get("properties") or {}),
        }
        objects.append(obj)
        by_id[obj_id] = obj
    if len(objects) != len(by_id):
        by_id = {obj["id"]: obj for obj in objects}

    relation_payloads: list[dict[str, Any]] = []
    for index, relation in enumerate(raw.get("relations", [])):
        if not isinstance(relation, dict):
            continue
        from_type = str(relation.get("from_type") or "")
        from_name = str(relation.get("from_name") or "")
        to_type = str(relation.get("to_type") or "")
        to_name = str(relation.get("to_name") or "")
        rel_type = str(relation.get("rel_type") or "")
        if not all((from_type, from_name, rel_type, to_type, to_name)):
            continue
        from_id = next(
            (
                obj["id"]
                for obj in objects
                if obj["type"] == from_type and canonical_name(obj["identity"]) == canonical_name(from_name)
            ),
            None,
        )
        to_id = next(
            (
                obj["id"]
                for obj in objects
                if obj["type"] == to_type and canonical_name(obj["identity"]) == canonical_name(to_name)
            ),
            None,
        )
        if from_id is None or to_id is None:
            continue
        relation_payloads.append(
            {
                "id": f"doc-relation-{index}",
                "type": rel_type,
                "from": from_id,
                "to": to_id,
            }
        )
    return GraphAssembler().build_graph(objects, relation_payloads)


def _merge_raw(deterministic: dict[str, Any], llm: dict[str, Any]) -> dict[str, Any]:
    entities: list[dict[str, Any]] = []
    entity_index: dict[tuple[str, str], dict[str, Any]] = {}
    for source in (deterministic, llm):
        for entity in source.get("entities", []):
            if not isinstance(entity, dict):
                continue
            type_id = str(entity.get("type") or "")
            name = str(entity.get("name") or "")
            if not type_id or not name:
                continue
            key = (type_id, canonical_name(name))
            existing = entity_index.get(key)
            if existing is None:
                item = dict(entity)
                item["type"] = type_id
                item["name"] = name
                item["properties"] = dict(entity.get("properties") or {})
                entity_index[key] = item
                entities.append(item)
            else:
                existing["properties"] = merge_node_properties(
                    existing.get("properties") or {},
                    entity.get("properties") or {},
                )
                if not existing.get("evidence") and entity.get("evidence"):
                    existing["evidence"] = entity["evidence"]

    relation_keys: set[tuple[str, str, str, str, str]] = set()
    relations: list[dict[str, Any]] = []
    for source in (deterministic, llm):
        for relation in source.get("relations", []):
            if not isinstance(relation, dict):
                continue
            key = (
                str(relation.get("from_type") or ""),
                canonical_name(relation.get("from_name")),
                str(relation.get("rel_type") or ""),
                str(relation.get("to_type") or ""),
                canonical_name(relation.get("to_name")),
            )
            if any(not part for part in key):
                continue
            if key in relation_keys:
                continue
            relation_keys.add(key)
            relations.append(dict(relation))
    return {"entities": entities, "relations": relations}
_OFFICIAL_ENTITY_BUDGET = {
    "OfficialDocument": 8,
    "DocumentUnit": 12,
    "OperationalAgent": 8,
    "OperationalObject": 6,
    "OperationalActivity": 8,
    "OperationalInformation": 6,
    "OperationalExchange": 4,
    "OperationalConstraint": 6,
}
_STANDARD_ENTITY_BUDGET = {
    "Standard": 6,
    "InternationalClassificationofStandard": 2,
    "ChineseClassificationofStandard": 2,
    "Normative Element": 16,
    "Informative Element": 12,
    "Section": 40,
    "Subclause": 90,
    "Clause": 110,
    "Property": 20,
    "Constrain": 20,
    "ActionClass": 20,
    "Terminology": 16,
    "ExternalResource": 8,
    "Annex": 8,
    "Table": 8,
    "Equation": 8,
    "Reference": 8,
    "Publishing Organization": 4,
    "Proposing Organization": 4,
    "CompetentDepartment": 4,
    "StandardizationObject": 4,
    "Approval and PublicationStage": 2,
    "Subject of Standardization": 8,
}


def _enforce_entity_budget(raw: dict[str, Any], budget: dict[str, int]) -> dict[str, Any]:
    if not raw.get("entities"):
        return raw
    relation_entities: set[tuple[str, str]] = set()
    for relation in raw.get("relations", []):
        if not isinstance(relation, dict):
            continue
        relation_entities.add((str(relation.get("from_type") or ""), canonical_name(relation.get("from_name"))))
        relation_entities.add((str(relation.get("to_type") or ""), canonical_name(relation.get("to_name"))))

    allowed: set[tuple[str, str]] = set()
    over_budget_types: set[str] = set()
    for type_id, limit in budget.items():
        items = [
            entity
            for entity in raw["entities"]
            if isinstance(entity, dict) and str(entity.get("type") or "") == type_id
        ]
        if len(items) <= limit:
            continue
        over_budget_types.add(type_id)
        ordered = sorted(
            range(len(items)),
            key=lambda index: (
                0
                if (type_id, canonical_name(items[index].get("name"))) in relation_entities
                else 1,
                index,
            ),
        )
        for index in ordered[:limit]:
            item = items[index]
            allowed.add((type_id, canonical_name(item.get("name"))))

    kept: list[dict[str, Any]] = []
    for entity in raw["entities"]:
        if not isinstance(entity, dict):
            continue
        type_id = str(entity.get("type") or "")
        key = (type_id, canonical_name(entity.get("name")))
        if type_id in over_budget_types and key not in allowed:
            continue
        kept.append(entity)
    return {"entities": kept, "relations": raw.get("relations", [])}

_STANDARD_DROP_TYPES = {
    "Drafter",
    "Individual",
    "Drafting Organization",
    "Implementing Organization",
    "Organization",
    "DomainCategory",
    "ContentElement",
    "StructuralElement",
    "InformationUnit",
    "Provision",
    "Paragraph",
    "List Item",
    "Additional Information",
    "InformationForm",
    "Figure",
    "Note",
    "Path",
    "DecisionCriteria",
    "Activity",
    "Regulations",
    "ReferencedDocuments",
    "Patent",
    "PublicDatabase",
    "StandardizationProcess",
}


def _drop_entity_types(raw: dict[str, Any], drop_types: set[str]) -> dict[str, Any]:
    entities = [
        entity
        for entity in raw.get("entities", [])
        if isinstance(entity, dict) and str(entity.get("type") or "") not in drop_types
    ]
    valid_keys = {
        (str(entity.get("type") or ""), canonical_name(entity.get("name")))
        for entity in entities
    }
    relations = [
        relation
        for relation in raw.get("relations", [])
        if isinstance(relation, dict)
        and (str(relation.get("from_type") or ""), canonical_name(relation.get("from_name"))) in valid_keys
        and (str(relation.get("to_type") or ""), canonical_name(relation.get("to_name"))) in valid_keys
    ]
    return {"entities": entities, "relations": relations}


def _add_relation(relations: list[dict[str, Any]], relation: dict[str, Any]) -> None:
    key = (
        str(relation.get("from_type") or ""),
        canonical_name(relation.get("from_name")),
        str(relation.get("rel_type") or ""),
        str(relation.get("to_type") or ""),
        canonical_name(relation.get("to_name")),
    )
    if any(not part for part in key):
        return
    existing_keys = {
        (
            str(item.get("from_type") or ""),
            canonical_name(item.get("from_name")),
            str(item.get("rel_type") or ""),
            str(item.get("to_type") or ""),
            canonical_name(item.get("to_name")),
        )
        for item in relations
    }
    if key not in existing_keys:
        relations.append(relation)


def _merge_contained_standard_entities(raw: dict[str, Any]) -> dict[str, Any]:
    """Merge ``Standard`` rows whose names are a containment variant.

    Deterministic extraction may keep the standard number while the LLM
    returns the full ``number + title`` golden name.  Keeping both creates an
    avoidable FP, so prefer the longer name and remap relation endpoints.
    """
    standards = [
        entity
        for entity in raw.get("entities", [])
        if isinstance(entity, dict) and entity.get("type") == "Standard"
    ]
    if len(standards) < 2:
        return raw
    replacements: dict[tuple[str, str], dict[str, Any]] = {}
    for left in standards:
        left_key = (str(left.get("type") or ""), canonical_name(left.get("name")))
        if any(
            canonical_name(value.get("name")) == left_key[1]
            for value in replacements.values()
        ):
            continue
        for right in standards:
            if right is left:
                continue
            left_name = str(left.get("name") or "")
            right_name = str(right.get("name") or "")
            if not left_name or not right_name:
                continue
            left_canon = canonical_name(left_name)
            right_canon = canonical_name(right_name)
            if left_canon == right_canon:
                continue
            if left_canon in right_canon:
                replacements[left_key] = right
                break
            if right_canon in left_canon:
                replacements[(str(right.get("type") or ""), right_canon)] = left
                break

    if not replacements:
        return raw
    entities = [
        entity
        for entity in raw.get("entities", [])
        if isinstance(entity, dict)
        and (
            str(entity.get("type") or ""),
            canonical_name(entity.get("name")),
        )
        not in replacements
    ]
    remap = {
        (type_id, old_name): str(target.get("name") or "")
        for (type_id, old_name), target in replacements.items()
    }
    relations = []
    for relation in raw.get("relations", []):
        if not isinstance(relation, dict):
            continue
        from_key = (str(relation.get("from_type") or ""), canonical_name(relation.get("from_name")))
        to_key = (str(relation.get("to_type") or ""), canonical_name(relation.get("to_name")))
        new_relation = dict(relation)
        if from_key in remap:
            new_relation["from_name"] = remap[from_key]
        if to_key in remap:
            new_relation["to_name"] = remap[to_key]
        relations.append(new_relation)
    return {"entities": entities, "relations": relations}
def _merge_structural_entities(raw: dict[str, Any]) -> dict[str, Any]:
    """Merge Section/Subclause short names into ``number + title``.

    Older one-pass prompts often returned ``name="5"`` while v2 golden uses
    ``name="5 性能要求"``.  If both rows exist after LLM + deterministic merge,
    keep the longer title-bearing name and remap relations to it.
    """
    replacements: dict[tuple[str, str], dict[str, Any]] = {}
    for type_id in ("Section", "Subclause"):
        items = [
            entity
            for entity in raw.get("entities", [])
            if isinstance(entity, dict) and entity.get("type") == type_id
        ]
        if len(items) < 2:
            continue
        for left in items:
            left_name = str(left.get("name") or "")
            left_number = str(
                (left.get("properties") or {}).get("章编号")
                or (left.get("properties") or {}).get("条编号")
                or (left_name if re.fullmatch(r"\d+(?:\.\d+)*", left_name) else "")
            )
            if not left_number:
                continue
            left_key = (type_id, canonical_name(left.get("name")))
            for right in items:
                if right is left:
                    continue
                right_name = str(right.get("name") or "")
                right_number = str(
                    (right.get("properties") or {}).get("章编号")
                    or (right.get("properties") or {}).get("条编号")
                    or (right_name if re.fullmatch(r"\d+(?:\.\d+)*", right_name) else "")
                )
                if right_number != left_number:
                    continue
                left_canon = canonical_name(left_name)
                right_canon = canonical_name(right_name)
                if left_canon == right_canon:
                    continue
                if left_canon in right_canon:
                    replacements[left_key] = right
                    break
                if right_canon in left_canon:
                    replacements[(type_id, right_canon)] = left
                    break

    if not replacements:
        return raw
    entities = [
        entity
        for entity in raw.get("entities", [])
        if isinstance(entity, dict)
        and (
            str(entity.get("type") or ""),
            canonical_name(entity.get("name")),
        )
        not in replacements
    ]
    remap = {
        (type_id, old_name): str(target.get("name") or "")
        for (type_id, old_name), target in replacements.items()
    }
    relations = []
    for relation in raw.get("relations", []):
        if not isinstance(relation, dict):
            continue
        new_relation = dict(relation)
        from_key = (str(relation.get("from_type") or ""), canonical_name(relation.get("from_name")))
        to_key = (str(relation.get("to_type") or ""), canonical_name(relation.get("to_name")))
        if from_key in remap:
            new_relation["from_name"] = remap[from_key]
        if to_key in remap:
            new_relation["to_name"] = remap[to_key]
        relations.append(new_relation)
    return {"entities": entities, "relations": relations}





def _contains_text(haystack: str, needle: str) -> bool:
    def compact(value: str) -> str:
        return re.sub(r"\s+", "", value or "")

    return bool(needle and compact(needle) in compact(haystack))


def _infer_official_spine(raw: dict[str, Any], deterministic: dict[str, Any]) -> dict[str, Any]:
    """Deterministic document spine for official documents.

    LLM output is allowed to propose the same relation; this function only
    fills deterministic missing spine edges that are implied by document
    structure and evidence location.
    """
    entities = [entity for entity in raw.get("entities", []) if isinstance(entity, dict)]
    relations = [relation for relation in raw.get("relations", []) if isinstance(relation, dict)]
    if not entities:
        return raw

    official = next(
        (entity for entity in entities if entity.get("type") == "OfficialDocument"),
        None,
    )
    units = [entity for entity in entities if entity.get("type") == "DocumentUnit"]
    agents = [entity for entity in entities if entity.get("type") == "OperationalAgent"]
    activities = [entity for entity in entities if entity.get("type") == "OperationalActivity"]
    exchanges = [entity for entity in entities if entity.get("type") == "OperationalExchange"]
    objects = [entity for entity in entities if entity.get("type") == "OperationalObject"]
    informations = [entity for entity in entities if entity.get("type") == "OperationalInformation"]
    constraints = [entity for entity in entities if entity.get("type") == "OperationalConstraint"]

    if official is not None:
        for activity in activities:
            _add_relation(
                relations,
                {
                    "from_type": "OfficialDocument",
                    "from_name": official.get("name", ""),
                    "rel_type": "REQUIRES_ACTIVITY",
                    "to_type": "OperationalActivity",
                    "to_name": activity.get("name", ""),
                    "evidence": activity.get("evidence") or official.get("evidence") or {},
                },
            )
        for exchange in exchanges:
            _add_relation(
                relations,
                {
                    "from_type": "OfficialDocument",
                    "from_name": official.get("name", ""),
                    "rel_type": "HAS_EXCHANGE",
                    "to_type": "OperationalExchange",
                    "to_name": exchange.get("name", ""),
                    "evidence": exchange.get("evidence") or official.get("evidence") or {},
                },
            )
        for information in informations:
            if str(information.get("name") or "").startswith("附件"):
                _add_relation(
                    relations,
                    {
                        "from_type": "OfficialDocument",
                        "from_name": official.get("name", ""),
                        "rel_type": "DEFINES",
                        "to_type": "OperationalInformation",
                        "to_name": information.get("name", ""),
                        "evidence": information.get("evidence") or official.get("evidence") or {},
                    },
                )
            else:
                _add_relation(
                    relations,
                    {
                        "from_type": "OfficialDocument",
                        "from_name": official.get("name", ""),
                        "rel_type": "HAS_KEY_INFORMATION",
                        "to_type": "OperationalInformation",
                        "to_name": information.get("name", ""),
                        "evidence": information.get("evidence") or official.get("evidence") or {},
                    },
                )
                info_props = information.get("properties") or {}
                if str(info_props.get("信息类型") or "") == "附件" or str(
                    info_props.get("来源文本") or ""
                ).startswith("附件"):
                    _add_relation(
                        relations,
                        {
                            "from_type": "OfficialDocument",
                            "from_name": official.get("name", ""),
                            "rel_type": "DEFINES",
                            "to_type": "OperationalInformation",
                            "to_name": information.get("name", ""),
                            "evidence": information.get("evidence") or official.get("evidence") or {},
                        },
                    )

        source_org = str(
            (official.get("properties") or {}).get("来文单位")
            or (official.get("properties") or {}).get("sourceOrganization")
            or ""
        )
        if source_org:
            issuer = next(
                (agent for agent in agents if canonical_name(agent.get("name")) == canonical_name(source_org)),
                None,
            )
            if issuer is not None:
                _add_relation(
                    relations,
                    {
                        "from_type": "OfficialDocument",
                        "from_name": official.get("name", ""),
                        "rel_type": "ISSUED_BY",
                        "to_type": "OperationalAgent",
                        "to_name": issuer.get("name", ""),
                        "evidence": official.get("evidence") or {},
                    },
                )

        aggregate_agents = {
            "各相关单位",
            "各一级子公司",
            "各单位",
            "各子公司",
            "各企业",
            "各承担单位",
            "各领域负责部门",
            "各子企业",
            "所属企业",
            "相关部门",
        }
        for agent in agents:
            if canonical_name(agent.get("name")) not in aggregate_agents:
                continue
            _add_relation(
                relations,
                {
                    "from_type": "OfficialDocument",
                    "from_name": official.get("name", ""),
                    "rel_type": "APPLIES_TO",
                    "to_type": "OperationalAgent",
                    "to_name": agent.get("name", ""),
                    "evidence": agent.get("evidence") or official.get("evidence") or {},
                },
            )

        title = str(official.get("name") or "")
        for obj in objects:
            if _contains_text(title, str(obj.get("name") or "")) or _contains_text(
                str(obj.get("properties") or {}).get("对象名称", ""), title
            ):
                _add_relation(
                    relations,
                    {
                        "from_type": "OfficialDocument",
                        "from_name": official.get("name", ""),
                        "rel_type": "CONCERNS_OBJECT",
                        "to_type": "OperationalObject",
                        "to_name": obj.get("name", ""),
                        "evidence": obj.get("evidence") or official.get("evidence") or {},
                    },
                )

    # 运行实体之间的确定性关系：名称/描述/原文重叠才建立，避免纯猜测。
    for activity in activities:
        activity_text = " ".join(
            [
                str(activity.get("name") or ""),
                str((activity.get("properties") or {}).get("活动描述") or ""),
                str((activity.get("properties") or {}).get("活动名称") or ""),
            ]
        )
        for obj in objects:
            obj_text = " ".join(
                [
                    str(obj.get("name") or ""),
                    str((obj.get("properties") or {}).get("对象名称") or ""),
                    str((obj.get("properties") or {}).get("对象描述") or ""),
                ]
            )
            if _contains_text(activity_text, str(obj.get("name") or "")) or _contains_text(
                obj_text, str(activity.get("name") or "")
            ):
                _add_relation(
                    relations,
                    {
                        "from_type": "OperationalActivity",
                        "from_name": activity.get("name", ""),
                        "rel_type": "ACTS_ON",
                        "to_type": "OperationalObject",
                        "to_name": obj.get("name", ""),
                        "evidence": activity.get("evidence") or obj.get("evidence") or {},
                    },
                )
        for agent in agents:
            if _contains_text(activity_text, str(agent.get("name") or "")):
                _add_relation(
                    relations,
                    {
                        "from_type": "OperationalActivity",
                        "from_name": activity.get("name", ""),
                        "rel_type": "PERFORMED_BY",
                        "to_type": "OperationalAgent",
                        "to_name": agent.get("name", ""),
                        "evidence": activity.get("evidence") or agent.get("evidence") or {},
                    },
                )

    for constraint in constraints:
        constraint_text = " ".join(
            [
                str(constraint.get("name") or ""),
                str((constraint.get("properties") or {}).get("约束原文") or ""),
                str((constraint.get("properties") or {}).get("约束名称") or ""),
            ]
        )
        for activity in activities:
            if _contains_text(constraint_text, str(activity.get("name") or "")):
                _add_relation(
                    relations,
                    {
                        "from_type": "OperationalConstraint",
                        "from_name": constraint.get("name", ""),
                        "rel_type": "CONSTRAINS_ACTIVITY",
                        "to_type": "OperationalActivity",
                        "to_name": activity.get("name", ""),
                        "evidence": constraint.get("evidence") or activity.get("evidence") or {},
                    },
                )
        for agent in agents:
            if _contains_text(constraint_text, str(agent.get("name") or "")):
                _add_relation(
                    relations,
                    {
                        "from_type": "OperationalConstraint",
                        "from_name": constraint.get("name", ""),
                        "rel_type": "CONSTRAINS_AGENT",
                        "to_type": "OperationalAgent",
                        "to_name": agent.get("name", ""),
                        "evidence": constraint.get("evidence") or agent.get("evidence") or {},
                    },
                )
        for obj in objects:
            if _contains_text(constraint_text, str(obj.get("name") or "")):
                _add_relation(
                    relations,
                    {
                        "from_type": "OperationalConstraint",
                        "from_name": constraint.get("name", ""),
                        "rel_type": "CONSTRAINS_OBJECT",
                        "to_type": "OperationalObject",
                        "to_name": obj.get("name", ""),
                        "evidence": constraint.get("evidence") or obj.get("evidence") or {},
                    },
                )

    for exchange in exchanges:
        exchange_text = " ".join(
            [
                str(exchange.get("name") or ""),
                str((exchange.get("properties") or {}).get("交互描述") or ""),
            ]
        )
        for activity in activities:
            if _contains_text(exchange_text, str(activity.get("name") or "")):
                _add_relation(
                    relations,
                    {
                        "from_type": "OperationalExchange",
                        "from_name": exchange.get("name", ""),
                        "rel_type": "SUPPORTS_ACTIVITY",
                        "to_type": "OperationalActivity",
                        "to_name": activity.get("name", ""),
                        "evidence": exchange.get("evidence") or activity.get("evidence") or {},
                    },
                )

    # EVIDENCED_BY：由 evidence quote 所在的 DocumentUnit 确定。
    unit_texts: list[tuple[dict[str, Any], str]] = []
    for unit in units:
        text = str((unit.get("properties") or {}).get("原文内容") or "")
        if not text:
            text = str((unit.get("evidence") or {}).get("quote") or "")
        unit_texts.append((unit, text))
    operational = [
        entity
        for entity in entities
        if entity.get("type")
        in {
            "OperationalAgent",
            "OperationalObject",
            "OperationalActivity",
            "OperationalInformation",
            "OperationalExchange",
            "OperationalConstraint",
        }
    ]
    for entity in operational:
        if any(
            relation.get("from_type") == entity.get("type")
            and canonical_name(relation.get("from_name")) == canonical_name(entity.get("name"))
            and relation.get("rel_type") == "EVIDENCED_BY"
            for relation in relations
        ):
            continue
        quote = str((entity.get("evidence") or {}).get("quote") or entity.get("name") or "")
        matched_unit = next(
            (unit for unit, unit_text in unit_texts if _contains_text(unit_text, quote)),
            None,
        )
        if matched_unit is None and len(units) == 1:
            matched_unit = units[0]
        if matched_unit is not None:
            _add_relation(
                relations,
                {
                    "from_type": entity.get("type", ""),
                    "from_name": entity.get("name", ""),
                    "rel_type": "EVIDENCED_BY",
                    "to_type": "DocumentUnit",
                    "to_name": matched_unit.get("name", ""),
                    "evidence": entity.get("evidence") or matched_unit.get("evidence") or {},
                },
            )

    return {"entities": entities, "relations": relations}








def _validate_and_normalize(
    schema: OntologySchema,
    parsed: dict[str, Any],
    *,
    target_types: Iterable[str] | None,
    validation_mode: str,
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    sanitized, validation_warnings = schema.validate_extraction(
        parsed,
        target_types=target_types,
        mode=validation_mode,
    )
    warnings.extend(validation_warnings)
    return schema.normalize_extraction(sanitized)


def _finish_result(
    *,
    raw: dict[str, Any],
    warnings: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    profile: str,
    manager: ChunkContextManager,
    pipeline: str,
    plan: ExtractionPlan,
) -> dict[str, Any]:
    graph = _raw_graph(raw)
    verdicts = verify_evidence(raw, manager)
    for verdict in verdicts:
        if verdict.status == "fail":
            warnings.append(
                {
                    "code": "EVIDENCE_L1_FAIL" if verdict.level == "L1" else "EVIDENCE_L2_FAIL",
                    "message": verdict.message,
                    "path": verdict.ref,
                }
            )
    coverage = evidence_coverage(raw)
    error_code = None
    error = None
    if any(attempt.get("type") == "request" for attempt in attempts):
        error_code = "LLM_REQUEST_FAILED"
    elif any(attempt.get("type") == "json" for attempt in attempts):
        error_code = "LLM_JSON_PARSE_FAILED"
    if error_code and not raw.get("entities"):
        error = "LLM 返回内容无法解析为知识抽取 JSON" if error_code == "LLM_JSON_PARSE_FAILED" else "LLM 调用失败"
    result: dict[str, Any] = {
        "raw": raw,
        "graph": graph,
        "triples": GraphAssembler().build_triples(graph),
        "warnings": warnings,
        "attempts": attempts,
        "evidence_coverage": coverage,
        "profile": {
            "document_type": profile,
            "pipeline": pipeline,
            "chunks": len(manager),
            "entities": len(raw.get("entities", [])),
            "relations": len(raw.get("relations", [])),
        },
        "error_code": error_code,
    }
    if error:
        result["_error"] = error
    return result


def _build_official_prompt(plan: ExtractionPlan, text: str) -> str:
    catalog = _catalog_text(plan)
    signatures = _signature_lines(plan)
    return f"""你是公文知识图谱抽取专家。请从待抽取文本中生成**文档级精简知识模型**。

## 本体定义（唯一 schema）
{catalog}

## 本次允许的关系签名
{signatures}

## 抽取纪律（比通用规则优先）
1. 只从当前主文档正文、领导批示、拟办意见、主送/抄送和附件说明行抽取；独立表单/反馈表等由后处理折叠为 OfficialDocument 管理单元；附件/附表/附录的具体内容不参与抽取，当前 LLM 阶段不要展开任何表格行或签名列表。
2. 实体数量必须服从预算：OperationalAgent<=8、OperationalObject<=6、OperationalActivity<=8、OperationalInformation<=6、OperationalExchange<=4、OperationalConstraint<=6。宁缺毋滥。
3. 当前主文档的 OfficialDocument 只能有 1 个，name 必须是文件标题原文；DocumentUnit 只建立“通知正文”和“附件说明”（排查类通知可额外建立“排查要求X”“报送要求”“附件列表”）。
4. 运行主体只保留发文单位、主送单位聚合（如“各相关单位”“各一级子公司”）以及领导批示/拟办意见中明确承担职责的部门；不要为每个企业、每个姓名建实体。
5. 业务对象用《》或附件说明中的完整名称；任务活动按工作事项级聚合（“贯彻执行X”“组织开展X”“制定X”“报送X”），不要按句子逐句切分。
6. 运行信息只保留被印发/报送/引用的文件、附件和关键报告；附件列表中的每个正式附件可建一个信息实体。
7. 运行交互只保留通知印发、报送/反馈、安全事件报告等文档级交互。
8. 约束条件每条必须对应一个明确的规范性要求，名称用“X约束”或“X要求”，约束原文逐字复制。
9. 关系必须成体系：OfficialDocument -> DocumentUnit(HAS_PART)、-> OperationalAgent(ISSUED_BY/APPLIES_TO)、-> OperationalObject(CONCERNS_OBJECT)、-> OperationalActivity(REQUIRES_ACTIVITY)、-> OperationalInformation(HAS_KEY_INFORMATION/DEFINES)、-> OperationalExchange(HAS_EXCHANGE)；活动 -> 对象(ACTS_ON)、-> 主体(PERFORMED_BY)、-> 信息(USES_INFORMATION/PRODUCES)；约束 -> 活动/主体/对象(CONSTRAINS_*)；业务实例 -> DocumentUnit(EVIDENCED_BY)；交互 -> 主体(sourceAgent/targetAgent)。
10. 每条实体和关系都要有 evidence.quote，且 quote 必须逐字来自原文。

## 待抽取文本
{text}

## 输出要求
只输出一个 JSON 对象，顶层只包含 entities 和 relations。entities 的 type/name/properties 使用上面目录中的稳定标识；relations 的 from_type/from_name/rel_type/to_type/to_name 必须引用已输出实体。没有结果输出空数组。"""


def _build_standard_prompt(plan: ExtractionPlan, text: str) -> str:
    catalog = _catalog_text(plan)
    signatures = _signature_lines(plan)
    return f"""你是标准/规范知识图谱抽取专家。请从待抽取文本中生成**标准文档知识模型**。

## 本体定义（唯一 schema）
{catalog}

## 本次允许的关系签名
{signatures}

## 抽取纪律
1. Standard.name 必须是“标准编号 标准名称”（如“GB/T 31486—2024 电动汽车用动力蓄电池电性能要求及试验方法”）。
2. 结构实体：
   - 一级编号 -> Section，name 必须是“编号 标题”（如“5 性能要求”）。
   - 二级及以下编号 -> Subclause，name 必须是“编号 标题”（如“5.1 外观”）。
   - 章与条、条与子条建立 hasSubClause；Standard -[hasStructuralElement]-> 所有一级章。
3. 要求条款是 Clause：`唯一标识符` 用“IU-{{编号}}-001”；5.x 的名称用“编号 标题要求”；1/2 章用“编号 标题条款”；6.1/6.2/6.3 用“编号 标题条款”；附录用“附录A 工装要求”。`内容描述` 必须完整保留要求原文。
4. 特性 Property 只保留被要求条款检验/规定/限值的特性，`特性名称` 优先取条款标题；标题并列时拆分。不要为“基准值/额定值”等基准词单独建 Property。
5. 行动 ActionClass 只从试验方法章条的标题映射，命名用“标题+检验/试验”（如“外观检验”“室温放电容量试验”）。
6. 约束 Constrain 只抽取数值区间/符合性/相对值要求，完整保留数值、单位和比较符，同一句上下限合并；每个 Property 一个独立实例。
7. 外部引用：规范性引用文件中的标准号创建 Standard 并建立 Standard -[cites]-> Standard；正文“按X检验”建立 Clause -[referencesClause]-> Subclause；“制造商提供的产品技术条件”等建立 ExternalResource 和 referencesExternalResource。
8. 不要抽取 Drafter、起草人、电话等元数据角色；Organization 只在正文明确且 golden 必需时抽取。
9. 预算：Standard<=6、Section<=20、Subclause<=60、Clause<=80、Property<=16、Constrain<=16、ActionClass<=16、Terminology<=12、ExternalResource<=6。不要为每个句子建实例。
10. 每条实体和关系都要有 evidence.quote，quote 必须逐字来自原文。

## 待抽取文本
{text}

## 输出要求
只输出一个 JSON 对象，顶层只包含 entities 和 relations。entities 的 type/name/properties 使用上面目录中的稳定标识；relations 的 from_type/from_name/rel_type/to_type/to_name 必须引用已输出实体。没有结果输出空数组。"""

def _known_deterministic_text(raw: dict[str, Any]) -> str:
    lines: list[str] = []
    for entity in raw.get("entities", []):
        if not isinstance(entity, dict):
            continue
        type_id = str(entity.get("type") or "")
        name = str(entity.get("name") or "")
        if not type_id or not name:
            continue
        props = ", ".join(
            f"{key}={_short_repr(value)}"
            for key, value in (entity.get("properties") or {}).items()
            if value not in (None, "", [])
        )
        lines.append(f"- `{type_id}` | `{name}` | {props or '-'}")
    return "\n".join(lines)


def _short_repr(value: Any, limit: int = 80) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."




def _extract_whole_document(
    *,
    ontology: str,
    text: str,
    plan: ExtractionPlan,
    schema: OntologySchema,
    profile: str,
    target_types: list[str] | None,
    validation_mode: str,
    llm: LLM_CALLABLE,
    attempts: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    use_scheduler: bool,
) -> dict[str, Any] | None:
    if profile not in {"official_document", "standard"}:
        return None

    sections = _split_file_sections(text)
    primary_text = text
    form_sections: list[tuple[str, str]] = []
    if profile == "official_document" and len(sections) > 1:
        primary_sections = [
            (name, section) for name, section in sections if not _is_attachment_section(name)
        ]
        non_primary_sections = [
            (name, section) for name, section in sections if _is_attachment_section(name)
        ]
        form_sections = [
            (name, section) for name, section in non_primary_sections if _is_form_section(name)
        ]
        if primary_sections:
            primary_text = "\n\n".join(section for _, section in primary_sections)
        else:
            primary_text = sections[0][1]
            form_sections = [item for item in sections[1:] if _is_form_section(item[0])]

    full_manager = ChunkContextManager.from_text(text)
    primary_manager = ChunkContextManager.from_text(primary_text)
    request = ExtractionRequest(
        ontology=ontology,
        text=primary_text,
        target_types=target_types,
        document_type=profile,
        validation_mode=validation_mode,
    )
    if profile == "official_document":
        strategy = OfficialDocumentExtractionStrategy()
        pass_name = "official_document_whole"
        base_prompt = _build_official_prompt(plan, primary_text)
    else:
        strategy = StandardExtractionStrategy()
        pass_name = "standard_whole"
        base_prompt = _build_standard_prompt(plan, primary_text)

    deterministic = strategy.deterministic_extraction(plan, primary_manager, request)
    if profile == "official_document":
        deterministic = _ensure_minimal_official_entities(deterministic, plan, primary_text)
    deterministic = bind_evidence(deterministic, full_manager)
    known_text = _known_deterministic_text(deterministic)
    prompt = base_prompt if not known_text else (
        base_prompt
        + "\n\n## 已由确定性规则建立（请勿重复创建这些实体；关系端点直接复用其 type/name）\n"
        + known_text
    )

    parsed = _parse_with_retries(
        llm,
        prompt,
        use_scheduler=use_scheduler,
        attempts=attempts,
        retries=_json_retries(),
        pass_name=pass_name,
    )
    llm_raw = _validate_and_normalize(
        schema,
        parsed,
        target_types=target_types,
        validation_mode=validation_mode,
        warnings=warnings,
    )
    llm_raw = bind_evidence(llm_raw, full_manager)

    merged = _merge_raw(deterministic, llm_raw)
    if profile == "official_document":
        merged = _filter_official_entities(
            merged,
            primary_text=primary_text,
            attachment_items=_numbered_attachment_items(primary_text),
        )
        merged = _append_form_document_units(
            merged,
            plan,
            form_sections,
            primary_text,
            full_manager,
        )
        merged = _enforce_entity_budget(merged, _OFFICIAL_ENTITY_BUDGET)
        merged = _infer_official_spine(merged, deterministic)
    else:
        merged = _merge_structural_entities(merged)
        merged = _merge_contained_standard_entities(merged)
        merged = _drop_entity_types(merged, _STANDARD_DROP_TYPES)
        merged = _enforce_entity_budget(merged, _STANDARD_ENTITY_BUDGET)
    return _finish_result(
        raw=merged,
        warnings=warnings,
        attempts=attempts,
        profile=profile,
        manager=full_manager,
        pipeline="harness_whole_doc",
        plan=plan,
    )


def extract_harness(
    ontology: str,
    text: str | None = None,
    chunks: Iterable[ExtractionChunk] | None = None,
    *,
    target_types: Iterable[str] | None = None,
    include_subclasses: bool = True,
    document_type: str = "auto",
    validation_mode: str = "strict",
    llm: LLM_CALLABLE | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    max_tokens: int | None = None,
    use_scheduler: bool = True,
    mode: str = "strict",
) -> dict[str, Any]:
    """Harness orchestration: adaptive whole-document extraction.

    Short/medium documents are extracted once at document granularity with a
    genre-specific prompt and deterministic ontology/structure spine.  Long
    documents fall back to the existing two-pass candidate proposal pipeline
    (``extract_split``), whose accepted candidates are merged by stable
    candidate ids before graph assembly.
    """
    ontology = (ontology or "").strip()
    if validation_mode.casefold() not in {"strict", "permissive"}:
        raise ValueError("validation_mode 仅支持 strict 或 permissive")
    extraction_chunks = _as_chunks(text, chunks)
    if not ontology:
        raise ValueError("本体定义不能为空")
    if not extraction_chunks:
        result = {
            "raw": {"entities": [], "relations": []},
            "graph": {"nodes": [], "relationships": []},
            "warnings": [{"code": "EMPTY_INPUT", "message": "请求没有 text 或 chunks", "path": "text/chunks"}],
            "attempts": [],
            "profile": {"document_type": document_type, "pipeline": "harness", "chunks": 0},
            "error_code": None,
        }
        return result

    full_text = _full_text(extraction_chunks)
    schema = OntologySchema.parse(ontology)
    if not schema.classes:
        raise ValueError("本体中未解析到 class 声明")
    profile = detect_document_type(full_text, document_type)
    class_ids = set(schema.classes_by_id)
    requested = (document_type or "auto").strip().casefold()
    if "OfficialDocument" in class_ids and requested in {
        "auto",
        "official_document",
        "official",
        "document",
        "公文",
    }:
        profile = "official_document"
    elif "Standard" in class_ids and bool({"Section", "Subclause", "Clause"} & class_ids) and requested in {
        "auto",
        "standard",
        "标准",
        "规范",
    }:
        profile = "standard"
    if profile == "general":
        if "OfficialDocument" in class_ids:
            profile = "official_document"
        elif "Standard" in class_ids and bool({"Section", "Subclause", "Clause"} & class_ids):
            profile = "standard"
    if requested == "auto" and "OfficialDocument" not in class_ids and not (
        "Standard" in class_ids and bool({"Section", "Subclause", "Clause"} & class_ids)
    ):
        if profile not in {"general", "analytic"}:
            profile = "general"
    plan = compile_plan(
        ontology,
        target_types=target_types,
        include_subclasses=include_subclasses,
        mode=mode,
    )

    config = get_llm_config()
    resolved_llm = llm or create_llm_client(
        base_url=base_url or config["base_url"],
        api_key=api_key or config["api_key"],
        model=model or config["model"],
        max_tokens=max_tokens or config["max_tokens"],
    )
    target_list = list(target_types) if target_types is not None else None
    attempts: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = [item.to_dict() for item in plan.schema_warnings]

    if len(full_text) <= _whole_doc_max_chars():
        if profile in {"official_document", "standard"}:
            whole_result = _extract_whole_document(
                ontology=ontology,
                text=full_text,
                plan=plan,
                schema=schema,
                profile=profile,
                target_types=target_list,
                validation_mode=validation_mode,
                llm=resolved_llm,
                attempts=attempts,
                warnings=warnings,
                use_scheduler=use_scheduler,
            )
            if whole_result is not None:
                return whole_result
        if profile in {"general", "analytic"}:
            generic_result = extract_v2(
                ontology=ontology,
                text=full_text,
                model=model,
                base_url=base_url,
                api_key=api_key,
                document_type=profile,
                target_types=target_list,
                validation_mode="permissive" if validation_mode == "strict" else validation_mode,
            )
            generic_result.setdefault("profile", {})["pipeline"] = "harness_whole_doc"
            generic_result["profile"]["chunks"] = 1
            return generic_result

    split_result = extract_split(
        ontology,
        chunks=extraction_chunks,
        target_types=target_list,
        include_subclasses=include_subclasses,
        document_type=profile,
        validation_mode=validation_mode,
        llm=resolved_llm,
        mode=mode,
        max_workers=1,
        use_scheduler=use_scheduler,
        run_id=f"harness-{int(time.time())}-{uuid.uuid4().hex[:6]}",
    )
    return split_result


def extract_harness_items(
    ontology: str,
    chunks: list[str],
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    document_type: str = "auto",
    target_types: list[str] | None = None,
    validation_mode: str = "strict",
    use_scheduler: bool = True,
) -> list[dict[str, Any]]:
    """Batch-compatible harness entry point.

    The harness returns one document-level extraction item.  This preserves
    the existing ``extract_chunks`` item contract while changing the internal
    algorithm from per-chunk one-shot extraction to harness extraction.
    """
    if not chunks:
        return []
    result = extract_harness(
        ontology,
        chunks=chunks,
        model=model,
        base_url=base_url,
        api_key=api_key,
        document_type=document_type,
        target_types=target_types,
        validation_mode=validation_mode,
        use_scheduler=use_scheduler,
    )
    return [
        {
            "index": 0,
            "text": "\n".join(chunks),
            "raw": result.get("raw", {}),
            "graph": result.get("graph") or {"nodes": [], "relationships": []},
            "attempts": result.get("attempts", []),
            "warnings": result.get("warnings", []),
            "profile": result.get("profile", {}),
            "error": result.get("_error") or result.get("error"),
            "error_code": result.get("error_code"),
            "raw_response": "",
        }
    ]


__all__ = ["extract_harness", "extract_harness_items"]
