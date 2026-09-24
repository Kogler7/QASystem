"""General Property/Constrain semantics for standard requirement clauses."""
from __future__ import annotations
import json, re, unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from ._logging import get_step_logger
from .candidate_models import EntityCandidate
from .evidence import Evidence
from .graph_utils import canonical_name
from .prompt_router import STANDARD_PROMPT_EXTENSION

logger = get_step_logger(__name__)

PTYPES = {"描述型", "能力型", "约束型"}
CTYPES = {"时间约束", "权限约束", "安全约束", "流程约束", "质量约束", "合规约束"}
BLOCK = {"适用范围", "电池类型", "范围", "电性能要求", "充电方法"}

SYSTEM = (
    STANDARD_PROMPT_EXTENSION
    + "\n\n任务：对给定条款正文输出 items 数组，元素为 property 或 constraint，字段按上规则；只输出 JSON object，顶层键为 items。"
)
SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "kind": {"type": "string"}, "name": {"type": "string"}, "type": {"type": "string"}, "value": {"type": "string"},
    "min": {"type": "string"}, "max": {"type": "string"}, "range": {"type": "string"}, "unit": {"type": "string"},
    "property": {"type": "string"}, "quote": {"type": "string"}},
    "required": ["kind", "name", "quote"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False}
_UNIT = r"(?:°C/h|℃/h|%/h|m/s|kPa|°C|℃|%|min|ms|h|d|s|V|A|Ah|Wh|kg|FS|天|次)"
_FORMULA = re.compile(r"\d+(?:\.\d+)?\s*I_\{?\d*\}?")
def _norm_quote(q: Any) -> str:
    q = unicodedata.normalize("NFKC", str(q or ""))
    q = q.replace("\\pm", "±").replace("^{\\circ}", "°").replace("^\\circ", "°").replace("\\circ", "°")
    q = q.replace("\\degree", "°").replace("\\%", "%").replace("\\sim", "~")
    q = re.sub(r"\\[ ,;!]", " ", q).replace("$", " ").replace("{", "").replace("}", "")
    q = q.replace("～", "~").replace("—", "-").replace("–", "-").replace("−", "-")
    return re.sub(r"\s+", " ", q).strip()
def _num(v: Any) -> str: return f"{float(v):g}"
def parse_constraint_values(quote: Any, name: str = "") -> dict[str, str]:
    q = _norm_quote(quote); out = {"最小值": "", "最大值": "", "阈值范围": "", "测量单位": ""}
    has_formula = bool(_FORMULA.search(q))
    if has_formula and "电流" in str(name): return out
    if has_formula: q = re.sub(r"\s+", " ", _FORMULA.sub(" ", q)).strip()
    m = re.search(rf"(\d+(?:\.\d+)?)\s*({_UNIT})?\s*(?:±|\+/-)\s*(\d+(?:\.\d+)?)\s*({_UNIT})?", q)
    if m:
        c, d = float(m.group(1)), float(m.group(3)); u = m.group(4) or m.group(2) or ""
        out.update({"最小值": _num(c-d), "最大值": _num(c+d), "阈值范围": f"{_num(c-d)}-{_num(c+d)}", "测量单位": u}); return out
    m = re.search(rf"(\d+(?:\.\d+)?)\s*({_UNIT})?\s*(?:~|至)\s*(\d+(?:\.\d+)?)\s*({_UNIT})?", q)
    if m:
        a, b = float(m.group(1)), float(m.group(3)); u = m.group(4) or m.group(2) or ""
        out.update({"最小值": _num(a), "最大值": _num(b), "阈值范围": f"{_num(a)}-{_num(b)}", "测量单位": u}); return out
    m = re.search(rf"(不应超过|不得超过|不应大于|不得大于|不超过|不大于|低于|小于|≤|<=|<)[^0-9]{{0,12}}?(\d+(?:\.\d+)?)\s*({_UNIT})?", q)
    if m: out.update({"最大值": _num(m.group(2)), "测量单位": m.group(3) or ""}); return out
    m = re.search(rf"(不应低于|不得低于|不应小于|不得小于|不低于|不小于|高于|大于|≥|>=|>)[^0-9]{{0,12}}?(\d+(?:\.\d+)?)\s*({_UNIT})?", q)
    if m: out.update({"最小值": _num(m.group(2)), "测量单位": m.group(3) or ""}); return out
    m = re.search(rf"±\s*(\d+(?:\.\d+)?)\s*({_UNIT})?", q)
    if m:
        v = float(m.group(1)); out.update({"最小值": _num(-v), "最大值": _num(v), "阈值范围": f"{_num(-v)}-{_num(v)}", "测量单位": m.group(2) or ""}); return out
    m = re.search(rf"(\d+(?:\.\d+)?)\s*({_UNIT})", q)
    if m: out.update({"阈值范围": _num(m.group(1)), "测量单位": m.group(2) or ""}); return out
    return out
