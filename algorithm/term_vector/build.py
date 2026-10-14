"""离线构建 Term 语义向量索引。

用法（在内网、env 已配好向量模型的环境执行；GPU 版走 VECTOR_MODEL_PATH 本地模型）：
    python -m algorithm.term_vector.build --output /path/term_index.json

流程：
  1. 按实体链接同款过滤拉取全部 :Term（KnowledgeUnit 中 resource_type CONTAINS 'Term'）；
  2. 从 KnowledgeProperty 逻辑属性的 properties_json 中解析 TermDefinition / Synonyms；
  3. 拼接向量化文本 = TermName + Synonyms + TermDefinition，调用 batch_text_to_vectors
     （与后端 /api/v1/embeddings/texts 同源）生成向量；
  4. 存成单一 JSON（records + vectors），供 entity_linker 运行时加载做向量召回。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

# 与后端 /api/v1/embeddings/texts 同源：按 env 自动选本地模型(VECTOR_MODEL_PATH)或 remote 回退
from algorithm.NL_to_cypher.vector import batch_text_to_vectors


def _split_synonyms(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            parts.extend(_split_synonyms(item))
        return [p for p in dict.fromkeys(parts) if p]
    text = str(value)
    for sep in ("；", ";", "，", ",", "、", "|"):
        text = text.replace(sep, "\n")
    return [p.strip() for p in text.splitlines() if p.strip()]


# Only the registered application gateway may query graph data. Standalone
# CLI calls must already have a user context; there is no direct DB fallback.
def _gateway_query(statement: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
    from external_services.application import get_user_headers, ExternalCapabilityUnavailable
    if not get_user_headers().get("authorization"):
        raise ExternalCapabilityUnavailable("graph.cypher.query", "术语索引构建需要已绑定的用户授权上下文；不支持独立直连图数据库")
    from backend.app.execution.manager import get_execution_manager, ExecutionUnavailable
    manager = get_execution_manager()
    if not manager.is_async:
        raise ExecutionUnavailable("术语索引取数必须使用共享执行队列")
    submission = manager.submit(operation="term_vector.query", resource_class="io.db",
                                payload={"statement": statement, "parameters": parameters})
    execution = manager.wait_blocking(submission.job_id, None)
    if execution["status"] != "succeeded":
        raise RuntimeError("术语索引应用服务查询任务未成功完成")
    return manager.result(submission.job_id)


def fetch_terms(limit: int, term_type: str) -> list[dict[str, Any]]:
    """通过认证队列和应用服务拉取 Term 节点。"""
    statement = (
        "MATCH (n:KnowledgeUnit) "
        "WHERE n.resource_type CONTAINS $term_type "
        "RETURN n.id AS id, n.name AS name, n.definition AS definition, "
        "       n.synonyms AS synonyms, n.content AS content, "
        "       n.domain AS domain, n.tags AS tags "
        "LIMIT $limit"
    )
    parameters = {"term_type": term_type, "limit": limit}
    return [dict(row) for row in _gateway_query(statement, parameters)]


def fetch_logical_props(term_type: str, limit: int) -> dict[str, list[dict[str, Any]]]:
    """批量取 Term 的 KnowledgeProperty 逻辑属性，供字段缺失时兜底。"""
    statement = (
        "MATCH (n:KnowledgeUnit)-[:HAS_LOGICAL_PROPERTY]->(p:KnowledgeProperty) "
        "WHERE n.resource_type CONTAINS $term_type "
        "RETURN n.id AS id, p.name AS name, p.property_type AS property_type, "
        "       p.properties_json AS properties_json "
        "LIMIT $limit"
    )
    parameters = {"term_type": term_type, "limit": limit}
    rows = _gateway_query(statement, parameters)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("id") or ""), []).append(dict(row))
    return grouped


def build_enriched_text(term: dict[str, Any]) -> str:
    name = str(term.get("name") or "").strip()
    synonyms = term.get("synonyms") or []
    definition = str(term.get("definition") or "").strip()
    domain = str(term.get("domain") or "").strip()

    parts = []
    if name:
        parts.append(f"术语名称：{name}")
    if synonyms:
        parts.append("同义词：" + "、".join(synonyms))
    if definition:
        parts.append(f"术语定义：{definition}")
    if domain:
        parts.append(f"所属领域：{domain}")
    return "；".join(parts)


def _logical_entries(props: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 KnowledgeProperty 的 properties_json 展开成 [{key, value}, ...]。

    实际数据里是形如 [{"key":"TermName","value":...},{"key":"TermDefinition","value":...}] 的 JSON。
    """
    entries: list[dict[str, Any]] = []
    for prop in props:
        raw = prop.get("properties_json")
        data: Any = None
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except Exception:
                data = raw
        elif raw is not None:
            data = raw
        if isinstance(data, list):
            entries.extend(item for item in data if isinstance(item, dict))
        elif isinstance(data, dict):
            entries.append(data)
        elif raw is not None:
            pname = str(prop.get("name") or "").strip()
            if pname:
                entries.append({"key": pname, "value": raw})
    return entries


