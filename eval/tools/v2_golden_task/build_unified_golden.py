"""Generate unified golden_v2 for a standard case (first version: standard_01)."""
from __future__ import annotations
import json, re, sys
from collections import Counter, defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
while not (_REPO_ROOT / ".git").exists() or not (_REPO_ROOT / "pytest.ini").is_file():
    _REPO_ROOT = _REPO_ROOT.parent
    if _REPO_ROOT == _REPO_ROOT.parent:
        raise RuntimeError("无法定位仓库根目录")
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
CASE = sys.argv[1] if len(sys.argv) > 1 else "standard_01"
ROOT = _REPO_ROOT / "test_data" / "eval-v2" / "standard" / CASE
OLD = json.load(open(ROOT / "extract/golden.json", encoding="utf-8"))
HEAD = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$", re.M)
NUM = re.compile(r"^((?:\d+(?:\.\d+)*)|(?:[A-Z](?:\.\d+)+))\s+(.+?)\s*$")
REF = re.compile(r"GB(?:/T)?\s*\d+(?:\.\d+)*\s*(?:[—–-]\s*\d{4})?[^\n]*")
TERM_SPLIT = re.compile(r"([\u3400-\u9fff][^\u4e00-\u9fffa-zA-Z]*?)\s*([a-zA-Z].*)?$")
def norm(s): return re.sub(r"\s+", "", str(s or ""))
def clean(s): return re.sub(r"\s+", " ", str(s or "")).strip()
def addi(s, *a): return any(x in s for x in a)
def first_sentence(body):
    body = str(body or "").strip()
    if not body: return ""
    m = re.split(r"(?<=[。！？；])", body)
    return (m[0] if m else body).strip()

# ---------- parse docs ----------
def parse_headings(text):
    hs = []
    for m in HEAD.finditer(text):
        body = m.group(2).strip(); nm = NUM.match(body)
        hs.append({"level": len(m.group(1)), "raw": m.group(0).strip(), "text": body,
                   "number": nm.group(1) if nm else None,
                   "title": nm.group(2).strip() if nm else clean(body),
                   "start": m.start(), "end": m.end()})
    return hs
def _strip_front_matter(t):
    return re.sub(r"\A\s*---\n.*?\n---\s*\n", "", t, count=1, flags=re.S)
def best_doc():
    parts = sorted((ROOT / "docs").glob("*.part-*.md"))
    if parts:
        text = "\n\n".join(_strip_front_matter(p.read_text(encoding="utf-8", errors="replace")) for p in parts)
        return parts[0].name, text, parse_headings(text), 10**9
    docs = [p for p in sorted((ROOT / "docs").glob("*.md")) if "_" not in p.name] or sorted((ROOT / "docs").glob("*.md"))
    best = None
    for p in docs:
        text = p.read_text(encoding="utf-8", errors="replace")
        hs = parse_headings(text)
        score = sum(10 for h in hs if h["number"] and "." in h["number"]) + sum(3 for h in hs if h["number"] and "." not in h["number"])
        if best is None or score > best[3]: best = (p.name, text, hs, score)
    return best
DOC, TEXT, HS, _ = best_doc()
def body_of(h):
    end = len(TEXT)
    for o in HS:
        if o["start"] > h["start"] and o["level"] <= h["level"]: end = o["start"]; break
    return TEXT[h["end"]:end].strip()

def strip_math_md(value):
    value = re.sub(r"\$[^$]*\$", "", str(value or ""))
    value = re.sub(r"\*\*?|__|`", "", value)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)|\[[^\]]*\]\([^)]*\)", "", value)
    return clean(value)

def short_name(value, n=24):
    text = strip_math_md(value)
    head = re.split(r"[，,；;：:]", text)[0].strip()
    if 4 <= len(head) <= n:
        return head
    if len(text) <= n:
        return text
    cut = text[:n]
    for sep in ("。", "；", ";", "，", ",", "：", ":"):
        pos = cut.rfind(sep)
        if pos >= n // 2:
            cut = cut[:pos]
            break
    return cut.strip("；;，,。：: ")

