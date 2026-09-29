"""Debug: run the V2 pipeline with a stub LLM to see where properties are lost."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
while not (ROOT / ".git").exists() or not (ROOT / "pytest.ini").is_file():
    ROOT = ROOT.parent
    if ROOT == ROOT.parent:
        raise RuntimeError("无法定位仓库根目录")
sys.path.insert(0, str(ROOT))

from algorithm.knowledge_extract.v2 import extract_harness          # noqa: E402
from algorithm.knowledge_extract.v2.chunk_context import ExtractionChunk  # noqa: E402

ONTOLOGY = (ROOT / "algorithm/knowledge_extract/v2/uml/术语本体.puml").read_text(encoding="utf-8")

CHUNK = """## 采购标段数量
// 术语名称: 采购标段数量
// 术语定义（术语标准定义）: 发起采购项目、编制寻源方案过程中，可将一揽子采购对象分为多个标段/包，有确定的编号及名称。
// 同义词: 标段、包、采购标段数、标段数量
"""

# The three terms we pretend the model found, with the ontology terms they map to.
TERMS = [
    {
        "mention": "采购标段数量",
        "quote": "// 术语名称: 采购标段数量",
        "props": {
            "name": "采购标段数量",
            "definition": "发起采购项目、编制寻源方案过程中，可将一揽子采购对象分为多个标段/包，有确定的编号及名称。",
            "synonyms": ["标段", "包", "采购标段数", "标段数量"],
        },
    },
]


class StubResponse:
    def __init__(self, text):
        self.text = text

    def __str__(self):
        return self.text


class StubLLM:
    """Answers the entity pass with spans and the property pass with values."""

    def __init__(self):
        self.calls = []

    def generate_structured(self, schema, messages, context):
        user = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
        task = context.get("pass_name", "")
        self.calls.append(task)

        if task == "property":
            # A real model reads the candidate ids out of the prompt catalog.
            ids = schema["properties"]["updates"]["items"]["properties"]["entity_id"]["enum"]
            updates = []
            for entity_id, term in zip(ids, TERMS):
                updates.append(
                    {
                        "entity_id": entity_id,
                        "properties": term["props"],
                        "evidence": {"quote": term["quote"]},
                    }
                )
            return StubResponse(json.dumps({"updates": updates}, ensure_ascii=False))

        # entity pass: one span entity per term
        entities = []
        for term in TERMS:
            start = CHUNK.find(term["mention"])
            entities.append(
                {
                    "type": "Term",
                    "mention": term["mention"],
                    "start_char": start,
                    "end_char": start + len(term["mention"]),
                    "evidence": {"quote": term["quote"]},
                }
            )
        return StubResponse(json.dumps({"entities": entities}, ensure_ascii=False))


def main():
    stub = StubLLM()
    result = extract_harness(
        ontology=ONTOLOGY,
        chunks=[ExtractionChunk(chunk_id="chunk-0", text=CHUNK)],
        llm=stub,
        use_scheduler=False,
    )
    print("=== passes called ===", stub.calls)
    print("=== status ===", result.get("status"), result.get("error_code"))
    print("=== _error ===", str(result.get("_error") or result.get("error"))[:300])
    print("=== attempts ===")
    for attempt in result.get("attempts") or []:
        print("  ", json.dumps(attempt, ensure_ascii=False)[:400])
    print("=== profile ===", json.dumps(result.get("profile"), ensure_ascii=False)[:600])
    print("=== property_enrichment ===", result.get("property_enrichment"))
    print("=== raw entities ===")
    print(json.dumps((result.get("raw") or {}).get("entities"), ensure_ascii=False, indent=2))
    print("=== warnings (filtered) ===")
    for w in result.get("warnings") or []:
        if "PROPERTY" in str(w.get("code", "")):
            print(" ", json.dumps(w, ensure_ascii=False))

    from algorithm.knowledge_extract.v2.result_contract import to_public_result
    print("=== public result ===")
    print(json.dumps(to_public_result(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