def _ptype(kind: str, quote: str) -> str:
    if kind in PTYPES: return kind
    if re.search(r"不低于|不超过|不大于|极差|不得|不应", quote): return "约束型"
    if re.search(r"容量|能量|功率|效率|电流|电压|温度|时间", quote): return "能力型"
    return "描述型"
def _ctype(kind: str, quote: str) -> str:
    if kind in CTYPES: return kind
    if "安全" in quote: return "安全约束"
    if re.search(r"期限|时间|日期|时限", quote): return "时间约束"
    if re.search(r"权限|授权", quote): return "权限约束"
    if re.search(r"流程|程序|步骤", quote): return "流程约束"
    if re.search(r"应符合|符合.{0,12}规定", quote): return "合规约束"
    return "质量约束"
def _spec(plan: Any, type_id: str) -> Any | None:
    specs = getattr(plan, "class_specs", {}) or {}
    key = canonical_name(type_id)
    return specs.get(type_id) or next((v for k, v in specs.items() if canonical_name(k) == key), None)
def _prop_id(spec: Any, *markers: str) -> str | None:
    normalized = tuple(canonical_name(m) for m in markers)
    for prop in getattr(spec, "properties", ()) or ():
        symbols = canonical_name(" ".join((str(getattr(prop, "identifier", "") or ""), str(getattr(prop, "display_name", "") or ""))))
        if any(m in symbols for m in normalized): return str(getattr(prop, "identifier", "") or getattr(prop, "display_name", ""))
    return None
def _props(spec: Any, values) -> dict:
    out = {}
    for markers, value in values:
        pid = _prop_id(spec, *markers)
        if pid and value not in (None, ""): out[pid] = value
    return out
def _body_of(entity: Any, manager: Any):
    evidence = next(iter(getattr(entity, "evidence", ()) or ()), None)
    if evidence is None: return None
    chunk = None
    for block_id in list(getattr(evidence, "source_block_ids", ()) or ()):
        try: chunk = manager.get_chunk(block_id); break
        except Exception: continue
    if chunk is None: return None
    text = chunk.text or ""
    start = evidence.end_char if isinstance(evidence.end_char, int) else 0
    if not 0 <= start <= len(text): start = 0
    m = re.search(r"(?m)^\s{0,3}#{1,6}\s+", text[start:])
    end = start + m.start() if m else len(text)
    return chunk, start, text[start:end]