def lead_before_list(body):
    m = re.search(r"\n\s*(?:[a-z][）)]|——)\s*", str(body or ""), re.M)
    return str(body or "")[:m.start()] if m else str(body or "")
def heading_by_number(num):
    return next((h for h in HS if h["number"] == num), None)

def own_body(h):
    end = len(TEXT)
    for o in HS:
        if o["start"] > h["start"]:
            end = o["start"]; break
    return TEXT[h["end"]:end].strip()

# ---------- new entities / relations ----------
ENTS, RELS, KEYS, RKEYS = [], [], set(), set()
def add(e):
    k = (e["type"], norm(e["name"]))
    if k in KEYS: return None
    KEYS.add(k); ENTS.append(e); return e
def link(ft, fn, rt, tt, tn, quote="", doc=""):
    k = (ft, fn, rt, tt, tn)
    if k in RKEYS: return
    RKEYS.add(k)
    r = {"from_type": ft, "from_name": fn, "rel_type": rt, "to_type": tt, "to_name": tn}
    if quote: r["evidence"] = {"quote": quote, "document": doc}
    RELS.append(r)

# ---------- standard metadata / orgs / stage ----------
old_std = next(e for e in OLD["entities"] if e.get("type") == "Standard")
STD = dict(old_std); add(STD); SN = STD["name"]
for r in OLD["relations"]:
    if r.get("from_name") == old_std["name"] and r.get("rel_type") in {"classifiedUnder", "issuedBy", "proposedBy", "administeredBy", "hasDevelopmentStage", "standardizes", "hasPart"}:
        target = next((e for e in OLD["entities"] if e.get("name") == r.get("to_name") and e.get("type") == r.get("to_type")), None)
        if target: add(dict(target)); link(r["from_type"], SN, r["rel_type"], r["to_type"], target["name"], str((r.get("evidence") or {}).get("quote", "")), (r.get("evidence") or {}).get("document", ""))

# ---------- structure ----------
sections, subclauses, terms, infos, annexes = [], [], [], [], []
for h in HS:
    if h["number"]:
        if h["number"].startswith("3."):
            terms.append(h); subclauses.append(h)
        elif "." not in h["number"]: sections.append(h)
        else: subclauses.append(h)
    else:
        n = norm(h["text"])
        if n in {"前言", "引言", "参考文献"}: infos.append(h)
        elif h["text"].startswith("附录"): annexes.append(h)
for h in sections: add({"type": "Section", "name": f'{h["number"]} {h["title"]}', "properties": {"章编号": h["number"], "章标题": h["title"]}, "evidence": {"quote": h["raw"], "document": DOC}})
for h in subclauses: add({"type": "Subclause", "name": f'{h["number"]} {h["title"]}', "properties": {"条编号": h["number"], "条标题": h["title"]}, "evidence": {"quote": h["raw"], "document": DOC}})
for h in annexes: add({"type": "Annex", "name": re.sub(r"\s+", "", h["text"]), "properties": {}, "evidence": {"quote": h["raw"], "document": DOC}})
# hierarchy relations
nums = {h["number"]: h for h in sections + subclauses if h["number"]}
for h in sections: link("Standard", SN, "hasStructuralElement", "Section", f'{h["number"]} {h["title"]}', h["raw"], DOC)
for h in subclauses:
    parts = str(h["number"]).split("."); parent = None
    for cut in range(len(parts) - 1, 0, -1):
        if ".".join(parts[:cut]) in nums: parent = nums[".".join(parts[:cut])]; break
    if parent:
        pt = "Section" if "." not in str(parent["number"]) else "Subclause"
        link(pt, f'{parent["number"]} {parent["title"]}', "hasSubClause", "Subclause", f'{h["number"]} {h["title"]}', h["raw"], DOC)

# annex subclauses (A.1 / B.2.3 ...) attach to their Annex
for h in subclauses:
    if re.match(r"^[A-Z](?:\.\d+)+$", str(h["number"])):
        letter = str(h["number"]).split(".")[0]
        annex = next((a for a in annexes if re.search(rf"附\s*录\s*{letter}", a["text"])), None)
        if annex:
            link("Annex", re.sub(r"\s+", "", annex["text"]), "hasSubClause", "Subclause",
                 f'{h["number"]} {h["title"]}', h["raw"], DOC)

