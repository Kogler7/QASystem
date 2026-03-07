"""
知识抽取：进程内调用 algorithm/uie 算法，按函数封装，不依赖独立 UIE 服务。
使用前需安装 algorithm/uie 依赖（见 algorithm/uie/README.md）。
"""
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[5]
_UIE_ROOT = _ROOT / "algorithm" / "uie"
if _UIE_ROOT.exists() and str(_UIE_ROOT) not in sys.path:
    sys.path.insert(0, str(_UIE_ROOT))

_extraction_api = None


def _get_extraction_api():
    global _extraction_api
    if _extraction_api is None:
        try:
            from ie import extraction_api as m
            _extraction_api = m
        except Exception as e:
            logger.warning("加载知识抽取模块失败: %s", e)
            raise
    return _extraction_api


def _normalize_graph(graph: dict[str, Any]) -> dict[str, Any]:
    if not graph:
        return graph
    nodes = []
    for n in graph.get("nodes") or []:
        props = n.get("properties") or {}
        name = props.get("RuleName") or props.get("TermId") or props.get("name") or n.get("uid") or ""
        nodes.append({"id": n.get("uid"), "label": n.get("label"), "name": name, "properties": props})
    rels = []
    for r in graph.get("relationships") or []:
        rels.append({"source": r.get("from_uid"), "target": r.get("to_uid"), "type": r.get("type")})
    onto = []
    for o in graph.get("ontology_relations") or []:
        onto.append({"source": o.get("from_uid"), "target": o.get("class_name"), "type": o.get("type")})
    return {"nodes": nodes, "relationships": rels, "ontology_relations": onto}


def health() -> dict[str, Any]:
    try:
        _get_extraction_api()
        return {"status": "ok"}
    except Exception as e:
        return {
            "status": "unavailable",
            "detail": f"知识抽取模块不可用: {e}。请确认已安装 algorithm/uie 依赖（见 algorithm/uie/README.md）。",
        }


def extract(
    main_object: str,
    text: str,
    use_local: bool = True,
    use_templates: bool = True,
    store_to_neo4j: bool = False,
) -> dict[str, Any]:
    api = _get_extraction_api()
    from ie.schema_mapper import SchemaMapper

    if store_to_neo4j:
        if use_templates:
            raw_result = api.extract_with_templates_and_store(main_object=main_object, text=text, use_local=use_local)
        else:
            raw_result = api.extract_and_store(main_object=main_object, text=text, use_local=use_local)
        raw_list = raw_result if isinstance(raw_result, list) else [raw_result]
        mapper = SchemaMapper(uri=None)
        try:
            graph_struct = mapper.build_graph_structure(raw_list, top_level_class=main_object)
        finally:
            mapper.close()
        data = {"raw": raw_list, "graph": _normalize_graph(graph_struct)}
    else:
        if use_templates:
            result = api.extract_with_templates_only(main_object=main_object, text=text, use_local=use_local)
        else:
            result = api.extract_only(main_object=main_object, text=text, use_local=use_local)
        data = {
            "raw": result.get("raw", result) if isinstance(result, dict) else result,
            "graph": _normalize_graph(result.get("graph", {})) if isinstance(result, dict) else _normalize_graph({}),
        }
    return {"data": data}
