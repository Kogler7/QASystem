import os
import json
import logging
import time
from typing import Any, Optional

from ie.llm_extractor import LLMExtractor
from ie.utils import BaseLLM, ApiLLM
from ie.schema_mapper import SchemaMapper, json_to_graph_structure, store_graph_to_neo4j as _store_graph_to_neo4j
from ie.template_matcher import (
    parse_examples_templates_from_puml,
    match_templates,
    build_llm_hint_from_matches,
)
from ie.chunked_json import extract_from_chunked_payload, parse_chunked_json_bytes


UML_DIR = os.path.join(os.path.dirname(__file__), "UML")
logger = logging.getLogger(__name__)

# 主对象到 UML 文件及 SchemaMapper 顶层类型的映射
_UML_CONFIG = {
    "RuleType": {
        "uml_file": "RuleType.puml",
        "prompt_main_object": "RuleType",
        "top_level_class": "RuleType",
    },
    "SystemElement": {
        "uml_file": "SystemElement.puml",
        "prompt_main_object": "SystemElement",
        "top_level_class": "SystemElement",
    },
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
    """在抽取结果中增加/填充顶层置信度字段。支持 list 或 {MainObject: [...]} 格式。"""

    def _set_conf(obj: Any):
        if isinstance(obj, dict) and "confidence" not in obj:
            obj["confidence"] = high if has_template_match else low
        return obj

    if isinstance(raw_data, list):
        return [_set_conf(item) for item in raw_data]
    if isinstance(raw_data, dict) and len(raw_data) == 1:
        key, val = next(iter(raw_data.items()))
        if isinstance(val, list):
            raw_data[key] = [_set_conf(item) for item in val]
            return raw_data
    return _set_conf(raw_data)


def extract(
    main_object: str,
    text: str,
    *,
    use_templates: bool = True,
    store_to_neo4j: bool = False,
    backend: Optional[BaseLLM] = None,
    llm_base_url: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_api_key: Optional[str] = None,
    neo4j_uri: str = "bolt://localhost:7687",
    neo4j_user: str = "neo4j",
    neo4j_password: str = "neo4j2025",
    high_confidence: float = 0.9,
    low_confidence: float = 0.6,
) -> dict:
    """
    知识抽取统一接口。

    参数：
        main_object: "RuleType" / "SystemElement" / "Term"
        text: 待抽取文本
        use_templates: 是否使用模板匹配增强（默认 True）
        store_to_neo4j: 是否将抽取结果写入 Neo4j（默认 False）
        backend: 大模型后端，None 时使用默认 ApiLLM
        llm_base_url, llm_model, llm_api_key: 可选，用于自定义 API 地址/模型
        neo4j_uri, neo4j_user, neo4j_password: Neo4j 连接参数
        high_confidence, low_confidence: 模板匹配时的置信度

    返回：
        统一格式 {"raw": ..., "graph": ...}，其中 raw 为抽取的 JSON，graph 为节点+边结构。
    """
    logger.info(
        "extract: main_object=%s text_len=%d use_templates=%s store_to_neo4j=%s",
        main_object, len(text), use_templates, store_to_neo4j,
    )
    t0 = time.perf_counter()

    if main_object not in _UML_CONFIG:
        raise ValueError(f"不支持的主对象: {main_object}")

    cfg = _UML_CONFIG[main_object]
    uml_text = _load_uml_text(cfg["uml_file"])
    prompt_main_object = cfg["prompt_main_object"]
    top_level_class = cfg["top_level_class"]

    # 1) 根据是否使用模板准备输入文本
    if use_templates:
        templates = parse_examples_templates_from_puml(uml_text)
        matches = match_templates(text, templates) if templates else []
        has_template_match = bool(matches)
        if has_template_match:
            hint = build_llm_hint_from_matches(matches)
            combined_text = f"{hint}\n\n【待抽取文本】\n{text}"
        else:
            combined_text = text
    else:
        has_template_match = False
        combined_text = text

    # 2) 调用 LLM 抽取
    if backend is None:
        kwargs = {}
        if llm_base_url is not None:
            kwargs["base_url"] = llm_base_url
        if llm_model is not None:
            kwargs["model"] = llm_model
        if llm_api_key is not None:
            kwargs["api_key"] = llm_api_key
        backend = ApiLLM(**kwargs)
    extractor = LLMExtractor(backend=backend)
    raw_data = extractor.extract(uml=uml_text, main_object=prompt_main_object, text=combined_text)
    
    # 检查LLM抽取结果
    if not raw_data or (isinstance(raw_data, str) and not raw_data.strip()):
        logger.warning("LLM抽取结果为空，可能是LLM调用失败")
        # 返回空结果，避免后续处理出错
        return {
            "raw": {},
            "graph": {"nodes": [], "relationships": [], "ontology_relations": []}
        }

    # 3) 若使用模板，注入置信度
    if use_templates:
        raw_data = _inject_confidence(
            raw_data,
            has_template_match=has_template_match,
            high=high_confidence,
            low=low_confidence,
        )

    # 4) 将 JSON 拆分为图结构；top_level_class 从 raw 顶层键自动推断，传入 neo4j 可填充 ontology_relations
    graph_struct = json_to_graph_structure(
        raw_data,
        neo4j_uri=neo4j_uri,
        neo4j_user=neo4j_user,
        neo4j_password=neo4j_password,
    )

    # 5) 若需要，写入 Neo4j
    if store_to_neo4j:
        _store_graph_to_neo4j(
            graph_struct,
            uri=neo4j_uri,
            user=neo4j_user,
            password=neo4j_password,
        )

    logger.info(
        "extract done: main_object=%s elapsed=%.2fs has_template_match=%s",
        main_object, time.perf_counter() - t0, has_template_match if use_templates else "n/a",
    )
    return {
        "raw": raw_data,
        "graph": graph_struct,
    }


def store_graph_to_neo4j(
    graph_struct: dict,
    neo4j_uri: str = "bolt://localhost:7687",
    neo4j_user: str = "neo4j",
    neo4j_password: str = "neo4j2025",
) -> None:
    """
    将 extract() 返回的 graph 结构存储到 Neo4j。

    用户可先用 extract(..., store_to_neo4j=False) 得到结果并确认，
    再调用本函数将 graph 写入 Neo4j。

    参数：
        graph_struct: extract() 返回的 "graph" 字段
        neo4j_uri, neo4j_user, neo4j_password: Neo4j 连接参数
    """
    _store_graph_to_neo4j(
        graph_struct,
        uri=neo4j_uri,
        user=neo4j_user,
        password=neo4j_password,
    )
    logger.info("store_graph_to_neo4j: 已写入 Neo4j")


def extract_from_chunked_json_bytes(
    data: bytes,
    main_object: str,
    *,
    use_templates: bool = True,
    llm_base_url: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_api_key: Optional[str] = None,
) -> dict:
    """
    解析切块 JSON 文件内容（UTF-8），对每段文本执行与 extract() 相同的抽取流程，合并为统一的 raw + graph。
    返回格式与 extract() 一致：{"raw": ..., "graph": ...}。
    """
    payload = parse_chunked_json_bytes(data)
    out = extract_from_chunked_payload(
        payload,
        extract,
        main_object=main_object,
        use_templates=use_templates,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
    )
    return {"raw": out["raw"], "graph": out["graph"]}


__all__ = ["extract", "store_graph_to_neo4j", "extract_from_chunked_json_bytes"]


def run_local_tests(
    result_root: Optional[str] = None,
    llm_base_url: Optional[str] = None,
    llm_model: Optional[str] = None,
):
    """
    在本地批量测试 data 目录下的 test 文件，结果不写入 Neo4j。
    """
    base_dir = os.path.dirname(os.path.dirname(__file__))
    data_root = os.path.join(base_dir, "data")
    result_root = result_root or os.path.join(base_dir, "result")

    configs = [
        {"rel_path": os.path.join("rule", "test_1.md"), "main_object": "RuleType", "use_templates": True},
        {"rel_path": os.path.join("rule", "test_2.md"), "main_object": "RuleType", "use_templates": True},
        {"rel_path": os.path.join("systemElement", "test.md"), "main_object": "SystemElement", "use_templates": False},
        {"rel_path": os.path.join("term", "test.md"), "main_object": "Term", "use_templates": False},
    ]

    os.makedirs(result_root, exist_ok=True)
    kwargs = {}
    if llm_base_url is not None:
        kwargs["base_url"] = llm_base_url
    if llm_model is not None:
        kwargs["model"] = llm_model
    backend = ApiLLM(**kwargs) if kwargs else ApiLLM()
    logger.info("[run_local_tests] result_root=%s", result_root)

    for cfg in configs:
        src_path = os.path.join(data_root, cfg["rel_path"])
        if not os.path.exists(src_path):
            logger.warning("[run_local_tests] 路径不存在，跳过: %s", src_path)
            continue

        rel_path = cfg["rel_path"]
        main_object = cfg["main_object"]
        use_templates = cfg["use_templates"]
        logger.info("[run_local_tests] 开始处理: %s (main_object=%s use_templates=%s)", rel_path, main_object, use_templates)

        try:
            with open(src_path, "r", encoding="utf-8") as f:
                text = f.read()

            data = extract(
                main_object=main_object,
                text=text,
                use_templates=use_templates,
                store_to_neo4j=False,
                backend=backend,
            )

            rel_no_ext = os.path.splitext(rel_path)[0]
            out_path = os.path.join(result_root, rel_no_ext + ".json")
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as fw:
                json.dump(data, fw, ensure_ascii=False, indent=2)
            logger.info("[run_local_tests] 已保存: %s", out_path)
        except Exception as e:
            logger.exception("[run_local_tests] 处理 %s 时出错: %s", rel_path, e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_local_tests()