# ---------- elements ----------
required = {"范围", "规范性引用文件", "术语和定义"}
def element(h, kind):
    scope = first_sentence(body_of(h)) or h["raw"]
    ename = re.sub(r"\s+", "", h["title"])
    add({"type": kind, "name": ename, "properties": {"要素状态": "必备" if kind == "Normative Element" else "可选", "作用范围": scope}, "evidence": {"quote": h["raw"], "document": DOC}})
    link("Standard", SN, "hasNormativeElement", kind, ename, h["raw"], DOC)
for h in sections: element(h, "Normative Element")
for h in infos: element(h, "Informative Element")
for h in annexes:
    kind = "Informative Element" if "资料性" in h["text"] else "Normative Element"
    element(h, kind)
# strict GB/T 1.1 states
for e in ENTS:
    if e["type"] in {"Normative Element", "Informative Element"}:
        e["properties"]["要素状态"] = "必备" if e["name"] in required else "可选"

# ---------- clauses ----------
def ctype(body):
    if re.search(r"不应|不得|应(?:当|该|能|予|按)", body): return "要求型"
    if "宜" in body: return "推荐型"
    if re.search(r"(?<!不)可(?:以|按|采用|使用|设置|选)", body): return "允许型"
    if re.search(r"说明|指明|给出|表示", body): return "指示型"
    return "陈述型"
def cconstraint(body):
    if re.search(r"应符合|符合.{0,12}规定|按规定", body): return "合规约束"
    if re.search(r"不低于|不超过|不大于|极差|容量|性能|质量", body): return "质量约束"
    if "安全" in body: return "安全约束"
    if re.search(r"期限|时间|日期|时限", body): return "时间约束"
    if re.search(r"权限|授权", body): return "权限约束"
    if re.search(r"流程|程序|步骤", body): return "流程约束"
    return ""
CL = {}
def make_clause(name, body, quote):
    cid = "IU-" + re.sub(r"[^\w.]+", "-", name)[:24]
    c = {"type": "Clause", "name": name, "properties": {"唯一标识符": cid, "内容描述": first_sentence(lead_before_list(body)), "条款类型": ctype(body), "约束类型": cconstraint(body)}, "evidence": {"quote": quote, "document": DOC}}
    add(c); CL[norm(name)] = c; return c
for h in sections:
    c = make_clause(f'{h["number"]} {h["title"]}条款', own_body(h), h["raw"])
    link("Section", f'{h["number"]} {h["title"]}', "hasClause", "Clause", c["name"], h["raw"], DOC)
for h in subclauses:
    if h["number"].startswith("3."): continue
    c = make_clause(f'{h["number"]} {h["title"]}条款', own_body(h), h["raw"])
    link("Subclause", f'{h["number"]} {h["title"]}', "hasClause", "Clause", c["name"], h["raw"], DOC)
for h in infos:
    if norm(h["text"]) == "参考文献": continue
    ename = re.sub(r"\s+", "", h["text"])
    c = make_clause(f'{ename}条款', own_body(h), h["raw"])
    link("Informative Element", ename, "hasClause", "Clause", c["name"], h["raw"], DOC)
for h in annexes:
    ename = re.sub(r"\s+", "", h["text"])
    c = make_clause(f'{ename}条款', own_body(h), h["raw"])
    kind = "Informative Element" if "资料性" in h["text"] else "Normative Element"
    link(kind, ename, "hasClause", "Clause", c["name"], h["raw"], DOC)
    link("Clause", c["name"], "hasRepresentationForm", "Annex", ename, h["raw"], DOC)

