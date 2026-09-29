import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape
from xml.etree import ElementTree as ET


FIXTURES = Path(__file__).resolve().parent / "fixtures"
MD_PATH = FIXTURES / "terminology.md"
DOCX_PATH = FIXTURES / "terminology.docx"
ONTOLOGY_PATH = FIXTURES / "ontology.puml"


GENERIC_INSTRUCTION = """## 抽取规则

你是知识图谱抽取专家。上方"本体定义"中的 PlantUML 类图就是你唯一的抽取 schema，请严格按本体定义抽取：

### 实体抽取
- 本体中每个 `class Xxx <<Stereotype>>` 定义一个实体类型，类名即为实体 `type`
- 类体中每个 `+属性名: 类型 // 注释` 即为该实体的属性字段，从文档中提取对应值填入 `properties`
- 属性类型标注为 `String[]` 的用 JSON 数组表示
- 文档中未提及的属性设为 null 或不包含在 properties 中
- 可以根据文档结构（## 标题、// 段落等）识别内容边界，将对应内容映射到本体属性

### 输出要求
- 只输出 JSON，不要任何解释、markdown 代码块或额外文字
- 不要自创本体中不存在的类型、属性或关系名
- 即使文档中某些字段缺失，也不要编造值"""


def ensure_ontology() -> str:
    ontology = """@startuml
class 术语 <<Terminology>> {
 + 术语标准名称 (TermName): String //术语的标准名称，用于统一业务表达，例如“采购标段数量”
 + 术语标准定义 (TermDefinition): String //术语的标准业务定义，用于说明该术语在业务场景中的准确含义
 + 同义词 (Synonyms): String[] //术语的同义表达、简称、别称或口语化表达，例如“标段、包、采购标段数、标段数量”
}
@enduml
"""
    ONTOLOGY_PATH.write_text(ontology, encoding="utf-8")
    return ontology


def build_prompt(ontology: str, text: str) -> str:
    return f"""你是一个知识图谱抽取专家。请根据以下本体定义，从文本中抽取实体和关系。

## 本体定义
{ontology}

## 实体标识规则
所有类型默认用 name 属性作为 identity 标识

{GENERIC_INSTRUCTION}

## 待抽取文本
{text}

## 输出格式
{{
  "entities": [
    {{
      "type": "本体中定义的类名",
      "name": "实体标识值",
      "properties": {{ "属性名": "属性值", ... }}
    }}
  ],
  "relations": []
}}"""


def extract_json(response: str) -> dict:
    response = (response or "").strip()
    if response.startswith("```"):
        response = re.sub(r"^```[a-zA-Z]*\s*", "", response)
        response = re.sub(r"\s*```$", "", response)
    start = response.find("{")
    end = response.rfind("}")
    if start >= 0 and end > start:
        response = response[start : end + 1]
    return json.loads(response)


def to_graph(extracted: dict) -> dict:
    nodes = []
    seen = set()
    for entity in extracted.get("entities", []):
        etype = entity.get("type") or "Element"
        name = entity.get("name") or ""
        props = entity.get("properties") or {}
        uid = f"{etype}::{name.lower()}"
        if uid in seen:
            continue
        seen.add(uid)
        nodes.append({"uid": uid, "label": etype, "properties": {"name": name, **props}})
    return {"nodes": nodes, "relationships": []}


def call_llm(prompt: str) -> dict:
    base_url = os.environ["TEST_LLM_BASE_URL"].rstrip("/")
    api_key = os.environ["TEST_LLM_API_KEY"]
    model = os.environ["TEST_LLM_MODEL"]
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个知识图谱抽取专家。严格按照本体定义从文本中抽取实体和关系。只输出JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 8192,
    }
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM HTTP {exc.code}: {body[:1000]}") from exc
    content = data["choices"][0]["message"]["content"]
    return extract_json(content)


def md_to_docx(md_text: str) -> None:
    paragraphs = [line.rstrip() for line in md_text.splitlines()]

    def p(text: str) -> str:
        return f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'

    document_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    {"".join(p(x) for x in paragraphs)}
    <w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>
  </w:body>
</w:document>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>'''
    rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''
    with zipfile.ZipFile(DOCX_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document_xml)


def docx_to_text(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml")
    root = ET.fromstring(xml)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    lines = []
    for para in root.findall(".//w:p", ns):
        text = "".join(t.text or "" for t in para.findall(".//w:t", ns))
        lines.append(text)
    return "\n".join(lines)


def run_case(name: str, text: str, ontology: str) -> dict:
    prompt = build_prompt(ontology, text)
    extracted = call_llm(prompt)
    graph = to_graph(extracted)
    result = {
        "case": name,
        "text_len": len(text),
        "entities": len(extracted.get("entities", [])),
        "nodes": len(graph["nodes"]),
        "first_entities": extracted.get("entities", [])[:3],
    }
    return result


def main() -> int:
    missing = [k for k in ("TEST_LLM_BASE_URL", "TEST_LLM_API_KEY", "TEST_LLM_MODEL") if not os.environ.get(k)]
    if missing:
        print("missing env:", ", ".join(missing), file=sys.stderr)
        return 2

    ontology = ensure_ontology()
    md_text = MD_PATH.read_text(encoding="utf-8")
    md_to_docx(md_text)
    docx_text = docx_to_text(DOCX_PATH)

    # Limit text size for fast local smoke test while still covering multiple terms.
    md_result = run_case("md", md_text[:6000], ontology)
    docx_result = run_case("docx_text", docx_text[:6000], ontology)

    out = {
        "ontology_path": str(ONTOLOGY_PATH),
        "md_path": str(MD_PATH),
        "docx_path": str(DOCX_PATH),
        "results": [md_result, docx_result],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
