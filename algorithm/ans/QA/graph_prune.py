"""LLM-based graph exploration pruning.
每一步探索时，先获取全部邻居，再由 LLM 对关系和实体进行两轮打分剪枝，
只返回最相关的少量路径。沿用原 wiki_func.py 的剪枝思路，适配 search_graph 的三元组格式。

剪枝流程:
  1. 从 triples 中提取去重关系列表
  2. LLM 对关系打分 → 保留 top N 个关系
  3. 每个保留下来的关系下，LLM 对实体打分 → 保留 top K 个实体
  4. 返回剪枝后的 triples
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from algorithm.ans.QA.utils import run_llm
from algorithm.ans.QA.prompt_list import graph_relation_prune_prompt, graph_entity_prune_prompt

logger = logging.getLogger(__name__)


def prune_triples(
    question: str,
    triples: list[dict[str, Any]],
    entity_name: str = "",
    max_relations: int = 3,
    max_entities_per_relation: int = 3,
    api_key: str = "",
    model: str = "",
) -> list[dict[str, Any]]:
    """LLM 两轮打分剪枝。

    如果 triples 总量已经很少，直接返回不做剪枝。
    如果 LLM 调用失败，回退到原始 triples 不做裁剪。
    """
    if not triples or len(triples) <= max_relations * max_entities_per_relation:
        return triples

    # ── 第一轮：关系剪枝 ──
    # 提取去重的有序关系列表
    relation_order: list[str] = []
    for triple in triples:
        rel = triple.get("relation", "")
        if rel not in relation_order:
            relation_order.append(rel)

    if len(relation_order) <= 1:
        # 只有一个关系，不需要 LLM 判断
        kept_relations = relation_order
    else:
        relations_text = "\n".join(
            f"- {rel}（{sum(1 for t in triples if t.get('relation') == rel)} 个邻居）"
            for rel in relation_order
        )
        prompt = graph_relation_prune_prompt.format(
            question=question,
            entity_name=entity_name or "当前节点",
            relations_text=relations_text,
        )
        try:
            response = run_llm(prompt, 0.1, 500, api_key, model)
            logger.debug("Relation prune LLM response: %s", response[:200])
            rel_data = _parse_json(response)
            rel_scores: dict[str, float] = {}
            for item in rel_data.get("relations", []):
                name = str(item.get("name", "")).strip()
                score = float(item.get("score", 0))
                if name and score > 0:
                    rel_scores[name] = score
            if rel_scores:
                kept_relations = sorted(rel_scores, key=rel_scores.get, reverse=True)[:max_relations]
            else:
                kept_relations = relation_order[:max_relations]
        except Exception as exc:
            logger.warning("关系剪枝 LLM 调用失败: %s，保留全部关系", exc)
            kept_relations = relation_order[:max_relations]

    # ── 第二轮：实体剪枝 ──
    kept_triples: list[dict[str, Any]] = []
    for rel in kept_relations:
        rel_triples = [t for t in triples if t.get("relation") == rel]

        if len(rel_triples) <= max_entities_per_relation:
            kept_triples.extend(rel_triples)
            continue

        entities_text = "\n".join(
            "- {}（标签：{}）".format(
                t.get("tail_entity", {}).get("name", "未知"),
                ", ".join(t.get("tail_entity", {}).get("labels", [])),
            )
            for t in rel_triples
        )

        prompt = graph_entity_prune_prompt.format(
            question=question,
            relation=rel,
            entities_text=entities_text,
        )
        try:
            response = run_llm(prompt, 0.1, 500, api_key, model)
            logger.debug("Entity prune LLM response: %s", response[:200])
            ent_data = _parse_json(response)
            ent_scores: dict[str, float] = {}
            for item in ent_data.get("entities", []):
                name = str(item.get("name", "")).strip()
                score = float(item.get("score", 0))
                if name and score > 0:
                    ent_scores[name] = score

            if ent_scores:
                scored = [
                    (t, ent_scores.get(t.get("tail_entity", {}).get("name", ""), 0))
                    for t in rel_triples
                ]
                scored.sort(key=lambda x: x[1], reverse=True)
                kept_triples.extend(t for t, _ in scored[:max_entities_per_relation])
            else:
                kept_triples.extend(rel_triples[:max_entities_per_relation])
        except Exception as exc:
            logger.warning("实体剪枝 LLM 调用失败 (rel=%s): %s，保留前 %d 个", rel, exc, max_entities_per_relation)
            kept_triples.extend(rel_triples[:max_entities_per_relation])

    logger.info(
        "剪枝完成: %d triples → %d triples（保留 %d 个关系）",
        len(triples), len(kept_triples), len(kept_relations),
    )
    return kept_triples


def _parse_json(text: str) -> dict[str, Any]:
    """从 LLM 响应中提取 JSON。"""
    text = (text or "").strip()
    if not text:
        return {}

    # 移除 think 标签
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL)
    text = text.strip()

    # 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 正则提取
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    logger.warning("无法从 LLM 响应中解析 JSON: %s", text[:200])
    return {}