# ---------- explicit list items (D5=A) ----------
LETTER = re.compile(r"^\s*([a-z])[）)]\s*(.+?)\s*$", re.M)
DASH = re.compile(r"^\s*——\s*(.+?)\s*$", re.M)
derived = 0
dashed = 0
for h, ptype in ([(x, "Section") for x in sections] + [(x, "Subclause") for x in subclauses]
                 + [(x, "Informative Element") for x in infos] + [(x, "Normative Element") for x in annexes]):
    body = own_body(h)
    key = h["number"] if h["number"] else re.sub(r"\s+", "", h["text"])
    parent_label = f'{h["number"]} {h["title"]}' if h["number"] else re.sub(r"\s+", "", h["text"])
    parent_clause = f"{parent_label}条款"
    for letter, item in LETTER.findall(body):
        item = clean(item)
        if not item: continue
        name = f"{key}.{letter} {short_name(item)}条款"
        c = {"type": "Clause", "name": name,
             "properties": {"唯一标识符": "IU-" + re.sub(r"[^\w.]+", "-", name)[:24], "内容描述": item,
                            "条款类型": ctype(item), "约束类型": cconstraint(item)},
             "evidence": {"quote": item, "document": DOC}}
        if add(c): link(ptype, parent_label, "hasClause", "Clause", name, item, DOC); derived += 1
    for item in DASH.findall(body):
        item = clean(item)
        if not item: continue
        name = f"{key}：{short_name(item)}"
        extra = {"type": "Additional Information", "name": name,
                 "properties": {"唯一标识符": "AI-" + re.sub(r"[^\w.]+", "-", name)[:24], "内容描述": item},
                 "evidence": {"quote": item, "document": DOC}}
        if add(extra):
            note_holder = next((e for e in ENTS if e["type"] == "Clause" and e["name"] == parent_clause), None)
            if note_holder: link("Clause", note_holder["name"], "hasNote", "Additional Information", name, item, DOC)
            else: link(ptype, parent_label, "hasClause", "Clause", parent_clause, h["raw"], DOC) if False else None
            dashed += 1

# ---------- terminology (3.x) ----------
old_terms = [e for e in OLD["entities"] if e.get("type") == "Terminology"]
for e in old_terms: add(dict(e))
for h in terms:
    definition = first_sentence(body_of(h), ) if body_of(h) else ""
    m = TERM_SPLIT.match(h["title"])
    if m:
        term, syn = clean(m.group(1)), clean(m.group(2) or "")
        add({"type": "Terminology", "name": term, "properties": {"术语标准名称": term, "术语标准定义": definition, "同义词": syn}, "evidence": {"quote": h["raw"], "document": DOC}})
created_terms = set()
for h in terms:
    m = TERM_SPLIT.match(h["title"])
    if m:
        created_terms.add(clean(m.group(1)))
for e in ENTS:
    if e["type"] == "Terminology" and e["name"] in created_terms:
        link("Normative Element", "术语和定义", "defines", "Terminology", e["name"])

# ---------- references ----------
old_std_nodes = [e for e in OLD["entities"] if e.get("type") == "Standard"]
for e in old_std_nodes:
    if e["name"] == old_std["name"]: continue
    n = f'规范性引用 {e["name"]}'
    add({"type": "ReferencedDocuments", "name": n, "properties": {"文件类型": "文献", "生效时间": "", "责任主体": ""}, "evidence": {"quote": (e.get("evidence") or {}).get("quote", ""), "document": (e.get("evidence") or {}).get("document", DOC)}})
    link("Standard", SN, "referencesExternalResource", "ReferencedDocuments", n)
    c2 = CL.get(norm("2 规范性引用文件条款"))
    if c2: link("Clause", c2["name"], "referencesExternalResource", "ReferencedDocuments", n)
old_refs = [e for e in OLD["entities"] if e.get("type") == "Reference"]
for e in old_refs: add(dict(e))
ref_iu = {"type": "InformationUnit", "name": "参考文献", "properties": {"唯一标识符": "IU-REF", "内容描述": first_sentence(next((body_of(h) for h in infos if norm(h["text"]) == "参考文献"), "")) or "参考文献"}, "evidence": {"quote": next((h["raw"] for h in infos if norm(h["text"]) == "参考文献"), ""), "document": DOC}}
add(ref_iu)
for e in ENTS:
    if e["type"] == "Reference": link("InformationUnit", "参考文献", "hasRepresentationForm", "Reference", e["name"])

