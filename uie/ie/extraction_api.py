import os
import json
import logging
import time
from typing import Any, Optional

from ie.llm_extractor import LLMExtractor
from ie.utils import BaseLLM, LocalLLM, ApiLLM
from ie.schema_mapper import SchemaMapper
from ie.template_matcher import (
    parse_examples_templates_from_puml,
    match_templates,
    build_llm_hint_from_matches,
)


UML_DIR = os.path.join(os.path.dirname(__file__), "UML")
logger = logging.getLogger(__name__)

# 主对象到 UML 文件及 SchemaMapper 顶层类型的映射
_UML_CONFIG = {
    # 规约规则本体
    "RuleType": {
        "uml_file": "RuleType.puml",
        "prompt_main_object": "RuleType",
        "top_level_class": "RuleType",
    },
    # 系统元素本体
    "SystemElement": {
        "uml_file": "SystemElement.puml",
        "prompt_main_object": "SystemElement",
        "top_level_class": "SystemElement",
    },
    # 术语本体
    "Glossary": {
        "uml_file": "Term.puml",
        "prompt_main_object": "Glossary",
        "top_level_class": "Glossary",
    },
    "Term": {
        "uml_file": "Term.puml",
        "prompt_main_object": "Term",
        "top_level_class": "Term",
    },
}


def _load_uml_text(uml_file: str) -> str:
    path = os.path.join(UML_DIR, uml_file)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _inject_confidence(raw_data: Any, has_template_match: bool, high: float, low: float) -> Any:
    """
    在抽取结果中增加/填充顶层置信度字段：
    - 若存在模板匹配，则顶层对象 confidence = high
    - 若不存在模板匹配，则 confidence = low
    """

    def _set_conf(obj: Any):
        if isinstance(obj, dict):
            # 若模型已经给了 confidence，就不覆盖；否则按模板情况填充
            if "confidence" not in obj:
                obj["confidence"] = high if has_template_match else low
        return obj

    if isinstance(raw_data, list):
        return [_set_conf(item) for item in raw_data]
    return _set_conf(raw_data)


def extract_and_store(
    main_object: str,
    text: str,
    backend: Optional[BaseLLM] = None,
    use_local: bool = True,
    neo4j_uri: str = "bolt://localhost:7687",
    neo4j_user: str = "neo4j",
    neo4j_password: str = "neo4j2025",
):
    """
    知识抽取模块对外统一接口（原始版本，不使用模板提示）：

    1. 根据主对象 main_object 选择对应 UML 定义
    2. 使用 LLMExtractor 抽取出 JSON 结构
    3. 使用 SchemaMapper 将结果写入 Neo4j
    """
    logger.info("extract_and_store: main_object=%s text_len=%d store=neo4j", main_object, len(text))
    t0 = time.perf_counter()
    if main_object not in _UML_CONFIG:
        raise ValueError(f"不支持的主对象: {main_object}")

    cfg = _UML_CONFIG[main_object]
    uml_text = _load_uml_text(cfg["uml_file"])
    prompt_main_object = cfg["prompt_main_object"]
    top_level_class = cfg["top_level_class"]

    # 1) 选择大模型后端并抽取
    if backend is None:
        backend = LocalLLM() if use_local else ApiLLM()
    extractor = LLMExtractor(backend=backend)
    raw_data = extractor.extract(uml=uml_text, main_object=prompt_main_object, text=text)

    # 2) 写入 Neo4j
    mapper = SchemaMapper(uri=neo4j_uri, user=neo4j_user, password=neo4j_password)
    try:
        mapper.map_to_graph(raw_data, top_level_class=top_level_class)
    finally:
        mapper.close()

    logger.info("extract_and_store done: main_object=%s elapsed=%.2fs", main_object, time.perf_counter() - t0)
    return raw_data


def extract_only(
    main_object: str,
    text: str,
    backend: Optional[BaseLLM] = None,
    use_local: bool = True,
):
    """
    仅使用 LLM 抽取结构化结果，不写入 Neo4j。
    - main_object: "RuleType" / "SystemElement" / "Term"
    - text: 原始待抽取文本
    返回：raw_data（通常为 JSON 反序列化对象）
    """
    logger.info("extract_only: main_object=%s text_len=%d", main_object, len(text))
    t0 = time.perf_counter()
    if main_object not in _UML_CONFIG:
        raise ValueError(f"不支持的主对象: {main_object}")

    cfg = _UML_CONFIG[main_object]
    uml_text = _load_uml_text(cfg["uml_file"])
    prompt_main_object = cfg["prompt_main_object"]
    top_level_class = cfg["top_level_class"]

    if backend is None:
        backend = LocalLLM() if use_local else ApiLLM()
    extractor = LLMExtractor(backend=backend)
    raw_data = extractor.extract(uml=uml_text, main_object=prompt_main_object, text=text)

    # 将抽取结果进一步拆分为「节点 + 边」结构，但不写入 Neo4j（不连接 Neo4j）
    mapper = SchemaMapper(uri=None)
    try:
        graph_struct = mapper.build_graph_structure(raw_data, top_level_class=top_level_class)
    finally:
        mapper.close()

    logger.info("extract_only done: main_object=%s elapsed=%.2fs", main_object, time.perf_counter() - t0)
    return {
        "raw": raw_data,
        "graph": graph_struct,
    }


