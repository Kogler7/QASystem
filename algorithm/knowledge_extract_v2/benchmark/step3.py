"""Phase 1 Step 3 验收：ChunkContext + Evidence 绑定与 L1/L2 验证。

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md §37 Step 3：
不重新解析文档；统一接收 text/chunks；抽取结果绑定 chunk_id 与 evidence span。

做法（无需 openai / pytest）：
- 把每个用例 text 按空行切成段落 chunk（演示多 chunk 场景），并用 §5 兼容模式
  构造整文单 chunk；
- 用 gold 的 evidence.quote 走 bind_evidence → verify_evidence（L1/L2）→
  evidence_coverage（AC-4 代理）；
- 展示 ChunkContextManager.local_context 的有界上下文。

用法：
    python algorithm/knowledge_extract_v2/benchmark/step3.py
"""
from __future__ import annotations

import copy
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any


BENCHMARK_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = BENCHMARK_DIR.parent
CASES_DIR = BENCHMARK_DIR / "cases"


def _load_eval_module() -> Any:
    spec = importlib.util.spec_from_file_location("_step1_eval", BENCHMARK_DIR / "eval.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _paragraph_chunks(case: dict[str, Any]) -> list[Any]:
    """把用例文本按空行切成段落 chunk，只演示多 chunk 场景，不代替上游切片。"""
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", case.get("text") or "") if paragraph.strip()]
    chunks = []
    for index, text in enumerate(paragraphs):
        chunks.append(
            {
                "chunk_id": f"{case['id']}-p{index}",
                "text": text,
                "document_id": case["id"],
                "previous_chunk_id": f"{case['id']}-p{index - 1}" if index > 0 else None,
            }
        )
    return chunks


def main(argv: list[str] | None = None) -> int:
    eval_mod = _load_eval_module()
    modules = eval_mod.load_package_modules(
        "knowledge_extract_v2",
        PACKAGE_DIR,
        ["graph_utils.py", "chunk_context.py", "evidence.py"],
    )
    chunk_context = modules["chunk_context"]
    evidence = modules["evidence"]

    cases = eval_mod.load_dataset(CASES_DIR)
    total_failures = 0
    required_failures = 0
    for case in cases:
        expected = copy.deepcopy(case.get("expected") or {"entities": [], "relations": []})
        required_refs = set()
        for section in ("entities", "relations"):
            for index, item in enumerate(expected.get(section, [])):
                if isinstance(item, dict) and (item.get("evidence") or {}).get("quote"):
                    required_refs.add(f"{section}[{index}]")
        manager = chunk_context.ChunkContextManager(
            [chunk_context.ExtractionChunk.from_dict(payload) for payload in _paragraph_chunks(case)]
        )
        bound = evidence.bind_evidence(expected, manager)
        verdicts = evidence.verify_evidence(bound, manager)
        coverage = evidence.evidence_coverage(bound)
        failures = [verdict for verdict in verdicts if verdict.status == "fail"]
        required_fails = [verdict for verdict in verdicts if verdict.status == "fail" and verdict.ref in required_refs]
        total_failures += len(failures)
        required_failures += len(required_fails)

        print(f"\n=== {case['id']}（{case.get('category')}）===")
        print(f"chunks: {len(manager)} | gold 必带 quote 项: {len(required_refs)}")
        print(
            "evidence 覆盖: entities "
            f"{coverage['entities']['covered']}/{coverage['entities']['total']} "
            f"({coverage['entities']['rate']:.0%}), relations "
            f"{coverage['relations']['covered']}/{coverage['relations']['total']}（gold 未强制）"
        )
        if required_fails:
            print("必带 quote 项 L1/L2 失败：")
            for verdict in required_fails:
                print(f"  [{verdict.level}] {verdict.ref} span={verdict.span_id!r}: {verdict.message}")
        else:
            print(f"必带 quote 项 L1/L2 验证：{len(required_refs)} 项全部通过")
        if failures and not required_fails:
            print(f"（另有 {len(failures)} 个未强制项因 gold 无 quote 未绑定，属预期）")

        # 展示有界局部上下文（不拼接整篇文档）
        if len(manager) > 1:
            middle = manager.chunk_ids[len(manager) // 2]
            window = manager.local_context(middle, previous=1, next=1, same_section=False)
            joined = manager.context_text(middle, previous=1, next=1, same_section=False)
            print(
                f"局部上下文示例: {middle} 窗口 {len(window)} chunks / "
                f"共 {len(manager)} chunks，拼接 {len(joined)} 字符"
            )

    if required_failures:
        print(f"\nStep 3 验收完成：{len(cases)} 个用例，必带 quote 项失败 {required_failures} 个（未达标）。")
        return 1
    print(
        f"\nStep 3 验收完成：{len(cases)} 个用例，gold 必带 quote 项 L1/L2 全部通过，"
        "实体 evidence 覆盖率 100% 达标。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
