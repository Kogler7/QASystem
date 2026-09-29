"""Scratch check: does the main extraction flow now go through Knowledge Extract V2?

Runs without langchain/openai because the adapter imports V2 lazily.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
while not (ROOT / ".git").exists() or not (ROOT / "pytest.ini").is_file():
    ROOT = ROOT.parent
    if ROOT == ROOT.parent:
        raise RuntimeError("无法定位仓库根目录")
sys.path.insert(0, str(ROOT))

from backend.app.services import knowledge_extract_adapter as adapter  # noqa: E402


def make_result(status="succeeded", error_code=None, terminal=None):
    """A V2-shaped harness result: two entities, one relation, anchored to chunks."""
    attempts = []
    if terminal:
        attempts.append(
            {
                "attempt": 1,
                "type": terminal[0],
                "message": terminal[2],
                "pass": terminal[3] if len(terminal) > 3 else "entity",
                "chunk": terminal[1],
                "terminal": True,
            }
        )
    return {
        "status": status,
        "error_code": error_code,
        "attempts": attempts,
        "warnings": [],
        "profile": {"document_route": {"kind": "generic"}},
        "raw": {"entities": [], "relations": []},
        "graph": {
            "nodes": [
                {"uid": "Term::采购标段数量", "label": "Term", "properties": {"name": "采购标段数量"}},
                {"uid": "Term::定标金额", "label": "Term", "properties": {"name": "定标金额"}},
            ],
            "relationships": [
                {"from_uid": "Term::采购标段数量", "to_uid": "Term::定标金额", "type": "RELATED_TO"}
            ],
        },
        "intermediate_model": {
            "objects": [
                {
                    "id": "entity-0",
                    "type": "Term",
                    "identity": "采购标段数量",
                    "display_name": "采购标段数量",
                    "properties": {},
                    "evidence": [{"source_block_ids": ["chunk-0000"], "quote": "采购标段数量"}],
                },
                {
                    "id": "entity-1",
                    "type": "Term",
                    "identity": "定标金额",
                    "display_name": "定标金额",
                    "properties": {},
                    "evidence": [{"source_block_ids": ["chunk-0001"], "quote": "定标金额"}],
                },
            ],
            "relations": [{"id": "rel-0", "type": "RELATED_TO", "from": "entity-0", "to": "entity-1"}],
        },
    }


class Recorder:
    """Stands in for extract_harness and records exactly what it is handed."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def check_wiring():
    recorder = Recorder(make_result())
    texts = ["## 采购标段数量\n正文", "## 定标金额\n正文"]
    items = adapter.extract_chunks(
        "@startuml\nclass Term\n@enduml", texts, runner=recorder
    )

    call = recorder.calls[0]
    chunks = call["chunks"]
    print("=== 传给 V2 的参数 ===")
    print("  runner kwargs:", sorted(call))
    print("  validation_mode:", call["validation_mode"])
    print("  best_effort    :", call["best_effort"])
    print("  chunk 类型     :", type(chunks[0]).__module__ + "." + type(chunks[0]).__name__)
    print("  chunk_id       :", [c.chunk_id for c in chunks])
    print("  chunk 文本     :", [c.text for c in chunks])
    print("  文本保留下 #   :", all(c.text.startswith("##") for c in chunks))

    print("\n=== 投影回 per-chunk 图 ===")
    for item in items:
        print(
            f"  item[{item['index']}] nodes={[n['uid'] for n in item['graph']['nodes']]}"
            f" rels={len(item['graph']['relationships'])} error={item['error']}"
        )

    merged = adapter.__dict__  # noqa: F841 - merged graph rebuilt by V1's dedup below
    from algorithm.knowledge_extract.v1 import deduplicate_chunk_graphs

    merged = deduplicate_chunk_graphs(items)
    print("\n=== 合并图（经既有 deduplicate_chunk_graphs） ===")
    for node in merged["nodes"]:
        print(f"  {node['uid']}  chunk_indices={node.get('chunk_indices')}")
    print("  关系数:", len(merged["relationships"]))


def check_failure_attribution():
    print("\n=== 失败归因 ===")
    cases = [
        ("成功", make_result()),
        ("chunk-0001 失败", make_result("failed", "LLM_REQUEST_FAILED", ("request", "chunk-0001", "请求失败"))),
        ("全局失败(chunk=final)", make_result("failed", "LLM_SCHEMA_INVALID", ("schema_validation", "final", "ontology 约束", "verification"))),
        ("带 batch 后缀", make_result("failed", "LLM_TIMEOUT", ("timeout", "chunk-0000:batch-1", "LLM 请求超时", "property"))),
    ]
    for label, result in cases:
        recorder = Recorder(result)
        items = adapter.extract_chunks("o", ["a", "b"], runner=recorder)
        failed = [i["index"] for i in items if i["error"]]
        codes = {i["error_code"] for i in items if i["error_code"]}
        print(f"  {label:24s} -> failed_positions={failed} codes={codes or None}")


def check_runner_raises():
    def boom(**kwargs):
        raise RuntimeError("provider exploded")

    items = adapter.extract_chunks("o", ["a", "b"], runner=boom)
    print("\n=== runner 抛异常 ===")
    print("  数量:", len(items), "error_code:", {i["error_code"] for i in items})


if __name__ == "__main__":
    check_wiring()
    check_failure_attribution()
    check_runner_raises()