def extract_with_templates_and_store(
    main_object: str,
    text: str,
    backend: Optional[BaseLLM] = None,
    use_local: bool = True,
    neo4j_uri: str = "bolt://localhost:7687",
    neo4j_user: str = "neo4j",
    neo4j_password: str = "neo4j2025",
    high_confidence: float = 0.9,
    low_confidence: float = 0.6,
):
    """
    使用「模板 + 大模型」的增强版本：

    1. 根据 main_object 载入 UML，并从其中解析 ExamplesTemplate 模板
    2. 先用正则在原文中匹配模板，抽取候选句子与槽位（模板优先）
    3. 将这些候选以提示形式拼接到原文前，交给 LLMExtractor 做最终 JSON 抽取
    4. 在抽取结果的顶层对象中增加/填充 confidence 字段：
       - 若存在模板匹配：confidence = high_confidence（默认 0.9）
       - 若无模板匹配：confidence = low_confidence（默认 0.6）
    5. 使用 SchemaMapper 将带置信度的结果写入 Neo4j

    不会修改原有的 extract_and_store 行为。
    """
    logger.info(
        "extract_with_templates_and_store: main_object=%s text_len=%d store=neo4j",
        main_object, len(text),
    )
    t0 = time.perf_counter()
    if main_object not in _UML_CONFIG:
        raise ValueError(f"不支持的主对象: {main_object}")

    cfg = _UML_CONFIG[main_object]
    uml_text = _load_uml_text(cfg["uml_file"])
    prompt_main_object = cfg["prompt_main_object"]
    top_level_class = cfg["top_level_class"]

    # 1) 从 UML 中解析模板并做正则匹配
    templates = parse_examples_templates_from_puml(uml_text)
    matches = match_templates(text, templates) if templates else []
    has_template_match = bool(matches)

    if has_template_match:
        hint = build_llm_hint_from_matches(matches)
        combined_text = f"{hint}\n\n【待抽取文本】\n{text}"
    else:
        combined_text = text

    # 2) 调用 LLM 抽取
    if backend is None:
        backend = LocalLLM() if use_local else ApiLLM()
    extractor = LLMExtractor(backend=backend)
    raw_data = extractor.extract(uml=uml_text, main_object=prompt_main_object, text=combined_text)

    # 3) 在结果中注入置信度
    data_with_conf = _inject_confidence(
        raw_data,
        has_template_match=has_template_match,
        high=high_confidence,
        low=low_confidence,
    )

    # 4) 写入 Neo4j
    mapper = SchemaMapper(uri=neo4j_uri, user=neo4j_user, password=neo4j_password)
    try:
        mapper.map_to_graph(data_with_conf, top_level_class=top_level_class)
    finally:
        mapper.close()

    logger.info(
        "extract_with_templates_and_store done: main_object=%s elapsed=%.2fs has_template_match=%s",
        main_object, time.perf_counter() - t0, has_template_match,
    )
    return data_with_conf