# ---------- normative references (source section 2) ----------
def _section2_text():
    h = next((x for x in HS if str(x["number"]) == "2"), None)
    if not h: return ""
    end = len(TEXT)
    for o in HS:
        if o["start"] > h["start"]: end = o["start"]; break
    return TEXT[h["end"]:end]

def _gb_key(value):
    m = re.search(r"GB(?:/T)?\s*\d+(?:\.\d+)*", norm(value))
    return m.group(0) if m else None

_have_rd = {_gb_key(e["name"]) for e in ENTS if e["type"] == "ReferencedDocuments"}
_have_rd.discard(None)
_sec2 = _section2_text()
if _sec2:
    _c2 = CL.get(norm("2 规范性引用文件条款"))
    for _line in _sec2.splitlines():
        _line = clean(_line)
        _m = REF.search(_line)
        if not _m: continue
        _src = clean(_m.group(0))
        _key = _gb_key(_src)
        if _key and _key in _have_rd: continue
        _nname = ("规范性引用 " + _src)[:140]
        _e = add({"type": "ReferencedDocuments", "name": _nname,
                  "properties": {"文件类型": "标准", "生效时间": "", "责任主体": ""},
                  "evidence": {"quote": _line[:200], "document": DOC}})
        if not _e: continue
        if _key: _have_rd.add(_key)
        link("Standard", SN, "referencesExternalResource", "ReferencedDocuments", _nname)
        if _c2: link("Clause", _c2["name"], "referencesExternalResource", "ReferencedDocuments", _nname)

# ---------- semantic import (Activity/Property/Constrain/Subject/Object/External/Table/Equation) ----------
map_type = {"ActionClass": "Activity", "Property": "Property", "Constrain": "Constrain", "Subject of Standardization": "Subject of Standardization", "StandardizationObject": "StandardizationObject", "ExternalResource": "ExternalResource", "Table": "Table", "Equation": "Equation"}
sem = {t: [] for t in map_type}
for e in OLD["entities"]:
    t = e.get("type")
    if t in map_type and t not in {"Property", "Constrain", "Subject of Standardization", "StandardizationObject", "ExternalResource", "Table", "Equation", "ActionClass"}:
        pass
    if t in map_type:
        e2 = dict(e); e2["type"] = map_type[t]; add(e2); sem[t].append(e2)
# remap old clause name -> new clause name
def remap_clause(oldname):
    if oldname == "参考文献条款": return "参考文献"
    num = re.match(r"^(\d+(?:\.\d+)*)", oldname or "")
    if num:
        for c in ENTS:
            if c["type"] == "Clause" and c["name"].startswith(num.group(1) + " "): return c["name"]
    if "附录A" in (oldname or ""):
        return next((c["name"] for c in ENTS if c["type"] == "Clause" and c["name"].startswith("附录A")), None)
    return None
for r in OLD["relations"]:
    rt = r.get("rel_type"); fn, tn = r.get("from_name"), r.get("to_name")
    if rt not in {"describesAction", "hasCharacteristic", "specifiesCharacteristic", "imposesConstraint", "involvesObject", "referencesExternalResource", "usesTerm", "referencesClause", "defines"}:
        continue
    nf, nt = fn, tn
    if r.get("from_type") == "Clause":
        nf = remap_clause(fn)
        if not nf: continue
        if r.get("to_type") == "ActionClass": nt = tn
    if r.get("from_type") == "ActionClass": nt2 = tn
    nft, ntt = r.get("from_type"), r.get("to_type")
    if nft == "ActionClass": nft = "Activity"
    if ntt == "ActionClass": ntt = "Activity"
    if not any(e["type"] == nft and e["name"] == nf for e in ENTS): continue
    if not any(e["type"] == ntt and e["name"] == nt for e in ENTS): continue
    link(nft, nf, rt, ntt, nt, str((r.get("evidence") or {}).get("quote", "")), (r.get("evidence") or {}).get("document", ""))

