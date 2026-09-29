"""Cross-check unified golden_v2 against the source markdown."""
from __future__ import annotations
import json, re, sys
from collections import Counter
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

_DOCNAME, TEXT = load_case_text()

HEAD = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$", re.M)
NUM = re.compile(r"^(\d+(?:\.\d+)*)\s+(.+?)\s*$")
LETTER = re.compile(r"^\s*([a-z])[）)]\s*(.+?)\s*$", re.M)
DASH = re.compile(r"^\s*——\s*(.+?)\s*$", re.M)
def norm(s): return re.sub(r"\s+", "", str(s or ""))
def clean(s): return re.sub(r"\s+", " ", str(s or "")).strip()
def parse(text):
    hs = []
    for m in HEAD.finditer(text):
        b = m.group(2).strip(); nm = NUM.match(b)
        hs.append({"raw": m.group(0).strip(), "text": b, "number": nm.group(1) if nm else None,
                   "title": nm.group(2).strip() if nm else clean(b), "start": m.start()})
    return text, hs
_, HS = parse(TEXT)

def section_body(title_norm):
    for i, h in enumerate(HS):
        if norm(h["text"]) == title_norm or norm(h["text"]).startswith(title_norm):
            end = HS[i + 1]["start"] if i + 1 < len(HS) else len(TEXT)
            return TEXT[h["start"]:end]
    return ""
quotes = {clean(e.get("evidence", {}).get("quote", "")) for e in E}
names = {norm(e["name"]) for e in E}
report = {"case": CASE, "entities": len(E), "relations": len(R), "checks": {}}

# 1) headings
skip = {"目次", "中华人民共和国国家标准"}
uncovered = []
for h in HS:
    n = norm(h["text"])
    if any(s in n for s in skip): continue
    if any(q and (q in h["raw"] or h["raw"] in q) for q in quotes): continue
    if h["number"] and any(nm.startswith(norm(h["number"]) + " ") or norm(h["number"]) == nm.split()[0] if nm else False for nm in names): continue
    if any(n in nm or nm in n for nm in names if len(n) >= 3): continue
    uncovered.append(h["text"][:50])
report["checks"]["headings"] = {"total": len([h for h in HS if not any(s in norm(h["text"]) for s in skip)]), "uncovered": uncovered}

# 2) explicit list items
letters = [clean(m.group(2)) for m in LETTER.finditer(TEXT)]
dashes = [clean(m.group(1)) for m in DASH.finditer(TEXT)]
def covered(item): return any(item and (item in v or v in item) for v in (quotes | names))
report["checks"]["list_items"] = {"a_b_c": len(letters), "a_b_c_uncovered": [x[:40] for x in letters if not covered(x)][:10],
                                  "dash": len(dashes), "dash_uncovered": [x[:40] for x in dashes if not covered(x)][:10]}

# 3) references
ref_scope = section_body("2规范性引用文件") + section_body("参考文献")
refs = [clean(x) for x in re.findall(r"GB(?:/T)?\s*\d+(?:\.\d+)*\s*(?:[—–-]\s*\d{4})?[^\n]*", ref_scope)]
def gbkey(value):
    m = re.search(r"GB(?:/T)?\s*\d+(?:\.\d+)*", norm(value))
    return m.group(0) if m else None
ref_names = [norm(e["name"]) for e in E if e["type"] in {"ReferencedDocuments", "Reference"}]
ref_keys = {gbkey(e["name"]) for e in E if e["type"] in {"ReferencedDocuments", "Reference"}}
ref_keys.discard(None)
def ref_covered(x):
    nx = norm(x)
    return any(nx in n or n in nx for n in ref_names) or (gbkey(x) in ref_keys)
report["checks"]["references"] = {"scope": "2规范性引用文件 + 参考文献", "total": len(refs),
                                  "uncovered": [x[:60] for x in refs if not ref_covered(x)]}

# 4) captions
import unicodedata
def nfk(s):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s or "")))
caps = [nfk(x) for x in re.findall(r"(?:图\s*[A-Z]?\.?\d+|表\s*\d+|公式\s*[（(]?\d+[）)]?)", TEXT)]
cap_names = [nfk(e["name"]) for e in E if e["type"] in {"Figure", "Table", "Equation"}]
report["checks"]["captions"] = {"total": len(set(caps)), "Figure": len([e for e in E if e["type"] == "Figure"]),
                                "uncovered": sorted({x for x in caps if not any(x in n for n in cap_names)})[:10]}

# 5) drafters/orgs
def name_list(label):
    m = re.search(label + r"\s*[：:]\s*", TEXT)
    if not m: return []
    tail = re.split(r"\n#{1,6}\s", TEXT[m.end():m.end() + 3000])[0]
    tail = re.split(r"\n\s*\n", tail)[0]
    out = []
    for part in re.split(r"[、，,;；\n]", tail):
        n = clean(part).strip("。.]）) ").replace("（", "(").replace("）", ")")
        if len(n) >= 2 and n not in out and not (len(n) > 20 and re.search(r"起草|本文件", n)): out.append(n)
    return out
for label, typ in ((r"本文件起草单位", "Drafting Organization"), (r"本文件主要起草人", "Drafter")):
    lst = name_list(label); ens = {norm(e["name"]) for e in E if e["type"] == typ}
    report["checks"][typ] = {"source": len(lst), "entities": len(ens), "uncovered": [x for x in lst if norm(x) not in ens][:10]}

# 6) terms
term_heads = [h for h in HS if h["number"] and h["number"].startswith("3.")]
term_names = {norm(e["name"]) for e in E if e["type"] == "Terminology"}
sub_names = {norm(e["name"]) for e in E if e["type"] == "Subclause"}
report["checks"]["terminology"] = {"source": len(term_heads), "terminology": len(term_names), "subclause": len(sub_names),
                                   "missing_subclause": [f'{h["number"]} {h["title"]}' for h in term_heads if norm(f'{h["number"]} {h["title"]}') not in sub_names][:10]}

# 7) standard metadata
std = next((e for e in E if e["type"] == "Standard"), {})
props = std.get("properties", {})
report["checks"]["standard"] = {"name": std.get("name", ""), "props": sorted(props), "has_ics": any(e["type"] == "InternationalClassificationofStandard" for e in E),
                                 "has_ccs": any(e["type"] == "ChineseClassificationofStandard" for e in E),
                                 "has_stage": any(e["type"] == "Approval and PublicationStage" for e in E),
                                 "has_drafters": any(e["type"] == "Drafter" for e in E)}

(ROOT / "extract/golden_v2_check.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=1))