def extract_with_templates_only(
    main_object: str,
    text: str,
    backend: Optional[BaseLLM] = None,
    use_local: bool = True,
    high_confidence: float = 0.9,
    low_confidence: float = 0.6,
):
    """
    使用「模板 + LLM」进行抽取，但不写入 Neo4j，只返回带置信度的结构化结果。

    逻辑与 extract_with_templates_and_store 相同，只是省略入库步骤。
    """
    logger.info(
        "extract_with_templates_only: main_object=%s text_len=%d",
        main_object, len(text),
    )
    t0 = time.perf_counter()
    if main_object not in _UML_CONFIG:
        raise ValueError(f"不支持的主对象: {main_object}")

    cfg = _UML_CONFIG[main_object]
    uml_text = _load_uml_text(cfg["uml_file"])
    prompt_main_object = cfg["prompt_main_object"]
    top_level_class = cfg["top_level_class"]

    templates = parse_examples_templates_from_puml(uml_text)
    matches = match_templates(text, templates) if templates else []
    has_template_match = bool(matches)

    if has_template_match:
        hint = build_llm_hint_from_matches(matches)
        combined_text = f"{hint}\n\n【待抽取文本】\n{text}"
    else:
        combined_text = text

    if backend is None:
        backend = LocalLLM() if use_local else ApiLLM()
    extractor = LLMExtractor(backend=backend)
    raw_data = extractor.extract(uml=uml_text, main_object=prompt_main_object, text=combined_text)

    data_with_conf = _inject_confidence(
        raw_data,
        has_template_match=has_template_match,
        high=high_confidence,
        low=low_confidence,
    )

    # 同样拆分为「节点 + 边」结构，但不写入 Neo4j（不连接 Neo4j）
    mapper = SchemaMapper(uri=None)
    try:
        graph_struct = mapper.build_graph_structure(data_with_conf, top_level_class=top_level_class)
    finally:
        mapper.close()

    logger.info(
        "extract_with_templates_only done: main_object=%s elapsed=%.2fs has_template_match=%s",
        main_object, time.perf_counter() - t0, has_template_match,
    )
    return {
        "raw": data_with_conf,
        "graph": graph_struct,
    }


__all__ = [
    "extract_and_store",
    "extract_with_templates_and_store",
    "extract_only",
    "extract_with_templates_only",
]


def run_local_tests(
    use_local: bool = True,
    result_root: Optional[str] = None,
):
    """
    在本地批量测试 data 目录下的几个 test 文件，结果不写入 Neo4j，
    仅调用抽取接口并把结果保存到 uie/result 下对应的 JSON 文件中。

    当前约定：
    - data/rule/test_1.md -> main_object="RuleType"，使用模板增强（extract_with_templates_only）
    - data/rule/test_2.md -> main_object="RuleType"，使用模板增强（extract_with_templates_only）
    - data/systemElement/test.md -> main_object="SystemElement"，不使用模板（extract_only）
    - data/term/test.md -> main_object="Term"，不使用模板（extract_only）
    """
    base_dir = os.path.dirname(os.path.dirname(__file__))  # /home/.../uie
    data_root = os.path.join(base_dir, "data")
    result_root = result_root or os.path.join(base_dir, "result")

    configs = [
        {
            "rel_path": os.path.join("rule", "test_1.md"),
            "main_object": "RuleType",
            "use_templates": True,
        },
        {
            "rel_path": os.path.join("rule", "test_2.md"),
            "main_object": "RuleType",
            "use_templates": True,
        },
        {
            "rel_path": os.path.join("systemElement", "test.md"),
            "main_object": "SystemElement",
            "use_templates": False,
        },
        {
            "rel_path": os.path.join("term", "test.md"),
            "main_object": "Term",
            "use_templates": False,
        },
    ]

    os.makedirs(result_root, exist_ok=True)

    # 只加载一次模型，多个 test 文件共用同一个 backend，避免反复 Loading checkpoint
    backend = LocalLLM() if use_local else ApiLLM()

    logger.info(f"[run_local_tests] use_local={use_local}, result_root={result_root}")

    for cfg in configs:
        src_path = os.path.join(data_root, cfg["rel_path"])
        if not os.path.exists(src_path):
            logger.warning(f"[run_local_tests] 路径不存在，跳过: {src_path}")
            continue

        rel_path = cfg["rel_path"]
        main_object = cfg["main_object"]
        use_templates = cfg["use_templates"]

        logger.info(
            f"[run_local_tests] 开始处理: {rel_path} "
            f"(main_object={main_object}, use_templates={use_templates})"
        )

        try:
            with open(src_path, "r", encoding="utf-8") as f:
                text = f.read()

            if use_templates:
                data = extract_with_templates_only(
                    main_object=main_object,
                    text=text,
                    backend=backend,
                    use_local=use_local,
                )
            else:
                data = extract_only(
                    main_object=main_object,
                    text=text,
                    backend=backend,
                    use_local=use_local,
                )

            # 保存到 result 下对应子目录，改扩展名为 .json
            rel_no_ext = os.path.splitext(rel_path)[0]
            out_path = os.path.join(result_root, rel_no_ext + ".json")
            out_dir = os.path.dirname(out_path)
            os.makedirs(out_dir, exist_ok=True)

            with open(out_path, "w", encoding="utf-8") as fw:
                json.dump(data, fw, ensure_ascii=False, indent=2)

            logger.info(f"[run_local_tests] 已保存结果: {out_path}")

        except Exception as e:
            logger.exception(f"[run_local_tests] 处理 {rel_path} 时出错: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_local_tests()