# ---------- drafters / orgs ----------
def name_list(label):
    m = re.search(label + r"\s*[：:]\s*", TEXT)
    if not m: return []
    tail = re.split(r"\n#{1,6}\s", TEXT[m.end():m.end() + 3000])[0]
    tail = re.split(r"\n\s*\n", tail)[0]
    out = []
    for part in re.split(r"[、，,;；\n]", tail):
        n = clean(part).strip("。.]）) ").replace("（", "(").replace("）", ")")
        if len(n) >= 2 and n not in out and not (len(n) > 20 and addi(n, "起草", "本文件")): out.append(n)
    return out
for n in name_list(r"本文件起草单位"):
    add({"type": "Drafting Organization", "name": n, "properties": {"相关方名称": n, "统一信用代码": "", "所在地": ""}}); link("Standard", SN, "draftedBy", "Drafting Organization", n)
for n in name_list(r"本文件主要起草人"):
    add({"type": "Drafter", "name": n, "properties": {"姓名": n, "所属单位": "", "联系电话": "", "联系地址": ""}}); link("Standard", SN, "draftedBy", "Drafter", n)

# ---------- figure captions ----------
FIG = re.compile(r"^\s*图\s*([A-Z]?\.?\d+)\s+(.+?)\s*$", re.M)
figures = []
for m in FIG.finditer(TEXT):
    fid, title = m.group(1), clean(m.group(2))
    name = f"图{fid} {title}"
    if add({"type": "Figure", "name": name, "properties": {},
            "evidence": {"quote": m.group(0).strip(), "document": DOC}}):
        figures.append((fid, name))
for fid, name in figures:
    for c in [e for e in ENTS if e["type"] == "Clause"]:
        body = body_at(c.get("evidence", {}).get("quote", "")) if "body_at" in globals() else ""
        if not body:
            i = TEXT.find(c.get("evidence", {}).get("quote", ""))
            if i >= 0:
                j = len(TEXT)
                for h in HS:
                    if h["start"] > i: j = h["start"]; break
                body = TEXT[i:j]
        if re.search(rf"图\s*{re.escape(fid)}", body) or f"图{fid}" in body.replace(" ", ""):
            link("Clause", c["name"], "hasRepresentationForm", "Figure", name, body[:80], DOC); break

# ---------- table/equation form links ----------
def body_at(quote):
    i = TEXT.find(quote)
    if i < 0: return ""
    j = len(TEXT)
    for h in HS:
        if h["start"] > i: j = h["start"]; break
    return TEXT[i:j]
table_linked = equation_linked = False
for c in [e for e in ENTS if e["type"] == "Clause"]:
    body = body_at(c.get("evidence", {}).get("quote", "")) or c.get("properties", {}).get("内容描述", "")
    if not table_linked and "表1" in body:
        link("Clause", c["name"], "hasRepresentationForm", "Table", "表1 试验项目、样品数量及编号", body[:80], DOC); table_linked = True
    if not equation_linked and ("公式（1）" in body or "能量效率" in body):
        link("Clause", c["name"], "hasRepresentationForm", "Equation", "公式（1）能量效率", body[:80], DOC); equation_linked = True
linked_actions = {r["to_name"] for r in RELS if r["rel_type"] == "describesAction"}
for a in [e for e in ENTS if e["type"] == "Activity" and e["name"] not in linked_actions]:
    for c in [e for e in ENTS if e["type"] == "Clause"]:
        if a["name"] in body_at(c.get("evidence", {}).get("quote", "")):
            link("Clause", c["name"], "describesAction", "Activity", a["name"]); break

# ---------- write ----------
out = {"entities": ENTS, "relations": RELS, "profile": "unified_golden_v2", "source_case": CASE}
dest = ROOT / "extract/golden_v2.json"
dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
report = {"case": CASE, "entities": len(ENTS), "relations": len(RELS), "derived_list_clauses": derived, "additional_information_items": dashed,
          "types": dict(Counter(e["type"] for e in ENTS).most_common()),
          "relation_types": dict(Counter(r["rel_type"] for r in RELS).most_common()),
          "old_types": dict(Counter(e.get("type") for e in OLD["entities"]).most_common())}
(ROOT / "extract/golden_v2_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=1))