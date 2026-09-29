"""文档路由改造（强信号多标签 + 按文档分段）单元测试。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract_v2/tests/test_document_route_multilabel.py

覆盖：
- 强信号修复：CJK 相邻标准号（无空格）、企业公文标题、文号；
- 信号规范化：全角/∕/⁄ 斜杠变体与全角数字；
- 段内采信阈值：文档头锚定、正文单次引用不采信、多命中佐证；
- 多标签判定与 kind 优先级（official_document > standard）；
- 单域本体偏好（source="ontology"）与中性/双域本体回退 generic；
- 按文档分段路由（document_id 边界 + "## 文件:" 批量头，含 chunk 中部头）；
- 聚合路由：labels 并集 / kind 优先级 / source 优先级；
- prompt_extension 多标签拼接与 build_chat_messages 的 labels 行；
- 标准补全解耦：双标签时 kind=official_document 仍触发 enrich_standard_semantics；
- §10 表格 JSON 化（columns/rows）后的识别：记录表策略、表单抑制规则、
  标准「表N 标题」的确定性表实体、候选结构支撑；
- 500-700 字符生产切块：批量头/纯标题 chunk 不再顶替文档头锚点，正文引用仍不采信；
- 真实解析+切片管线用例（缺 langchain_core 时明确 skip）：
  knowledge_pipeline.chunk_document_items -> ExtractionChunk -> enrich_chunk_metadata
  -> resolve_chunk_routes，覆盖真实公文/标准/批量拼接/表单产物与 JSON 保护切片。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_DIR.parents[1]
FIXTURES_DIR = PACKAGE_DIR / "tests" / "fixtures"
ONTOLOGIES_DIR = FIXTURES_DIR / "ontologies"
CASES_DIR = FIXTURES_DIR / "cases"
BENCHMARK_DIR = PACKAGE_DIR / "benchmark"
TESTS_DIR = PACKAGE_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if str(REPO_ROOT) not in sys.path:
    # 生产路径 algorithm.knowledge_pipeline.* 需要仓库根在 sys.path。
    sys.path.insert(0, str(REPO_ROOT))


def _load_package_modules(package_name: str, directory: Path, stems: list[str]) -> dict[str, types.ModuleType]:
    # pytest 全量运行时多个测试文件共享 sys.modules：已加载的模块直接复用，
    # 避免同名模块被重复 exec 出多个类对象（isinstance/类型一致性污染）。
    parent = sys.modules.get(package_name)
    if parent is None:
        parent = types.ModuleType(package_name)
        parent.__path__ = [str(directory)]
        parent.__package__ = package_name
        sys.modules[package_name] = parent
    loaded: dict[str, types.ModuleType] = {}
    for stem in stems:
        module_name = f"{package_name}.{stem}"
        if module_name in sys.modules:
            loaded[stem] = sys.modules[module_name]
            continue
        spec = importlib.util.spec_from_file_location(module_name, directory / f"{stem}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        loaded[stem] = module
    return loaded


MODULES = _load_package_modules(
    "knowledge_extract_v2",
    PACKAGE_DIR,
    [
        "config",
        "scheduler",
        "graph_utils",
        "ontology_schema",
        "ontology_compiler",
        "chunk_context",
        "structured_text",
        "chunk_metadata",
        "evidence",
        "graph_assembler",
        "prompt_router",
        "relation_resolver",
        "candidate_generation",
        "property_enrichment",
        "split_extractor",
        "candidate_models",
        "candidate_selector",
        "standard_semantics",
    ],
)
CC = MODULES["chunk_context"]
CM = MODULES["chunk_metadata"]
OC = MODULES["ontology_compiler"]
PR = MODULES["prompt_router"]
SE = MODULES["split_extractor"]
SS = MODULES["standard_semantics"]
ST = MODULES["structured_text"]
CS = MODULES["candidate_selector"]
MODELS = MODULES["candidate_models"]
EVID = MODULES["evidence"]

from stub_llm import StubLLM  # noqa: E402


OFFICIAL_ONTOLOGY = (ONTOLOGIES_DIR / "official_document_ontology.puml").read_text(encoding="utf-8")
STANDARD_ONTOLOGY = (ONTOLOGIES_DIR / "standard_ontology.puml").read_text(encoding="utf-8")

#: 中性本体：不含任何公文/标准域类，用于验证「无本体先验」时的信号判定。
NEUTRAL_ONTOLOGY = """@startuml neutral-ontology
class "规则" as Rule <<KnowledgeUnit>> {
  + 规则编号 (ruleId): String // 规则唯一编号
  + 规则内容 (ruleText): String // 规则内容原文
}
' @identity Rule: ruleId
@enduml
"""

#: 双域本体：同时含公文与标准类（镜像插件 v5.0 元模型包结构），本体无先验。
DUAL_ONTOLOGY = """@startuml dual-domain-ontology
class "公文" as OfficialDocument <<KnowledgeUnit>> {
  + 文件标题 (documentTitle): String // 文件标题
}
class "标准" as Standard <<KnowledgeUnit>> {
  + 标准号 (standardNumber): String // 标准号
}
' @identity OfficialDocument: documentTitle
' @identity Standard: standardNumber
@enduml
"""

OFFICIAL_TEXT = (
    "中国中车集团关于印发《数据安全管理办法》的通知\n"
    "中车股份办科信〔2026〕4号\n"
    "各所属企业应于2026年4月10日前完成排查。"
)
NEUTRAL_TEXT = "普通正文，没有任何域强信号。"

NOTICE_MD = (
    BENCHMARK_DIR
    / "official"
    / "official_02"
    / "docs"
    / "关于印发《轨道交通装备行业高质量数据集链主任务工作方案》的通知.md"
)
FORM_MD = BENCHMARK_DIR / "official" / "official_02" / "docs" / "表单.md"
STANDARD_DOCS_DIR = BENCHMARK_DIR / "standard" / "standard_01" / "docs"

#: 500-700 字符是即将切换的生产 chunk 上限；用接近该长度的正文模拟每片都无信号的常规切片。
_BODY_FILLER = (
    "本文件规定了试验条件、试样准备、试验方法与检验规则，"
    "适用于电动汽车用动力蓄电池单体、模块和系统的电性能试验与检验。"
)

#: 迷你标准本体：仅声明「表」信息单元表述形式，供确定性补全的表实体测试。
TABLE_ONTOLOGY = """@startuml standard-table-ontology
class "标准" as Standard <<KnowledgeUnit>> {
  + 标准号 (standardNumber): String // 标准号
}
class "信息单元表述形式" as InformationForm <<KnowledgeUnit>>
class "表" as Table <<Table>> {
  + 表名 (tableName): String // 表名
}
"表" --|> "信息单元表述形式"
' @identity Standard: standardNumber
@enduml
"""


def _json_table(payload: dict) -> str:
    """§10 解析行为规范：表格统一是 json 代码块内的 {"columns", "rows"}。"""
    return "```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```"


JSON_PAYLOAD = {
    "columns": ["分类", "检查项", "状态"],
    "rows": [
        {"分类": "基础管理", "检查项": "制度上墙", "状态": "符合"},
        {"分类": "现场作业", "检查项": "隔离执行", "状态": "不符合"},
    ],
}

#: 解析层 §10 产物形态：Markdown 外壳 + 表格 JSON 代码块。
JSON_TABLE_MD = "# 迎检数据\n\n## 迎检数据\n\n" + _json_table(JSON_PAYLOAD) + "\n"

FORM_PAYLOAD = {
    "columns": ["来文单位", "来文字号", "拟办意见", "领导批示"],
    "rows": [
        {
            "来文单位": "信息公司",
            "来文字号": "科信〔2026〕4号",
            "拟办意见": "由技术中心牵头办理并反馈",
            "领导批示": "已阅",
        }
    ],
}

#: 收文处理单表格 JSON：切片撕掉围栏后标题与数据可能分属不同 chunk。
FORM_JSON_MD = "# 收文处理单\n\n" + _json_table(FORM_PAYLOAD) + "\n"


def _plan(ontology: str, **kwargs):
    return OC.compile_plan(ontology, **kwargs)


def _chunk(text: str, chunk_id: str = "chunk-0", **kwargs) -> object:
    return CC.ExtractionChunk(chunk_id=chunk_id, text=text, **kwargs)


def _require_file(path: Path) -> str:
    if not path.exists():
        raise unittest.SkipTest(f"基准文档不存在: {path}")
    return path.read_text(encoding="utf-8")


def _real_chunks(markdown: str, chunk_size: int = 4000) -> list:
    """镜像生产路径：上游解析 Markdown -> knowledge_pipeline 递归切片 -> V2 ExtractionChunk。

    ChunkOptions 默认（backend/app/schemas.py）：recursive(size=4000, overlap=0)，
    即将切到 500-700；knowledge_extract_adapter.build_chunks 只带 range 偏移、
    不设 document_id（表单识别因此不能依赖文件名）。
    """
    try:
        from algorithm.knowledge_pipeline import chunk_document_items
    except Exception as exc:  # langchain_core 缺失等；本机明确 skip
        raise unittest.SkipTest(f"真实解析/切片管线不可用（需 langchain_core）: {exc}") from exc
    items = chunk_document_items(
        markdown,
        chunk_type=0,
        chunk_size=chunk_size,
        chunk_characters=None,
        similarity=0.6,
        chunk_overlap=0,
        breakpoint_method="percentile",
    )
    chunks = [
        CC.ExtractionChunk(
            chunk_id=f"chunk-{index:04d}",
            text=item["text"],
            start_offset=item["range"][0],
            end_offset=item["range"][1],
        )
        for index, item in enumerate(items)
    ]
    CM.enrich_chunk_metadata(chunks)
    return chunks


def _real_batch_chunks(documents: list[tuple[str, str, str]], chunk_size: int = 4000) -> list:
    """Mirror the batch service contract: chunk each source document independently.

    The service stores one combined Markdown artifact, but it passes extraction
    chunks with explicit document IDs and global ranges.  Keeping this helper
    here makes the routing tests exercise that upstream boundary contract rather
    than relying on a mid-chunk ``## 文件:`` heuristic.
    """
    combined_cursor = 0
    chunks = []
    for document_id, file_name, markdown in documents:
        part = f"\n## 文件: {file_name}\n\n{markdown}"
        part_start = combined_cursor
        for index, chunk in enumerate(_real_chunks(part, chunk_size=chunk_size)):
            chunk.chunk_id = f"{document_id}-{index:04d}"
            chunk.start_offset = (chunk.start_offset or 0) + part_start
            chunk.end_offset = (chunk.end_offset or 0) + part_start
            chunk.document_id = document_id
            chunk.metadata["source_file"] = file_name
            chunks.append(chunk)
        combined_cursor += len(part) + 1  # service joins parts with one newline
    return chunks


class SignalDetectionTest(unittest.TestCase):
    """A.1 强信号正则修复：只信精确率优先的强信号。"""

    def test_standard_number_cjk_boundary(self) -> None:
        for text in (
            "本文件按照GB/T 1.1—2020《标准化工作导则》起草。",
            "依据 GB/T 12345-2020 编制。",
            "标准号：GB 38031—2020",
            "Q/CR 200-2020",
        ):
            self.assertTrue(PR._has_standard_number(text), text)
        # 左侧字母数字边界不命中；"第X条" 不再给标准加分（法规体属公文特征）。
        self.assertFalse(PR._has_standard_number("WGB 12345"))
        self.assertFalse(PR._has_standard_number("第一条 为规范管理，制定本办法。"))

    def test_official_signals(self) -> None:
        for text in (
            "中国重汽集团关于印发《产品研发管理办法》的通知",
            "中车股份办科信〔2026〕4号",
            "中国中车集团有限公司科技研究开发计划管理办法",
        ):
            self.assertTrue(PR._has_official_signal(text), text)
        # 标准号行不参与企业标题判定；无标题形式/文号的条款句不再触发。
        self.assertFalse(PR._has_official_signal("GB/T 12345-2020 某实施方案"))
        self.assertFalse(PR._has_official_signal("第一条 为规范管理，制定本办法。"))
        self.assertFalse(PR._has_official_signal("本文件按照GB/T 1.1—2020《标准化工作导则》起草。"))

    def test_standard_number_typographic_variants(self) -> None:
        # 国标排版混用斜杠与破折号变体；匹配前先规范化，否则整类漏判。
        for text in (
            "本文件按照GB∕T 1.1—2020《标准化工作导则》起草。",  # U+2215 division slash
            "依据 GB／T 12345-2020 编制。",  # 全角 solidus
            "本文件按照GB⁄T 1.1—2020起草。",  # U+2044 fraction slash
            "ＧＢ／Ｔ 12345-2020",  # 全角字母与斜杠
        ):
            self.assertTrue(PR._has_standard_number(text), text)

    def test_fullwidth_official_number(self) -> None:
        # 全角数字经 NFKC 折叠后应按文号命中。
        self.assertTrue(PR._has_official_signal("中车股份办科信〔２０２６〕４号"))


class MultiLabelRouteTest(unittest.TestCase):
    """A.2 多标签判定：强信号存在性 + kind 优先级。"""

    def setUp(self) -> None:
        self.plan = _plan(NEUTRAL_ONTOLOGY)

    def test_standard_number_labels_standard(self) -> None:
        chunk = _chunk("GB/T 31486—2024《电动汽车用动力蓄电池电性能要求及试验方法》")
        route = PR.resolve_document_route(self.plan, [chunk])
        self.assertEqual(route.kind, "standard")
        self.assertEqual(route.labels, ("standard",))
        self.assertEqual(route.source, "chunks")
        self.assertIn("route.chunks.standard_number", route.reasons)

    def test_official_number_labels_official(self) -> None:
        chunk = _chunk("中车股份办科信〔2026〕4号")
        route = PR.resolve_document_route(self.plan, [chunk])
        self.assertEqual(route.kind, "official_document")
        self.assertEqual(route.labels, ("official_document",))
        self.assertIn("route.chunks.official", route.reasons)

    def test_metadata_signal_participates(self) -> None:
        chunk = _chunk(NEUTRAL_TEXT, metadata={"title": "中国中车集团关于印发《管理办法》的通知"})
        route = PR.resolve_document_route(self.plan, [chunk])
        self.assertEqual(route.labels, ("official_document",))

    def test_dual_labels_keep_official_priority(self) -> None:
        chunk = _chunk(OFFICIAL_TEXT + "\n本文件按照GB/T 1.1—2020《标准化工作导则》起草。")
        route = PR.resolve_document_route(self.plan, [chunk])
        self.assertEqual(route.kind, "official_document")
        self.assertEqual(route.labels, ("official_document", "standard"))


class SegmentThresholdTest(unittest.TestCase):
    """A.2b 段内采信阈值：文档头锚定 + 正文多命中佐证，替代存在性判定。"""

    def setUp(self) -> None:
        self.plan = _plan(NEUTRAL_ONTOLOGY)

    def test_body_only_citation_not_labeled(self) -> None:
        chunks = [
            _chunk(OFFICIAL_TEXT, "chunk-0", document_id="doc-a"),
            _chunk("本文件按照GB/T 1.1—2020《标准化工作导则》起草。", "chunk-1", document_id="doc-a"),
        ]
        routes = PR.resolve_chunk_routes(self.plan, chunks)
        for chunk in chunks:
            self.assertEqual(routes[chunk.chunk_id].labels, ("official_document",))
        aggregate = PR.resolve_document_route(self.plan, chunks)
        self.assertNotIn("standard", aggregate.labels)
        self.assertIn("route.chunks.standard_number.unconfirmed", aggregate.reasons)
        self.assertNotIn("route.chunks.standard_number", aggregate.reasons)

    def test_blank_head_chunk_metadata_still_anchors(self) -> None:
        # 空白 chunk 只带 metadata 信号时仍属文档头，不应被降级为正文引用。
        chunks = [
            _chunk(
                "",
                "chunk-0",
                document_id="doc-a",
                metadata={"title": "中国中车集团关于印发《管理办法》的通知"},
            ),
            _chunk("正文其余内容，无信号。", "chunk-1", document_id="doc-a"),
            _chunk("正文其余内容，无信号。", "chunk-2", document_id="doc-a"),
        ]
        routes = PR.resolve_chunk_routes(self.plan, chunks)
        for chunk in chunks:
            self.assertEqual(routes[chunk.chunk_id].kind, "official_document")

    def test_head_signal_labels_whole_segment(self) -> None:
        chunks = [
            _chunk(
                "GB/T 31486—2024《电动汽车用动力蓄电池电性能要求及试验方法》",
                "chunk-0",
                document_id="doc-a",
            ),
            _chunk("正文其余内容，无信号。", "chunk-1", document_id="doc-a"),
            _chunk("正文其余内容，无信号。", "chunk-2", document_id="doc-a"),
        ]
        routes = PR.resolve_chunk_routes(self.plan, chunks)
        for chunk in chunks:
            self.assertEqual(routes[chunk.chunk_id].kind, "standard")

    def test_body_corroborated_two_hits_labeled(self) -> None:
        chunks = [
            _chunk(OFFICIAL_TEXT, "chunk-0", document_id="doc-a"),
            _chunk("本文件按照GB/T 1.1—2020《标准化工作导则》起草。", "chunk-1", document_id="doc-a"),
            _chunk("规范性引用文件：GB/T 31486—2024《电动汽车用动力蓄电池》。", "chunk-2", document_id="doc-a"),
        ]
        aggregate = PR.resolve_document_route(self.plan, chunks)
        # 正文 2 处命中 >= max(2, ceil(0.5*3))，按佐证采信。
        self.assertIn("standard", aggregate.labels)
        self.assertIn("route.chunks.standard_number.corroborated", aggregate.reasons)
        self.assertEqual(aggregate.kind, "official_document")

    def test_low_ratio_body_hits_not_labeled(self) -> None:
        chunks = [
            _chunk(OFFICIAL_TEXT, "chunk-0", document_id="doc-a"),
            _chunk("本文件按照GB/T 1.1—2020《标准化工作导则》起草。", "chunk-1", document_id="doc-a"),
            _chunk("规范性引用文件：GB/T 31486—2024《电动汽车用动力蓄电池》。", "chunk-2", document_id="doc-a"),
            _chunk("正文其余内容，无信号。", "chunk-3", document_id="doc-a"),
            _chunk("正文其余内容，无信号。", "chunk-4", document_id="doc-a"),
        ]
        # 2 处命中 < ceil(0.5*5)=3，比例不足，不采信。
        aggregate = PR.resolve_document_route(self.plan, chunks)
        self.assertNotIn("standard", aggregate.labels)
        self.assertIn("route.chunks.standard_number.unconfirmed", aggregate.reasons)

    def test_scaffolding_only_detection(self) -> None:
        self.assertTrue(PR._is_scaffolding_only(""))
        self.assertTrue(PR._is_scaffolding_only("## 文件: 标准.txt\n"))
        self.assertTrue(PR._is_scaffolding_only("## 文件: 标准.txt\n# 封面标题\n"))
        self.assertFalse(PR._is_scaffolding_only("## 文件: 标准.txt\nGB/T 1.1—2020 正文"))
        self.assertFalse(PR._is_scaffolding_only("GB/T 1.1—2020《标准化工作导则》"))

    def test_batch_header_scaffolding_chunk_keeps_head_anchor(self) -> None:
        # 500-700 字符切块下 "## 文件:" 批量头常独占段首 chunk（不含正文内容）。
        # 若把它当文档头锚点，封面 chunk 的标准号会落进正文分支，凑不满多命中
        # 阈值而被静默丢弃；锚点必须落到首个实质内容 chunk。
        chunks = [
            _chunk("## 文件: GB_T 31486.md", "chunk-0"),
            _chunk(
                "GB/T 31486—2024《电动汽车用动力蓄电池电性能要求及试验方法》\n"
                + _BODY_FILLER * 9,
                "chunk-1",
            ),
            _chunk("3 术语和定义\n" + _BODY_FILLER * 10, "chunk-2"),
            _chunk("4 试验条件\n" + _BODY_FILLER * 10, "chunk-3"),
            _chunk("5 试验方法\n" + _BODY_FILLER * 10, "chunk-4"),
            _chunk("6 检验规则\n" + _BODY_FILLER * 10, "chunk-5"),
        ]
        aggregate = PR.resolve_document_route(self.plan, chunks)
        self.assertIn("standard", aggregate.labels)
        self.assertIn("route.chunks.standard_number", aggregate.reasons)
        routes = PR.resolve_chunk_routes(self.plan, chunks)
        for chunk in chunks:
            self.assertEqual(routes[chunk.chunk_id].kind, "standard", chunk.chunk_id)

    def test_long_chunk_body_citation_not_labeled(self) -> None:
        # 500-700 字符下正文引用标准号仍不得给整段贴标准标签。
        chunks = [
            _chunk(OFFICIAL_TEXT + "\n" + _BODY_FILLER * 8, "chunk-0", document_id="doc-a"),
            _chunk(
                "本文件按照GB/T 1.1—2020《标准化工作导则》起草。" + _BODY_FILLER * 8,
                "chunk-1",
                document_id="doc-a",
            ),
            _chunk(_BODY_FILLER * 9, "chunk-2", document_id="doc-a"),
            _chunk(_BODY_FILLER * 9, "chunk-3", document_id="doc-a"),
        ]
        aggregate = PR.resolve_document_route(self.plan, chunks)
        self.assertNotIn("standard", aggregate.labels)
        self.assertIn("route.chunks.standard_number.unconfirmed", aggregate.reasons)


class OntologyBiasTest(unittest.TestCase):
    """A.3 本体偏好：仅单域本体在无强信号时补标签。"""

    def test_official_single_domain_bias(self) -> None:
        route = PR.resolve_document_route(_plan(OFFICIAL_ONTOLOGY), [_chunk(NEUTRAL_TEXT)])
        self.assertEqual(route.kind, "official_document")
        self.assertEqual(route.labels, ("official_document",))
        self.assertEqual(route.source, "ontology")
        self.assertIn("route.preference.ontology.official_document", route.reasons)

    def test_standard_single_domain_bias(self) -> None:
        route = PR.resolve_document_route(_plan(STANDARD_ONTOLOGY), [_chunk(NEUTRAL_TEXT)])
        self.assertEqual(route.kind, "standard")
        self.assertEqual(route.source, "ontology")
        self.assertIn("route.preference.ontology.standard", route.reasons)

    def test_neutral_ontology_falls_back_to_generic(self) -> None:
        route = PR.resolve_document_route(_plan(NEUTRAL_ONTOLOGY), [_chunk(NEUTRAL_TEXT)])
        self.assertEqual(route.kind, "generic")
        self.assertEqual(route.labels, ())
        self.assertEqual(route.source, "fallback")
        self.assertEqual(route.reasons, ("route.fallback.no_domain_signal",))
        self.assertEqual(route.prompt_extension, "")

    def test_dual_ontology_has_no_bias(self) -> None:
        route = PR.resolve_document_route(_plan(DUAL_ONTOLOGY), [_chunk(NEUTRAL_TEXT)])
        self.assertEqual(route.kind, "generic")
        self.assertEqual(route.source, "fallback")


class SegmentationTest(unittest.TestCase):
    """A.4/A.5 按文档分段路由与聚合。"""

    def setUp(self) -> None:
        self.plan = _plan(NEUTRAL_ONTOLOGY)

    def test_batch_header_starts_new_segment(self) -> None:
        chunks = [
            _chunk(OFFICIAL_TEXT, "chunk-0"),
            _chunk("## 文件: 标准.txt\n\nGB/T 1.1—2020《标准化工作导则》", "chunk-1"),
        ]
        routes = PR.resolve_chunk_routes(self.plan, chunks)
        self.assertEqual(routes["chunk-0"].kind, "official_document")
        self.assertEqual(routes["chunk-1"].kind, "standard")
        aggregate = PR.resolve_document_route(self.plan, chunks)
        self.assertEqual(aggregate.kind, "official_document")
        self.assertEqual(aggregate.labels, ("official_document", "standard"))
        self.assertEqual(aggregate.source, "chunks")

    def test_header_inside_chunk_starts_new_segment(self) -> None:
        chunks = [
            _chunk(OFFICIAL_TEXT, "chunk-0"),
            _chunk("上一文件结尾\n## 文件: 标准.txt\nGB/T 1.1—2020 标准正文", "chunk-1"),
        ]
        routes = PR.resolve_chunk_routes(self.plan, chunks)
        self.assertEqual(routes["chunk-0"].kind, "official_document")
        # 一个上游 chunk 跨越两个批量文件时，V2 不得猜测边界；拒判并要求
        # 上游重切片，避免把前一文件内容与后一文件路由信号混在一起。
        self.assertEqual(routes["chunk-1"].kind, "generic")
        self.assertEqual(routes["chunk-1"].decision, "ambiguous")
        self.assertEqual(
            routes["chunk-1"].abstain_reason,
            "batch_header_mid_chunk_requires_upstream_resplit",
        )

    def test_route_explanation_and_scores_are_serializable(self) -> None:
        route = PR.resolve_document_route(
            self.plan, [_chunk("ASTM D638-22 tensile test specification")]
        )
        self.assertEqual(route.labels, ("standard",))
        self.assertEqual(route.decision, "accepted")
        self.assertEqual(route.scores["standard"], 1.0)
        payload = route.to_dict()
        self.assertEqual(payload["router_version"], PR.ROUTER_VERSION)
        self.assertIn("route.chunks.standard_number", payload["signals"])

    def test_untrusted_metadata_does_not_route(self) -> None:
        chunk = _chunk(
            NEUTRAL_TEXT,
            metadata={
                "title": "关于印发《伪造标题》的通知",
                "signal_source": "user",
            },
        )
        route = PR.resolve_document_route(self.plan, [chunk])
        self.assertEqual(route.kind, "generic")

    def test_metadata_control_chars_and_oversized_values_are_ignored(self) -> None:
        chunk = _chunk(
            NEUTRAL_TEXT,
            metadata={
                "title": "关于\x00印发《伪造标题》的通知",
                "heading": "关于印发《" + ("x" * 300) + "》的通知",
            },
        )
        self.assertEqual(PR.resolve_document_route(self.plan, [chunk]).kind, "generic")

    def test_document_id_change_starts_new_segment(self) -> None:
        chunks = [
            _chunk(OFFICIAL_TEXT, "chunk-0", document_id="doc-a"),
            _chunk("GB/T 1.1—2020 标准正文", "chunk-1", document_id="doc-b"),
        ]
        routes = PR.resolve_chunk_routes(self.plan, chunks)
        self.assertEqual(routes["chunk-0"].kind, "official_document")
        self.assertEqual(routes["chunk-1"].kind, "standard")

    def test_same_document_chunks_share_route(self) -> None:
        chunks = [
            _chunk(OFFICIAL_TEXT, "chunk-0", document_id="doc-a"),
            _chunk(NEUTRAL_TEXT, "chunk-1", document_id="doc-a"),
        ]
        routes = PR.resolve_chunk_routes(self.plan, chunks)
        self.assertEqual(routes["chunk-0"].kind, "official_document")
        # 同段内任一 chunk 命中强信号即整段生效（避免信号只落在首片时漏判）。
        self.assertEqual(routes["chunk-1"].kind, "official_document")

    def test_aggregate_source_priority(self) -> None:
        chunks = [
            _chunk(NEUTRAL_TEXT, "chunk-0", document_id="doc-a"),
            _chunk("GB/T 1.1—2020 标准正文", "chunk-1", document_id="doc-b"),
        ]
        aggregate = PR.resolve_document_route(self.plan, chunks)
        self.assertEqual(aggregate.source, "chunks")
        self.assertEqual(aggregate.labels, ("standard",))
        self.assertIn("route.fallback.no_domain_signal", aggregate.reasons)
        self.assertIn("route.chunks.standard_number", aggregate.reasons)


class PromptExtensionTest(unittest.TestCase):
    """A.6 多标签扩展拼接与 chat messages 路由行。"""

    def setUp(self) -> None:
        self.plan = _plan(NEUTRAL_ONTOLOGY)

    def test_extension_join_order(self) -> None:
        chunk = _chunk(OFFICIAL_TEXT + "\nGB/T 1.1—2020《标准化工作导则》")
        route = PR.resolve_document_route(self.plan, [chunk])
        self.assertTrue(route.prompt_extension.startswith(PR.OFFICIAL_DOCUMENT_PROMPT_EXTENSION))
        self.assertIn("\n\n" + PR.STANDARD_PROMPT_EXTENSION, route.prompt_extension)

    def test_build_chat_messages_route_line(self) -> None:
        chunk = _chunk(OFFICIAL_TEXT + "\nGB/T 1.1—2020《标准化工作导则》")
        route = PR.resolve_document_route(self.plan, [chunk])
        messages = PR.build_chat_messages(task="entity", plan=self.plan, route=route, chunk=chunk)
        system, user = messages[0]["content"], messages[1]["content"]
        self.assertIn(PR.OFFICIAL_DOCUMENT_PROMPT_EXTENSION, system)
        self.assertIn(PR.STANDARD_PROMPT_EXTENSION, system)
        self.assertIn("kind=official_document; labels=official_document, standard", user)

    def test_generic_has_no_extension(self) -> None:
        chunk = _chunk(NEUTRAL_TEXT)
        route = PR.resolve_document_route(self.plan, [chunk])
        messages = PR.build_chat_messages(task="entity", plan=self.plan, route=route, chunk=chunk)
        self.assertEqual(messages[0]["content"], PR.BASE_SYSTEM_PROMPT)
        self.assertIn("labels=none", messages[1]["content"])


class StandardGateDecouplingTest(unittest.TestCase):
    """A.7 标准补全解耦：门控看 labels，不看被公文抢占的 kind。"""

    def _run_with_spy(self, chunks: list) -> tuple[list, dict]:
        calls: list[bool] = []
        original = SE.enrich_standard_semantics

        def spy(pool, manager, plan, *, warnings=None):  # noqa: ANN001
            calls.append(True)
            return {}

        SE.enrich_standard_semantics = spy
        try:
            result = SE.extract_split(
                DUAL_ONTOLOGY,
                chunks=chunks,
                llm=StubLLM(),
            )
        finally:
            SE.enrich_standard_semantics = original
        return calls, result

    def test_dual_label_document_runs_standard_enrichment(self) -> None:
        text = OFFICIAL_TEXT + "\n本文件按照GB/T 1.1—2020《标准化工作导则》起草。"
        route = PR.resolve_document_route(_plan(DUAL_ONTOLOGY), [_chunk(text)])
        # 旧实现 kind=official_document 时门控直接跳过标准补全；labels 必须保留 standard。
        self.assertEqual(route.kind, "official_document")
        self.assertIn("standard", route.labels)
        calls, result = self._run_with_spy([_chunk(text)])
        self.assertEqual(len(calls), 1)
        self.assertIn("standard", result["profile"]["document_route"]["labels"])
        self.assertEqual(result["profile"]["chunk_routes"], {"chunk-0": "official_document"})

    def test_generic_document_skips_standard_enrichment(self) -> None:
        calls, result = self._run_with_spy([_chunk(NEUTRAL_TEXT)])
        self.assertEqual(len(calls), 0)
        self.assertEqual(result["profile"]["document_route"]["labels"], [])
        self.assertEqual(result["profile"]["chunk_routes"], {"chunk-0": "generic"})

    def test_citation_only_standard_signal_skips_standard_enrichment(self) -> None:
        # 正文单处引用标准号不再贴 standard 标签，标准补全不应被触发。
        chunks = [
            _chunk(OFFICIAL_TEXT, "chunk-0", document_id="doc-a"),
            _chunk("本文件按照GB/T 1.1—2020《标准化工作导则》起草。", "chunk-1", document_id="doc-a"),
        ]
        calls, result = self._run_with_spy(chunks)
        self.assertEqual(len(calls), 0)
        self.assertEqual(
            result["profile"]["document_route"]["labels"], ["official_document"]
        )
        self.assertEqual(
            result["profile"]["chunk_routes"],
            {"chunk-0": "official_document", "chunk-1": "official_document"},
        )


class ProfileRouteOutputTest(unittest.TestCase):
    """调用输出：profile.chunk_routes 逐 chunk 暴露路由 kind。"""

    def test_per_chunk_routes_surface_in_profile(self) -> None:
        chunks = [
            _chunk(OFFICIAL_TEXT, "chunk-0"),
            _chunk("## 文件: 标准.txt\nGB/T 1.1—2020 标准正文", "chunk-1"),
        ]
        result = SE.extract_split(DUAL_ONTOLOGY, chunks=chunks, llm=StubLLM())
        self.assertEqual(
            result["profile"]["chunk_routes"],
            {"chunk-0": "official_document", "chunk-1": "standard"},
        )


class JsonTableChunkTest(unittest.TestCase):
    """A.8 §10 表格 JSON 化后的 chunk 识别：记录表策略与表单抑制规则不丢失。"""

    def setUp(self) -> None:
        self.plan = _plan(DUAL_ONTOLOGY)
        self.route = PR.DocumentRoute(kind="generic", source="fallback")

    def test_fenced_json_table_is_record_table(self) -> None:
        # 解析层形态：Markdown 外壳 + json 代码块（可能还有标题行）。
        chunk = _chunk("# 迎检数据\n\n" + _json_table(JSON_PAYLOAD))
        self.assertTrue(PR._looks_like_record_table(chunk))

    def test_bare_torn_json_chunk_is_record_table(self) -> None:
        # 新切片把 JSON 块保护成独立 chunk：围栏留在相邻片段，chunk 文本是裸 JSON。
        chunk = _chunk(json.dumps(JSON_PAYLOAD, ensure_ascii=False, indent=2))
        self.assertTrue(PR._looks_like_record_table(chunk))

    def test_prose_chunk_with_minor_json_has_payload_but_not_record_table(self) -> None:
        # 正文夹带少量 JSON：算「含表格 JSON」（结构支撑），但不套记录表策略。
        chunk = _chunk(
            _BODY_FILLER * 10 + "\n" + _json_table({"columns": ["检查项"], "rows": []})
        )
        self.assertFalse(PR._looks_like_record_table(chunk))
        self.assertTrue(ST.has_json_table_payload(chunk.text))

    def test_torn_closing_fence_is_tolerated(self) -> None:
        # 切片把收尾围栏留给了 JSON 后面的片段；即使围栏落在裸 JSON 末尾也要能识别。
        chunk = _chunk(json.dumps(JSON_PAYLOAD, ensure_ascii=False, indent=2) + "\n```")
        self.assertTrue(ST.is_structured_payload(chunk.text))

    def test_torn_form_json_chunk_detected_without_title(self) -> None:
        # 生产 build_chunks 不设 document_id，且切片可能把「收文处理单」标题
        # 与表格数据分到不同 chunk：数据 chunk 靠表单字段名 + JSON 负载兜底。
        chunk = _chunk(json.dumps(FORM_PAYLOAD, ensure_ascii=False, indent=2))
        head = chunk.text[:600]
        self.assertNotIn("表单", head)
        self.assertNotIn("收文处理单", head)
        self.assertTrue(PR._looks_like_official_form(chunk))

    def test_form_rules_attach_to_torn_json_chunk(self) -> None:
        chunk = _chunk(json.dumps(FORM_PAYLOAD, ensure_ascii=False, indent=2))
        messages = PR.build_chat_messages(
            task="entity", plan=self.plan, route=self.route, chunk=chunk
        )
        user = messages[1]["content"]
        self.assertIn(PR._OFFICIAL_FORM_RULES, user)
        self.assertIn(PR._RECORD_TABLE_ENTITY_RULES, user)

    def test_generic_json_table_is_not_official_form(self) -> None:
        chunk = _chunk(
            json.dumps(
                {"columns": ["名称", "数量"], "rows": [{"名称": "甲", "数量": "1"}]},
                ensure_ascii=False,
                indent=2,
            )
        )
        self.assertFalse(PR._looks_like_official_form(chunk))


class StandardTableFormJsonTest(unittest.TestCase):
    """A.9 标准「表N 标题」结构支撑：§10 JSON 表格（含被切到相邻 chunk）不再丢表实体。"""

    class _PoolStub:
        def __init__(self) -> None:
            self.entity_candidate_count = 0
            self.entities: list = []

        def add_entity(self, candidate, warnings=None):  # noqa: ANN001
            self.entity_candidate_count += 1
            self.entities.append(candidate)
            return candidate

    def _run(self, chunks: list) -> tuple[dict, "StandardTableFormJsonTest._PoolStub"]:
        plan = _plan(TABLE_ONTOLOGY)
        pool = self._PoolStub()
        counts = SS._add_forms(pool, list(chunks), plan, [])
        return counts, pool

    def test_caption_with_json_opener_in_same_chunk(self) -> None:
        chunks = [
            _chunk("表1 检查项清单\n```json", "chunk-0"),
            _chunk(json.dumps(JSON_PAYLOAD, ensure_ascii=False, indent=2), "chunk-1"),
        ]
        counts, pool = self._run(chunks)
        self.assertEqual(counts.get("Table"), 1)
        self.assertEqual([entity.name for entity in pool.entities], ["表1 检查项清单"])

    def test_caption_with_bare_json_following_chunk(self) -> None:
        chunks = [
            _chunk("表1 检查项清单", "chunk-0"),
            _chunk(json.dumps(JSON_PAYLOAD, ensure_ascii=False, indent=2), "chunk-1"),
        ]
        counts, pool = self._run(chunks)
        self.assertEqual(counts.get("Table"), 1)
        self.assertEqual([entity.name for entity in pool.entities], ["表1 检查项清单"])

    def test_caption_with_fence_attached_json_chunk(self) -> None:
        # 切片器修复后：围栏随 JSON 同行，数据 chunk 是完整围栏块，表题在上一 chunk。
        chunks = [
            _chunk("表1 检查项清单", "chunk-0"),
            _chunk(_json_table(JSON_PAYLOAD), "chunk-1"),
        ]
        counts, pool = self._run(chunks)
        self.assertEqual(counts.get("Table"), 1)
        self.assertEqual([entity.name for entity in pool.entities], ["表1 检查项清单"])

    def test_caption_with_pipe_table(self) -> None:
        # .md/.txt 原文直读：管道表格此前同样不被 <table\b 覆盖。
        chunks = [
            _chunk("表1 检查项清单\n| 检查项 | 状态 |\n| --- | --- |\n| 制度上墙 | 符合 |", "chunk-0"),
        ]
        counts, pool = self._run(chunks)
        self.assertEqual(counts.get("Table"), 1)
        self.assertEqual([entity.name for entity in pool.entities], ["表1 检查项清单"])

    def test_caption_without_table_structure_skipped(self) -> None:
        counts, pool = self._run([_chunk("表1 检查项清单\n表格内容见附件。", "chunk-0")])
        self.assertEqual(counts.get("Table", 0), 0)
        self.assertEqual(pool.entities, [])


class InformationFormTableJsonTest(unittest.TestCase):
    """A.10 表 InformationForm 候选的结构支撑：§10 JSON 表格不再被当作无结构而拒绝。"""

    def _candidate(self, block_id: str, text: str) -> object:
        quote = "表1 检查项清单"
        start = text.find(quote)
        evidence = EVID.Evidence(
            span_id=f"{block_id}:{start}:{start + len(quote)}",
            source_block_ids=[block_id],
            quote=quote,
            start_char=start,
            end_char=start + len(quote),
            evidence_type="explicit",
        )
        return MODELS.EntityCandidate(
            candidate_id="cand-1",
            proposed_type="Table",
            canonical_type="Table",
            name=quote,
            properties={},
            evidence=[evidence],
            source="llm",
        )

    def _run(self, chunk_texts: list[str]) -> tuple[object, list[dict]]:
        plan = _plan(TABLE_ONTOLOGY)
        chunks = [_chunk(text, f"chunk-{index:04d}") for index, text in enumerate(chunk_texts)]
        manager = CC.ChunkContextManager(chunks)
        candidate = self._candidate("chunk-0000", chunk_texts[0])
        warnings: list[dict] = []
        selector = CS.CandidateSelector(plan=plan, manager=manager, warnings=warnings)
        selector.post_relation([candidate], [])
        return candidate, warnings

    def test_caption_followed_by_fence_opener_supported(self) -> None:
        # 500-700 字符切块：表题与数据分属相邻 chunk，围栏开头留在表题 chunk 里。
        candidate, warnings = self._run(
            [
                "本节规定了检查项要求。\n\n表1 检查项清单\n\n```json",
                json.dumps(JSON_PAYLOAD, ensure_ascii=False, indent=2),
            ]
        )
        self.assertEqual(candidate.mapping_status, "accepted")
        self.assertEqual(warnings, [])

    def test_caption_followed_by_fence_attached_chunk_supported(self) -> None:
        # 切片器修复后：围栏收进 JSON 片，表题 chunk 不再残留半截围栏。
        candidate, warnings = self._run(
            [
                "本节规定了检查项要求。\n\n表1 检查项清单\n",
                _json_table(JSON_PAYLOAD),
            ]
        )
        self.assertEqual(candidate.mapping_status, "accepted")
        self.assertEqual(warnings, [])

    def test_caption_with_fenced_json_same_chunk_supported(self) -> None:
        candidate, warnings = self._run(
            ["表1 检查项清单\n\n" + _json_table(JSON_PAYLOAD)]
        )
        self.assertEqual(candidate.mapping_status, "accepted")
        self.assertEqual(warnings, [])

    def test_caption_without_table_structure_still_rejected(self) -> None:
        candidate, warnings = self._run(["表1 检查项清单\n\n表格内容见附件。"])
        self.assertEqual(candidate.mapping_status, "rejected")
        self.assertTrue(
            any(item["code"] == "CANDIDATE_UNSUPPORTED_INFORMATION_FORM" for item in warnings)
        )


class RealPipelineRouteTest(unittest.TestCase):
    """B. 真实解析+切片管线：直接消费上游 Markdown 产物（缺 langchain_core 时 skip）。"""

    def setUp(self) -> None:
        # 镜像插件 v5.0 元模型：公文+标准双域本体，路由完全依赖 chunk 强信号。
        self.plan = _plan(DUAL_ONTOLOGY)

    def test_real_notice_markdown_routes_official(self) -> None:
        markdown = _require_file(NOTICE_MD)
        chunks = _real_chunks(markdown)
        self.assertTrue(chunks)
        routes = PR.resolve_chunk_routes(self.plan, chunks)
        for chunk in chunks:
            self.assertEqual(
                routes[chunk.chunk_id].kind, "official_document", chunk.chunk_id
            )

    def test_real_standard_case_text_routes_standard(self) -> None:
        text = json.loads((CASES_DIR / "01_standard.json").read_text(encoding="utf-8"))["text"]
        chunks = _real_chunks(text)
        route = PR.resolve_document_route(self.plan, chunks)
        self.assertEqual(route.kind, "standard")

    def test_real_official_case_text_routes_official(self) -> None:
        text = json.loads((CASES_DIR / "02_official_document.json").read_text(encoding="utf-8"))["text"]
        chunks = _real_chunks(text)
        route = PR.resolve_document_route(self.plan, chunks)
        self.assertEqual(route.kind, "official_document")

    def test_real_batch_concat_per_segment_routes(self) -> None:
        notice = _require_file(NOTICE_MD)
        standard_docs = sorted(STANDARD_DOCS_DIR.glob("*.md"))
        if not standard_docs:
            raise unittest.SkipTest(f"缺少标准基准文档: {STANDARD_DOCS_DIR}")
        standard = standard_docs[0].read_text(encoding="utf-8")
        chunks = _real_batch_chunks([
            ("通知.md", "通知.md", notice),
            ("标准.txt", "标准.txt", standard),
        ])
        routes = PR.resolve_chunk_routes(self.plan, chunks)
        expected_by_document = {"通知.md": "official_document", "标准.txt": "standard"}
        for chunk in chunks:
            self.assertEqual(routes[chunk.chunk_id].kind, expected_by_document[chunk.document_id], chunk.chunk_id)
        aggregate = PR.resolve_document_route(self.plan, chunks)
        self.assertEqual(aggregate.kind, "official_document")
        self.assertEqual(aggregate.labels, ("official_document", "standard"))

    def test_real_batch_concat_at_600_chars_per_segment_routes(self) -> None:
        # 500-700 字符生产切块 + 批量拼接：段边界与段内路由都必须保持。
        notice = _require_file(NOTICE_MD)
        standard_docs = sorted(STANDARD_DOCS_DIR.glob("*.md"))
        if not standard_docs:
            raise unittest.SkipTest(f"缺少标准基准文档: {STANDARD_DOCS_DIR}")
        standard = standard_docs[0].read_text(encoding="utf-8")
        chunks = _real_batch_chunks([
            ("通知.md", "通知.md", notice),
            ("标准.txt", "标准.txt", standard),
        ], chunk_size=600)
        routes = PR.resolve_chunk_routes(self.plan, chunks)
        self.assertTrue(chunks)
        self.assertTrue(all(chunk.document_id for chunk in chunks))
        expected_by_document = {"通知.md": "official_document", "标准.txt": "standard"}
        for chunk in chunks:
            self.assertEqual(routes[chunk.chunk_id].kind, expected_by_document[chunk.document_id], chunk.chunk_id)
        self.assertEqual(
            PR.resolve_document_route(self.plan, chunks).labels,
            ("official_document", "standard"),
        )

    def test_real_cases_at_600_chars_route_by_signal(self) -> None:
        # 两个用例 fixture 的强信号都在文档头；600 字符切块下也必须命中。
        standard = json.loads((CASES_DIR / "01_standard.json").read_text(encoding="utf-8"))["text"]
        official = json.loads((CASES_DIR / "02_official_document.json").read_text(encoding="utf-8"))["text"]
        self.assertEqual(
            PR.resolve_document_route(self.plan, _real_chunks(standard, chunk_size=600)).kind,
            "standard",
        )
        self.assertEqual(
            PR.resolve_document_route(self.plan, _real_chunks(official, chunk_size=600)).kind,
            "official_document",
        )

    def test_real_json_table_chunks_survive_protection(self) -> None:
        # §10 表格 JSON 经真实切片（JSON 保护）后仍是可识别的记录表。
        for size in (4000, 600):
            chunks = _real_chunks(JSON_TABLE_MD, chunk_size=size)
            payload_chunks = [chunk for chunk in chunks if ST.is_structured_payload(chunk.text)]
            self.assertTrue(payload_chunks, f"chunk_size={size}")
            for chunk in payload_chunks:
                self.assertTrue(PR._looks_like_record_table(chunk), f"chunk_size={size}")

    def test_real_form_json_chunks_keep_form_rules(self) -> None:
        chunks = _real_chunks(FORM_JSON_MD, chunk_size=600)
        payload_chunks = [chunk for chunk in chunks if ST.is_structured_payload(chunk.text)]
        self.assertTrue(payload_chunks)
        route = PR.DocumentRoute(kind="generic", source="fallback")
        for chunk in payload_chunks:
            self.assertTrue(PR._looks_like_official_form(chunk))
            messages = PR.build_chat_messages(
                task="entity", plan=self.plan, route=route, chunk=chunk
            )
            self.assertIn(PR._OFFICIAL_FORM_RULES, messages[1]["content"])

    def test_real_form_markdown_does_not_crash(self) -> None:
        markdown = _require_file(FORM_MD)
        chunks = _real_chunks(markdown)
        route = PR.resolve_document_route(self.plan, chunks)
        # 表单信号不定：断言按实际信号落在已知集合内即可。
        self.assertIn(route.kind, ("official_document", "generic"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
