"""Re-extract Property/Constrain from source clauses per ontology, replacing legacy nodes."""
from __future__ import annotations
import json, os, re, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CASE = sys.argv[1] if len(sys.argv) > 1 else "standard_01"
ROOT = Path.home() / "sxm/qasystem/algorithm/knowledge_extract_v2/benchmark/standard" / CASE
G = json.load(open(ROOT / "extract/golden_v2.json", encoding="utf-8"))
E, R = G["entities"], G["relations"]
def _strip_front_matter(t):
    return re.sub(r"\A\s*---\n.*?\n---\s*\n", "", t, count=1, flags=re.S)

def load_case_text():
    """03/04 是多 part 文档：按 part 号拼接；下划线整版是低质量转换，忽略。"""
    docs_dir = ROOT / "docs"
    parts = sorted(docs_dir.glob("*.part-*.md"))
    if parts:
        text = "\n\n".join(_strip_front_matter(p.read_text(encoding="utf-8", errors="replace")) for p in parts)
        return parts[0].name, text
    docs = sorted(docs_dir.glob("*.md"))
    main = [p for p in docs if "_" not in p.name] or docs
    p = max(main, key=lambda x: x.stat().st_size)
    return p.name, p.read_text(encoding="utf-8", errors="replace")

DOCNAME, TEXT = load_case_text()

HEAD = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$", re.M)
HS = [{"start": m.start(), "level": len(m.group(1)), "raw": m.group(0).strip(), "text": m.group(2).strip()} for m in HEAD.finditer(TEXT)]
def own_body_of(quote):
    i = TEXT.find(quote)
    if i < 0: return ""
    j = len(TEXT)
    for h in HS:
        if h["start"] > i: j = h["start"]; break
    return TEXT[i:j].strip()
def clean(s): return re.sub(r"\s+", " ", str(s or "")).strip()

def build_client():
    sys.path.insert(0, "/home/xmshi/sxm/qasystem")
    from algorithm.knowledge_extract_v2 import v2_benchmark_eval as ev
    return ev._build_judge_client(model=ev._DEFAULT_MODEL, base_url=ev._DEFAULT_BASE_URL,
                                  api_key=ev._DEFAULT_API_KEY, max_tokens=None)

SYSTEM = """你是标准技术语义抽取器。只依据给定条款正文，输出 items 数组，每项 kind 为 "property" 或 "constraint"。
property 字段：name(<=8汉字)、type(描述型/能力型/约束型)、value、quote(逐字原文)。
constraint 字段：name(<=20汉字)、type(时间约束/权限约束/安全约束/流程约束/质量约束/合规约束)、min、max、range(格式 最小值-最大值)、unit、property(被约束的特性名)、quote(逐字原文)。
只输出正文有依据的项；quote 必须逐字来自正文；不要把"适用范围/电池类型"当特性；没有就输出空 items。"""

SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "kind": {"type": "string"}, "name": {"type": "string"}, "type": {"type": "string"}, "value": {"type": "string"},
    "min": {"type": "string"}, "max": {"type": "string"}, "range": {"type": "string"}, "unit": {"type": "string"},
    "property": {"type": "string"}, "quote": {"type": "string"}},
    "required": ["kind", "name", "quote"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False}

candidates = []
for c in [e for e in E if e["type"] == "Clause"]:
    name = c["name"]
    if not re.match(r"^\d+(?:\.\d+)* ", name): continue
    body = own_body_of(c.get("evidence", {}).get("quote", ""))
    if len(body) < 20: continue
    if not re.search(r"不低于|不超过|不大于|极差|容量|性能|尺寸|外观|标识|能量效率|电压|电流|质量|温度|符合", body): continue
    candidates.append((c["name"], body))

def normalize(data):
    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            data = data["items"]
        elif isinstance(data.get("properties"), list) or isinstance(data.get("constraints"), list):
            items = []
            for x in data.get("properties") or []:
                items.append({**x, "kind": "property"})
            for x in data.get("constraints") or []:
                items.append({**x, "kind": "constraint"})
            data = items
        else:
            data = [data]
    if not isinstance(data, list):
        return {"items": []}
    out = []
    for it in data:
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind") or "").casefold()
        is_cons = ("con" in kind or "约束" in str(it.get("type") or "")
                   or any(k in it for k in ("min", "max", "range", "unit", "最小值", "最大值", "阈值范围", "测量单位")))
        out.append({"kind": "constraint" if is_cons else "property",
                    "name": str(it.get("name") or it.get("名称") or ""),
                    "type": str(it.get("type") or it.get("特性类型") or it.get("约束类型") or ""),
                    "value": str(it.get("value") or it.get("特性值") or ""),
                    "min": str(it.get("min") or it.get("最小值") or ""),
                    "max": str(it.get("max") or it.get("最大值") or ""),
                    "range": str(it.get("range") or it.get("阈值范围") or ""),
                    "unit": str(it.get("unit") or it.get("测量单位") or ""),
                    "property": str(it.get("property") or it.get("特性") or ""),
                    "quote": str(it.get("quote") or it.get("证据") or "")})
    return {"items": out}