def _extract_one(llm: Any, clause: str, body: str) -> list:
    resp = llm.generate_structured(schema=SCHEMA, messages=[
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": json.dumps({"clause": clause, "text": body[:5000]}, ensure_ascii=False)}],
        run_context={"schema_name": "standard_requirements", "response_mode": "json_schema"})
    raw = getattr(resp, "text", resp)
    data = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(data, dict): return data.get("items") or []
    return data if isinstance(data, list) else []
def enrich_standard_requirements(pool: Any, manager: Any, plan: Any, llm: Any, *, warnings: Any = None, max_workers: int = 4, max_clauses: int = 80) -> dict[str, int]:
    clause_spec, prop_spec, cons_spec = _spec(plan, "Clause"), _spec(plan, "Property"), _spec(plan, "Constrain")
    if clause_spec is None or prop_spec is None or cons_spec is None or llm is None: return {}
    work = []
    for entity in pool.accepted_entities():
        if canonical_name(getattr(entity, "canonical_type", "")) != canonical_name("Clause"): continue
        name = str(getattr(entity, "name", ""))
        if not re.match(r"^\d+(?:\.\d+)* ", name): continue
        info = _body_of(entity, manager)
        if info is None: continue
        chunk, start, body = info
        if len(body) < 20: continue
        if not re.search(r"不低于|不超过|不大于|极差|容量|性能|尺寸|外观|标识|能量效率|电压|电流|质量|温度|符合|范围|单位|精度", body): continue
        work.append((entity, chunk, start, body))
    work = work[:max_clauses]
    if not work: return {"Property": 0, "Constrain": 0}
    def run(item):
        entity, chunk, start, body = item
        try: return item, _extract_one(llm, str(getattr(entity, "name", "")), body)
        except Exception: return item, []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
        results = list(ex.map(run, work))
    counts = {"Property": 0, "Constrain": 0}
    seen_props: set[str] = set()
    seen_cons: set[str] = set()
    for (entity, chunk, start, body), items in results:
        text = chunk.text or ""
        for item in items:
            if not isinstance(item, dict): continue
            quote = str(item.get("quote") or "")
            if not quote or quote not in body: continue
            offset = body.find(quote); begin = start + offset; finish = begin + len(quote)
            evidence = Evidence(span_id=f"{chunk.chunk_id}:{begin}:{finish}", source_block_ids=[chunk.chunk_id],
                                quote=quote, start_char=begin, end_char=finish, evidence_type="explicit")
            data = {
                "name": item.get("name") or item.get("名称") or "",
                "type": item.get("type") or item.get("特性类型") or item.get("约束类型") or "",
                "value": item.get("value") or item.get("特性值") or "",
                "min": item.get("min") or item.get("最小值") or "",
                "max": item.get("max") or item.get("最大值") or "",
                "range": item.get("range") or item.get("阈值范围") or "",
                "unit": item.get("unit") or item.get("测量单位") or "",
            }
            raw_kind = str(item.get("kind") or "")
            is_constraint = (
                "con" in raw_kind.casefold()
                or "约束" in raw_kind + str(data["type"])
                or any(str(data[key]).strip() for key in ("min", "max", "range", "unit"))
            )
            if is_constraint:
                name = re.sub(r"\s+", "", str(data["name"]))[:20]
                if not name: continue
                if any(token in name for token in ("方法", "步骤", "计算", "公式", "单位", "试验")): continue
                if name.endswith("要求"): continue
                if name in seen_cons: continue
                seen_cons.add(name)
                mn, mx, rg, un = (str(data[key]).strip() for key in ("min", "max", "range", "unit"))
                if not any((mn, mx, rg, un)):
                    parsed = parse_constraint_values(quote, name); mn, mx, rg, un = parsed["最小值"], parsed["最大值"], parsed["阈值范围"], parsed["测量单位"]
                properties = _props(cons_spec, ((("约束类型", "constrainttype"), _ctype(str(data["type"]), quote)),
                                                (("最大值", "maxvalue"), mx), (("最小值", "minvalue"), mn),
                                                (("阈值范围", "range"), rg), (("测量单位", "unit"), un)))
                candidate = EntityCandidate(candidate_id="", proposed_type="Constrain", canonical_type="Constrain", name=name,
                                            properties=properties, evidence=[evidence], source="standard_semantics")
                before = pool.entity_candidate_count; pool.add_entity(candidate, warnings=warnings); counts["Constrain"] += pool.entity_candidate_count - before
            else:
                name = re.sub(r"\s+", "", str(data["name"]))
                for suffix in ("要求", "条件", "方法", "步骤", "计量", "单位", "计算", "试验", "检查", "定义", "规定"):
                    if name.endswith(suffix) and len(name) > len(suffix):
                        name = name[: -len(suffix)]
                if not name or name in BLOCK or len(name) > 8 or name in seen_props: continue
                seen_props.add(name)
                properties = _props(prop_spec, ((("特性名称", "name"), name), (("特性类型", "type"), _ptype(str(data["type"]), quote)),
                                                (("特性值", "value"), str(data["value"]))))
                candidate = EntityCandidate(candidate_id="", proposed_type="Property", canonical_type="Property", name=name,
                                            properties=properties, evidence=[evidence], source="standard_semantics")
                before = pool.entity_candidate_count; pool.add_entity(candidate, warnings=warnings); counts["Property"] += pool.entity_candidate_count - before
    logger.info("[步骤][抽取] 标准要求语义补充: clauses=%d Property=%d Constrain=%d", len(work), counts["Property"], counts["Constrain"])
    return counts
__all__ = ["enrich_standard_requirements", "parse_constraint_values"]
