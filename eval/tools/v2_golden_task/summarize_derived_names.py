"""Summarize derived Clause/Additional Information names; keep verbatim content in 内容描述."""
from __future__ import annotations
import json, os, re, sys
from concurrent.futures import ThreadPoolExecutor
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
def clean(s): return re.sub(r"\s+", "", str(s or ""))

def build_client():
    from eval.legacy import v2_benchmark_eval as ev
    return ev._build_judge_client(model=ev._DEFAULT_MODEL, base_url=ev._DEFAULT_BASE_URL, api_key=ev._DEFAULT_API_KEY, max_tokens=None)

SYSTEM = """你是标准条款命名器。为每条给定内容生成一个概括短标题（<=12个汉字），用于实体名字。
要求：忠实概括，不引入新事实；不要照抄长句；只输出 JSON。"""
SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "id": {"type": "string"}, "title": {"type": "string"}}, "required": ["id", "title"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False}

items = []
for i, e in enumerate(E):
    name = e["name"]
    m = re.match(r"^(.+?)\.([a-z]) .+条款$", name)
    if m and e["type"] == "Clause":
        items.append({"id": f"c{i}", "parent": m.group(1), "letter": m.group(2), "kind": "clause",
                      "text": e.get("properties", {}).get("内容描述", "")[:260]})
    elif e["type"] == "Additional Information" and "：" in name:
        items.append({"id": f"a{i}", "parent": name.split("：", 1)[0], "letter": "", "kind": "extra",
                      "text": e.get("properties", {}).get("内容描述", "")[:260]})

def run_batch(batch):
    try:
        resp = build_client().generate_structured(schema=SCHEMA,
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": json.dumps({"items": batch}, ensure_ascii=False)}],
            run_context={"schema_name": "derived_titles", "response_mode": "json_schema"})
        raw = getattr(resp, "text", resp)
        data = json.loads(raw) if isinstance(raw, str) else raw
        out = {}
        if isinstance(data, dict):
            for x in data.get("items") or []:
                if isinstance(x, dict) and x.get("id"): out[str(x["id"])] = clean(x.get("title"))[:12]
        elif isinstance(data, list):
            for x in data:
                if isinstance(x, dict) and x.get("id"): out[str(x["id"])] = clean(x.get("title"))[:12]
        return out
    except Exception:
        return {}

titles = {}
batches = [items[i:i + 5] for i in range(0, len(items), 5)]
with ThreadPoolExecutor(max_workers=4) as ex:
    for out in ex.map(run_batch, batches):
        titles.update(out)
missing = [it for it in items if it["id"] not in titles or not titles.get(it["id"])]
if missing:
    with ThreadPoolExecutor(max_workers=2) as ex:
        for out in ex.map(run_batch, [missing[i:i + 3] for i in range(0, len(missing), 3)]):
            titles.update(out)
def fallback_title(it):
    text = clean(it.get("text", ""))
    if it.get("kind") == "extra":
        t = re.sub(r"^(更改了|增加了|删除了|合并了|本次为|2015年首次发布为|首次发布为)", "", text)
        t = re.sub(r"（.*$", "", t)
        t = t.strip("，,。；;：: ")
        return t[:10] or text[:8]
    return re.split(r"[，,；;：:]", text)[0][:10]
for it in items:
    if it["id"] not in titles or not titles[it["id"]]:
        titles[it["id"]] = fallback_title(it)

renames = {}
for i, e in enumerate(E):
    key = None
    if e["type"] == "Clause":
        m = re.match(r"^(.+?)\.([a-z]) .+条款$", e["name"])
        if m: key = f"c{i}"
    elif e["type"] == "Additional Information" and "：" in e["name"]:
        key = f"a{i}"
    if not key or key not in titles or not titles[key]: continue
    title = titles[key]
    if e["type"] == "Clause":
        m = re.match(r"^(.+?)\.([a-z]) .+条款$", e["name"]); new = f"{m.group(1)}.{m.group(2)} {title}条款"
    else:
        new = f"{e['name'].split('：', 1)[0]}：{title}"
    old = e["name"]
    if new != old:
        e["name"] = new
        e.setdefault("properties", {})["唯一标识符"] = ("AI-" if e["type"] == "Additional Information" else "IU-") + re.sub(r"[^\w.]+", "-", new)[:24]
        renames[old] = new

for r in R:
    if r.get("from_name") in renames: r["from_name"] = renames[r["from_name"]]
    if r.get("to_name") in renames: r["to_name"] = renames[r["to_name"]]
keys = [(e["type"], e["name"]) for e in E]
dups = {k for k in keys if keys.count(k) > 1}
(ROOT / "extract/golden_v2.json").write_text(json.dumps(G, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps({"case": CASE, "renamed": len(renames), "llm_titles": len(titles), "duplicates": list(dups)[:5],
                  "samples": list(renames.items())[:8]}, ensure_ascii=False, indent=1))