def extract(item):
    name, body = item
    try:
        client = build_client()
        resp = client.generate_structured(schema=SCHEMA,
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": json.dumps({"clause": name, "text": body[:5000]}, ensure_ascii=False)}],
            run_context={"schema_name": "standard_semantics_v2", "response_mode": "json_schema"})
        raw = getattr(resp, "text", resp)
        data = json.loads(raw) if isinstance(raw, str) else raw
        return name, body, normalize(data)
    except Exception as exc:
        return name, body, {"items": []}

results = []
with ThreadPoolExecutor(max_workers=4) as ex:
    for name, body, data in ex.map(extract, candidates):
        results.append((name, body, data))

import unicodedata

_UNIT = r"(?:°C/h|℃/h|%/h|m/s|kPa|°C|℃|%|min|ms|h|d|s|V|A|Ah|Wh|kg|FS|天|次)"
_FORMULA = re.compile(r"\d+(?:\.\d+)?\s*I_\{?\d*\}?")

def _norm_quote(quote):
    q = unicodedata.normalize("NFKC", str(quote or ""))
    q = q.replace("\\pm", "±").replace("^{\\circ}", "°").replace("^\\circ", "°").replace("\\circ", "°")
    q = q.replace("\\degree", "°").replace("\\%", "%").replace("\\sim", "~")
    q = re.sub(r"\\[ ,;!]", " ", q)
    q = q.replace("$", " ").replace("{", "").replace("}", "")
    q = q.replace("～", "~").replace("—", "-").replace("–", "-").replace("−", "-")
    q = re.sub(r"°\s*C", "°C", q)
    q = re.sub(r"\s+", " ", q)
    return q.strip()

def _num(v):
    return f"{float(v):g}"

def parse_constraint_values(quote, name=""):
    """A1: 从 quote 解析数值到 最小值/最大值/阈值范围/测量单位。
    公式型电流约束（如 1I3、0.2I1）不做数值化，保留原 quote。"""
    q = _norm_quote(quote)
    out = {"最小值": "", "最大值": "", "阈值范围": "", "测量单位": ""}
    has_formula = bool(_FORMULA.search(q))
    if has_formula and "电流" in str(name):
        return out
    if has_formula:
        q = _FORMULA.sub(" ", q)
        q = re.sub(r"\s+", " ", q).strip()
    m = re.search(rf"(\d+(?:\.\d+)?)\s*({_UNIT})?\s*(?:±|\+/-)\s*(\d+(?:\.\d+)?)\s*({_UNIT})?", q)
    if m:
        c = float(m.group(1)); d = float(m.group(3)); u = m.group(4) or m.group(2) or ""
        out.update({"最小值": _num(c - d), "最大值": _num(c + d), "阈值范围": f"{_num(c - d)}-{_num(c + d)}", "测量单位": u})
        return out
    m = re.search(rf"(\d+(?:\.\d+)?)\s*({_UNIT})?\s*(?:~|至)\s*(\d+(?:\.\d+)?)\s*({_UNIT})?", q)
    if m:
        a = float(m.group(1)); b = float(m.group(3)); u = m.group(4) or m.group(2) or ""
        out.update({"最小值": _num(a), "最大值": _num(b), "阈值范围": f"{_num(a)}-{_num(b)}", "测量单位": u})
        return out
    m = re.search(rf"(不应超过|不得超过|不应大于|不得大于|不应低于|不得低于|不应小于|不得小于|不超过|不大于|不低于|不小于|低于|小于|高于|大于|≤|<=|≥|>=|<|>)[^0-9]{{0,12}}?(\d+(?:\.\d+)?)\s*({_UNIT})?", q)
    if m:
        kw = m.group(1)
        if kw in {"不应超过", "不得超过", "不应大于", "不得大于", "不超过", "不大于", "低于", "小于", "<", "<=", "≤"}:
            out.update({"最大值": _num(m.group(2)), "测量单位": m.group(3) or ""})
        else:
            out.update({"最小值": _num(m.group(2)), "测量单位": m.group(3) or ""})
        return out
    m = re.search(rf"±\s*(\d+(?:\.\d+)?)\s*({_UNIT})?", q)
    if m:
        v = float(m.group(1))
        out.update({"最小值": _num(-v), "最大值": _num(v), "阈值范围": f"{_num(-v)}-{_num(v)}", "测量单位": m.group(2) or ""})
        return out
    m = re.search(rf"(\d+(?:\.\d+)?)\s*({_UNIT})", q)
    if m:
        out.update({"阈值范围": _num(m.group(1)), "测量单位": m.group(2) or ""})
        return out
    return out