def _extract_logical(props: list[dict[str, Any]]) -> dict[str, Any]:
    """从逻辑属性中提取规范字段，返回 {name, definition, synonyms, domain}。"""
    out: dict[str, Any] = {"name": "", "definition": "", "synonyms": [], "domain": ""}
    for entry in _logical_entries(props):
        key = str(entry.get("key") or entry.get("name") or "").strip()
        value = entry.get("value")
        low = key.lower()
        if low in {"termdefinition", "definition", "定义", "释义", "术语定义"}:
            if value is not None and not out["definition"]:
                out["definition"] = str(value).strip()
        elif low in {"synonyms", "synonym", "同义词", "别名", "termsynonyms"}:
            if not out["synonyms"]:
                out["synonyms"] = _split_synonyms(value)
        elif low in {"termname", "name", "术语名称"}:
            if value is not None and not out["name"]:
                out["name"] = str(value).strip()
        elif low in {"domain", "领域", "业务域", "所属领域"}:
            if value is not None and not out["domain"]:
                out["domain"] = str(value).strip()
    return out


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="构建 Term 语义向量索引")
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[2] / "backend" / "data" / "term_index.json"),
        help="索引输出路径，默认写入可写backend-data卷",
    )
    parser.add_argument("--limit", type=int, default=10000, help="最大拉取 Term 条数")
    parser.add_argument(
        "--term-type",
        default="Term",
        help="resource_type 过滤关键词（默认 Term，与实体链接一致）",
    )
    parser.add_argument("--no-logical", action="store_true", help="跳过逻辑属性兜底")
    args = parser.parse_args(argv)

    print("取数通道：应用团队 Cypher 网关，共享认证队列；需要已绑定用户授权上下文。")

    terms = fetch_terms(args.limit, args.term_type)
    if not terms:
        raise SystemExit(f"未拉到 Term（term_type 含 '{args.term_type}'）。"
                         f"请确认图谱可达且该谓词正确，可用 --term-type 调整。")

    logical: dict[str, list[dict[str, Any]]] = {}
    if not args.no_logical:
        logical = fetch_logical_props(args.term_type, args.limit * 4)

    records: list[dict[str, Any]] = []
    texts: list[str] = []
    missing = {"definition": 0, "synonyms": 0}
    for raw in terms:
        term = dict(raw)
        node_id = str(term.get("id") or "")
        term["id"] = node_id
        extra = _extract_logical(logical.get(node_id, [])) if logical else {}
        content = str(term.get("content") or "").strip()
        definition = (
            str(term.get("definition") or "").strip()
            or content
            or str(extra.get("definition") or "").strip()
        )
        synonyms = _split_synonyms(term.get("synonyms")) or list(extra.get("synonyms") or [])
        domain = (
            str(term.get("domain") or "").strip()
            or str(extra.get("domain") or "").strip()
        )
        if not definition:
            missing["definition"] += 1
        if not synonyms:
            missing["synonyms"] += 1

        term["name"] = str(term.get("name") or "").strip() or extra.get("name") or ""
        term["definition"] = definition
        term["synonyms"] = synonyms
        if domain:
            term["domain"] = domain
        else:
            term.pop("domain", None)

        enriched = build_enriched_text(term)
        term["enriched_text"] = enriched
        texts.append(enriched)
        records.append(term)

    raw_vectors = batch_text_to_vectors(texts)
    vectors = np.asarray(raw_vectors, dtype=np.float32)
    if vectors.ndim != 2 or vectors.shape[0] != len(records):
        raise SystemExit("Embedding 返回数量与 Term 条数不一致，中止")

    from algorithm.term_vector.index import TermVectorIndex

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    TermVectorIndex(records, vectors).to_json(args.output)

    print(f"Term 总数：{len(records)}")
    print(f"缺定义(回退 content/逻辑属性)：{missing['definition']}，无同义词：{missing['synonyms']}")
    print(f"向量维度：{vectors.shape[1]}；输出：{args.output}")
    for sample in records[:3]:
        print("-" * 40)
        print(f"id={sample.get('id')} name={sample.get('name')}")
        print(f"  synonyms={sample.get('synonyms')}")
        print(f"  enriched={sample.get('enriched_text')[:160]}")


if __name__ == "__main__":
    main()