PTYPES = {"描述型", "能力型", "约束型"}
CTYPES = {"时间约束", "权限约束", "安全约束", "流程约束", "质量约束", "合规约束"}
BLOCK = {"适用范围", "电池类型", "范围", "电性能要求", "充电方法"}
# B1 同类项：不是特性(功能/流程/工具/分类维度)，与 01 的电性能要求/充电方法同类
BLOCK |= {
    "standard_02": {"用户注册管理", "共建控制管理", "日志管理", "协商一致机制", "服务绩效管理"},
    "standard_03": {"结构化预处理工具", "内容标记", "单元/模块测试", "集成测试", "性能测试", "成果类型", "验证维度"},
    "standard_04": {"角色能力", "资源条件", "行为活动", "数据要素"},
}.get(CASE, set())
def ptype(kind_type, quote):
    if kind_type in PTYPES: return kind_type
    if re.search(r"不低于|不超过|不大于|极差|不得|不应", quote): return "约束型"
    if re.search(r"容量|能量|功率|效率|电流|电压|温度|时间", quote): return "能力型"
    return "描述型"
def ctype(kind_type, quote):
    if kind_type in CTYPES: return kind_type
    if "安全" in quote: return "安全约束"
    if re.search(r"期限|时间|日期|时限", quote): return "时间约束"
    if re.search(r"权限|授权", quote): return "权限约束"
    if re.search(r"流程|程序|步骤", quote): return "流程约束"
    if re.search(r"应符合|符合.{0,12}规定", quote): return "合规约束"
    return "质量约束"
props, cons = [], []
for clause, body, data in results:
    for it in (data.get("items") if isinstance(data, dict) else []) or []:
        q = it.get("quote") or ""
        if not q or q not in body: continue
        name = clean(it.get("name"))
        if it.get("kind") == "constraint":
            mn, mx, rg, un = clean(it.get("min")), clean(it.get("max")), clean(it.get("range")), clean(it.get("unit"))
            if not any((mn, mx, rg, un)) and not re.search(r"\d", q): continue
            parsed = parse_constraint_values(q, name)
            cprops = {"约束类型": ctype(clean(it.get("type")), q),
                     "最大值": mx or parsed["最大值"], "最小值": mn or parsed["最小值"],
                     "阈值范围": rg or parsed["阈值范围"], "测量单位": un or parsed["测量单位"]}
            cons.append((clause, clean(it.get("property")), {"type": "Constrain", "name": name[:20] or (rg or q)[:20],
                         "properties": cprops,
                         "evidence": {"quote": q, "document": DOCNAME}}))
        else:
            if not name or name in BLOCK: continue
            props.append((clause, {"type": "Property", "name": name[:12],
                         "properties": {"特性名称": name[:12], "特性类型": ptype(clean(it.get("type")), q),
                                        "特性值": clean(it.get("value"))},
                         "evidence": {"quote": q, "document": DOCNAME}}))

legacy = {e["name"] for e in E if e["type"] in {"Property", "Constrain"}}
E = [e for e in E if e["type"] not in {"Property", "Constrain"}]
R = [r for r in R if r.get("to_name") not in legacy and r.get("from_name") not in legacy and r.get("rel_type") != "imposesConstraint"]
keys = {(e["type"], e["name"]) for e in E}
def add(e):
    k = (e["type"], e["name"])
    if k in keys: return None
    keys.add(k); E.append(e); return e

subjects = [e["name"] for e in E if e["type"] == "Subject of Standardization"]
prop_by_name = {}
for clause, ent in props:
    if add(ent):
        prop_by_name[ent["name"]] = ent
        R.append({"from_type": "Clause", "from_name": clause, "rel_type": "specifiesCharacteristic", "to_type": "Property", "to_name": ent["name"]})
        subj = next((s for s in subjects if s in ent["evidence"]["quote"]), subjects[0] if subjects else None)
        if subj: R.append({"from_type": "Subject of Standardization", "from_name": subj, "rel_type": "hasCharacteristic", "to_type": "Property", "to_name": ent["name"]})
for clause, prop_name, ent in cons:
    if add(ent):
        target = prop_name if prop_name in prop_by_name else next((p for c2, p in [(c, e["name"]) for c, e in props] if c == clause), None)
        if target:
            R.append({"from_type": "Property", "from_name": target, "rel_type": "imposesConstraint", "to_type": "Constrain", "to_name": ent["name"]})
        else:
            R.append({"from_type": "Clause", "from_name": clause, "rel_type": "imposesConstraint", "to_type": "Constrain", "to_name": ent["name"]})

G["entities"], G["relations"] = E, R
G["semantics_v2"] = {"source_clauses": len(candidates), "properties": len(props), "constraints": len(cons), "errors": [d.get("error") for _, _, d in results if d.get("error")][:3]}
(ROOT / "extract/golden_v2.json").write_text(json.dumps(G, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps({"case": CASE, "candidate_clauses": len(candidates), "properties": len(props), "constraints": len(cons),
                  "errors": G["semantics_v2"]["errors"]}, ensure_ascii=False, indent=1))