"""修正版 LLM judge。

旧实现让 judge 直接输出全体 golden/predicted 的全局 index；列表一长模型就退化成
顺序枚举（例如 simple 输出 (i,i+1)、standard_02 输出 (0,0)..(19,19)），TP 完全失真。

本模块的修正思路：
1. 本地先做名称归一化的短名单（每个 item 最多保留 top-K 候选），把 O(n*m) 的
   候选空间压到几十~几百对；
2. 同 type 同名的直接判对，不发 LLM；
3. 其余灰色候选按小批（默认 8 对/批）发给 judge，但 **只输出 pair_id + matched**，
   不输出任何全局 index；返回后本地按 pair_id/名称回验，伪造的 pair_id 直接丢弃；
4. 一对一贪心分配后计算 P/R/F1；LLM 失败时退回本地确定性判定，保证离线可复现。
"""

from __future__ import annotations

import difflib
import json
import re
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable

_ENTITY_BATCH_SIZE = 8
_RELATION_BATCH_SIZE = 8
_MAX_CANDIDATES_PER_ITEM = 3
_MIN_NAME_SIMILARITY = 0.55
_NEAR_NAME_AUTO = 0.93
_MIN_REL_SIMILARITY = 0.6


def _norm_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"[\s\u3000]+", "", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)
    return text


def _norm_type(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"[\s_\-]+", "", text)


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if not (set(a) & set(b)):
        return 0.0
    matcher = difflib.SequenceMatcher(None, a, b)
    if matcher.real_quick_ratio() < _MIN_NAME_SIMILARITY or matcher.quick_ratio() < _MIN_NAME_SIMILARITY:
        return 0.0
    return matcher.ratio()


def _name_similarity(gold: dict[str, Any], predicted: dict[str, Any]) -> float:
    gn = _norm_name(gold.get("name"))
    pn = _norm_name(predicted.get("name"))
    if not gn or not pn:
        return 0.0
    score = _similarity(gn, pn)
    if gn == pn:
        score = max(score, 0.99)
    elif min(len(gn), len(pn)) >= 3 and (gn in pn or pn in gn):
        score = max(score, 0.8)
    return score


def _rel_similarity(gold: dict[str, Any], predicted: dict[str, Any]) -> float:
    return _similarity(_norm_type(gold.get("rel_type")), _norm_type(predicted.get("rel_type")))


def _simple_entities(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for entity in items:
        result.append({
            "type": entity.get("type"),
            "name": entity.get("name"),
            "properties": dict(entity.get("properties") or {}),
        })
    return result


def _simple_relations(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for relation in items:
        item = {
            "from_type": relation.get("from_type"),
            "from_name": relation.get("from_name"),
            "rel_type": relation.get("rel_type"),
            "to_type": relation.get("to_type"),
            "to_name": relation.get("to_name"),
        }
        properties = relation.get("properties")
        if isinstance(properties, dict) and properties:
            item["properties"] = dict(properties)
        result.append(item)
    return result


def _merge_stats(target: dict[str, int], extra: dict[str, int]) -> None:
    for key, value in extra.items():
        target[key] = int(target.get(key, 0)) + int(value)


def _calculate_metrics(tp: int, golden: int, predicted: int) -> dict[str, float]:
    precision = tp / predicted if predicted else 0.0
    recall = tp / golden if golden else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "golden": golden,
        "predicted": predicted,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "tp": tp,
        "fp": max(0, predicted - tp),
        "fn": max(0, golden - tp),
    }


def _decode_response(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    text = str(response).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("judge 返回必须是 JSON object")
    return data


_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pair_id": {"type": "string"},
                    "matched": {"type": "boolean"},
                },
                "required": ["pair_id", "matched"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


_ENTITY_SYSTEM = """你是知识抽取评测员。下面给出若干候选配对，每对包含 golden 和 predicted 两个实体。
判断规则：
1. predicted 的 type 与 golden 指向同一个本体类型即可，字符串可以不同（如 Section/Clause 这种层级/别名差异由你判断是否同一语义）。
2. name 指向同一实体即可，允许同义表达、别名、规范名、标点/空格/全半角差异。
3. properties：两侧都出现的属性，键和值应一致（允许日期格式、全半角、数值精度差异）；golden 没有的属性不判错。
4. 每个 pair_id 独立判断，只输出 {"verdicts":[{"pair_id":"...","matched":true/false}]}，不要输出解释。"""

_RELATION_SYSTEM = """你是知识抽取评测员。下面给出若干候选配对，每对包含 golden 和 predicted 两个关系。
判断规则：
1. rel_type 语义等价即可（允许大小写/下划线差异、同义关系表达）。
2. from/to 两个端点必须分别对应到同一实体，方向不能反。
3. properties：两侧都出现的属性键与值应一致；golden 没有的属性不判错。
4. 每个 pair_id 独立判断，只输出 {"verdicts":[{"pair_id":"...","matched":true/false}]}，不要输出解释。"""


class FixedJudge:
    """短名单 + 名称级小批校验的 judge，替代旧的全量 index 匹配。"""

    def __init__(
        self,
        client: Any,
        *,
        batch_size: int = _ENTITY_BATCH_SIZE,
        relation_batch_size: int = _RELATION_BATCH_SIZE,
        max_workers: int = 4,
        logger: Any = print,
    ) -> None:
        self.client = client
        self.batch_size = max(1, int(batch_size))
        self.relation_batch_size = max(1, int(relation_batch_size))
        self.max_workers = max(1, int(max_workers))
        self.logger = logger

    # ------------------------------------------------------------------ LLM

    def _verify_batch(self, pairs: list[dict[str, Any]], kind: str) -> dict[str, bool]:
        valid_ids = {str(item["pair_id"]) for item in pairs}
        system = _ENTITY_SYSTEM if kind == "entity" else _RELATION_SYSTEM
        user = json.dumps({"pairs": pairs}, ensure_ascii=False)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        last_error: Exception | None = None
        for mode in ("json_schema", "json_object", "plain"):
            try:
                response = self.client.generate_structured(
                    schema=_VERDICT_SCHEMA,
                    messages=messages,
                    run_context={
                        "schema_name": "knowledge_extract_v2_fixed_judge",
                        "response_mode": mode,
                        "prefer_chat_json_schema": True,
                    },
                )
                data = _decode_response(response)
                verdicts = data.get("verdicts")
                if not isinstance(verdicts, list):
                    raise ValueError("verdicts 必须是数组")
                result: dict[str, bool] = {}
                for item in verdicts:
                    if not isinstance(item, dict):
                        continue
                    pair_id = str(item.get("pair_id") or "")
                    matched = item.get("matched")
                    if pair_id in valid_ids and isinstance(matched, bool):
                        result[pair_id] = matched
                if result:
                    return result
                raise ValueError("没有可用的 verdict")
            except Exception as exc:  # noqa: BLE001 - 逐模式回退
                last_error = exc
        raise RuntimeError(f"fixed judge {kind} 批次失败: {last_error}")

    def _verify_pairs(self, pairs: list[dict[str, Any]], kind: str) -> tuple[dict[str, bool], dict[str, int]]:
        """并发校验灰色候选；返回 {pair_id: matched} 和统计。"""
        stats = {"llm_pairs": len(pairs), "llm_accept": 0, "llm_reject": 0, "llm_fallback": 0}
        if not pairs:
            return {}, stats
        batch_size = self.batch_size if kind == "entity" else self.relation_batch_size
        batches = [pairs[i:i + batch_size] for i in range(0, len(pairs), batch_size)]
        accepted: dict[str, bool] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="fixed-judge") as executor:
            futures = {executor.submit(self._verify_batch, batch, kind): batch for batch in batches}
            for future in as_completed(futures):
                batch = futures[future]
                try:
                    verdict = future.result()
                except Exception as exc:  # noqa: BLE001 - 单批失败走确定性回退
                    self.logger(f"[fixed-judge] {kind} 批次失败，走本地回退: {exc}")
                    verdict = {}
                    stats["llm_fallback"] += len(batch)
                    fallback_accept = 0
                    for item in batch:
                        pair_id = str(item["pair_id"])
                        if kind == "entity":
                            keep = float(item.get("name_similarity") or 0.0) >= 0.9 and bool(item.get("same_type"))
                        else:
                            keep = float(item.get("rel_similarity") or 0.0) >= 0.9
                        verdict[pair_id] = keep
                        fallback_accept += int(keep)
                    stats["llm_accept"] += fallback_accept
                    stats["llm_reject"] += len(batch) - fallback_accept
                    accepted.update(verdict)
                    continue
                for item in batch:
                    pair_id = str(item["pair_id"])
                    accepted[pair_id] = bool(verdict.get(pair_id, False))
                stats["llm_accept"] += sum(1 for item in batch if accepted[str(item["pair_id"])])
                stats["llm_reject"] += sum(1 for item in batch if not accepted[str(item["pair_id"])])
        return accepted, stats

    # -------------------------------------------------------------- entities

    def _entity_candidates(
        self,
        golden: list[dict[str, Any]],
        predicted: list[dict[str, Any]],
    ) -> dict[tuple[int, int], float]:
        candidates: dict[tuple[int, int], float] = {}
        for j, pitem in enumerate(predicted):
            scored: list[tuple[float, int]] = []
            for i, gitem in enumerate(golden):
                score = _name_similarity(gitem, pitem)
                if score >= _MIN_NAME_SIMILARITY:
                    scored.append((score, i))
            for score, i in sorted(scored, reverse=True)[:_MAX_CANDIDATES_PER_ITEM]:
                candidates[(i, j)] = max(candidates.get((i, j), 0.0), score)
        # 保证每个 golden 至少保留最相似的 predicted，避免短名单漏配
        for i, gitem in enumerate(golden):
            if any(key[0] == i for key in candidates):
                continue
            best_score, best_j = 0.0, -1
            for j, pitem in enumerate(predicted):
                score = _name_similarity(gitem, pitem)
                if score > best_score:
                    best_score, best_j = score, j
            if best_j >= 0 and best_score >= _MIN_NAME_SIMILARITY:
                candidates[(i, best_j)] = best_score
        return candidates

    def _match_entities(
        self,
        golden: list[dict[str, Any]],
        predicted: list[dict[str, Any]],
    ) -> tuple[list[tuple[int, int, float, str]], dict[str, int]]:
        candidates = self._entity_candidates(golden, predicted)
        auto: list[tuple[int, int, float, str]] = []
        pending: list[dict[str, Any]] = []
        stats = {"pairs_total": len(candidates), "auto": 0, "llm_pairs": 0, "llm_accept": 0, "llm_reject": 0, "llm_fallback": 0}
        for (i, j), score in candidates.items():
            gitem, pitem = golden[i], predicted[j]
            gn, pn = _norm_name(gitem.get("name")), _norm_name(pitem.get("name"))
            gt, pt = _norm_type(gitem.get("type")), _norm_type(pitem.get("type"))
            if gn and gn == pn and gt == pt:
                auto.append((i, j, 1.0, "exact_name_type"))
                continue
            if gt == pt and score >= _NEAR_NAME_AUTO:
                auto.append((i, j, 0.95, "near_name_same_type"))
                continue
            pending.append({
                "pair_id": f"E-{i}-{j}",
                "golden": _simple_entities([gitem])[0],
                "predicted": _simple_entities([pitem])[0],
                "name_similarity": round(score, 4),
                "same_type": gt == pt,
                "_golden_index": i,
                "_predicted_index": j,
            })
        stats["auto"] = len(auto)
        # 灰色候选择优限流：每个 golden/predicted 最多 3 条，避免 LLM 批次爆炸
        pending.sort(key=lambda item: item["name_similarity"], reverse=True)
        limited: list[dict[str, Any]] = []
        used_g: dict[int, int] = defaultdict(int)
        used_p: dict[int, int] = defaultdict(int)
        for item in pending:
            gi, pi = item["_golden_index"], item["_predicted_index"]
            if used_g[gi] >= _MAX_CANDIDATES_PER_ITEM or used_p[pi] >= _MAX_CANDIDATES_PER_ITEM:
                continue
            used_g[gi] += 1
            used_p[pi] += 1
            limited.append(item)
        verdicts, llm_stats = self._verify_pairs(limited, "entity")
        _merge_stats(stats, llm_stats)

        def assign(rows: list[tuple[int, int, float, str]]) -> list[tuple[int, int, float, str]]:
            ordered = sorted(rows, key=lambda row: (-row[2], -row[1], row[0]))
            used_gi: set[int] = set()
            used_pi: set[int] = set()
            result: list[tuple[int, int, float, str]] = []
            for gi, pi, score, reason in ordered:
                if gi in used_gi or pi in used_pi:
                    continue
                used_gi.add(gi)
                used_pi.add(pi)
                result.append((gi, pi, score, reason))
            return result

        matched: list[tuple[int, int, float, str]] = list(auto)
        for item in limited:
            if verdicts.get(item["pair_id"]):
                matched.append((
                    item["_golden_index"],
                    item["_predicted_index"],
                    0.9 + float(item["name_similarity"]) * 0.05,
                    "llm_accept",
                ))
        final = assign(matched)

        # 第二阶段：名称短名单对不上、但同类型的语义候选回捞。
        # 处理 official 这类 golden 名是概括词、predicted 是原文长句的情况。
        matched_g = {gi for gi, _, _, _ in final}
        matched_p = {pi for _, pi, _, _ in final}
        unmatched_g = [i for i in range(len(golden)) if i not in matched_g]
        unmatched_p = [j for j in range(len(predicted)) if j not in matched_p]
        by_type_p: dict[str, list[int]] = defaultdict(list)
        for j in unmatched_p:
            by_type_p[_norm_type(predicted[j].get("type"))].append(j)
        decided = {(gi, pi) for gi, pi in candidates}
        fallback: list[dict[str, Any]] = []
        used_g3: dict[int, int] = defaultdict(int)
        used_p3: dict[int, int] = defaultdict(int)
        for gi in unmatched_g:
            same_type = by_type_p.get(_norm_type(golden[gi].get("type")), [])
            if not same_type:
                continue
            ranked = sorted(
                same_type,
                key=lambda j: _name_similarity(golden[gi], predicted[j]),
                reverse=True,
            )
            for pi in ranked:
                if (gi, pi) in decided:
                    continue
                if used_g3[gi] >= _MAX_CANDIDATES_PER_ITEM or used_p3[pi] >= _MAX_CANDIDATES_PER_ITEM:
                    continue
                fallback.append({
                    "pair_id": f"E2-{gi}-{pi}",
                    "golden": _simple_entities([golden[gi]])[0],
                    "predicted": _simple_entities([predicted[pi]])[0],
                    "name_similarity": round(_name_similarity(golden[gi], predicted[pi]), 4),
                    "same_type": True,
                    "_golden_index": gi,
                    "_predicted_index": pi,
                })
                used_g3[gi] += 1
                used_p3[pi] += 1
        stats["semantic_pairs"] = len(fallback)
        if fallback:
            fallback_verdicts, fallback_stats = self._verify_pairs(fallback, "entity")
            _merge_stats(stats, fallback_stats)
            extra_rows: list[tuple[int, int, float, str]] = []
            for item in fallback:
                if fallback_verdicts.get(item["pair_id"]):
                    extra_rows.append((
                        item["_golden_index"],
                        item["_predicted_index"],
                        0.85,
                        "llm_accept_semantic",
                    ))
            if extra_rows:
                # 第二阶段只补 final 没覆盖到的 item
                remaining_g = {i for i in range(len(golden)) if i not in matched_g}
                remaining_p = {j for j in range(len(predicted)) if j not in matched_p}
                narrowed = [row for row in extra_rows if row[0] in remaining_g and row[1] in remaining_p]
                final.extend(assign(narrowed))
        return final, stats

    # ------------------------------------------------------------- relations

    def _entity_lookup(self, entities: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], list[int]], dict[str, list[int]]]:
        typed: dict[tuple[str, str], list[int]] = defaultdict(list)
        named: dict[str, list[int]] = defaultdict(list)
        for index, entity in enumerate(entities):
            name = _norm_name(entity.get("name"))
            ntype = _norm_type(entity.get("type"))
            typed[(name, ntype)].append(index)
            named[name].append(index)
        return typed, named

    def _endpoint_indices(
        self,
        relation: dict[str, Any],
        side: str,
        typed: dict[tuple[str, str], list[int]],
        named: dict[str, list[int]],
    ) -> set[int]:
        name = _norm_name(relation.get(f"{side}_name"))
        ntype = _norm_type(relation.get(f"{side}_type"))
        exact = typed.get((name, ntype))
        if exact:
            return set(exact)
        return set(named.get(name, []))

    def _match_relations(
        self,
        golden: list[dict[str, Any]],
        predicted: list[dict[str, Any]],
        entity_mapping: dict[int, int],
        golden_entities: list[dict[str, Any]],
        predicted_entities: list[dict[str, Any]],
    ) -> tuple[list[tuple[int, int, float, str]], dict[str, int]]:
        g_typed, g_named = self._entity_lookup(golden_entities)
        p_typed, p_named = self._entity_lookup(predicted_entities)
        # 预计算预测关系的端点实体 index 集合
        pred_endpoints: list[tuple[set[int], set[int]]] = []
        for relation in predicted:
            pred_endpoints.append((
                self._endpoint_indices(relation, "from", p_typed, p_named),
                self._endpoint_indices(relation, "to", p_typed, p_named),
            ))
        auto: list[tuple[int, int, float, str]] = []
        pending: list[dict[str, Any]] = []
        stats = {"pairs_total": 0, "auto": 0, "llm_pairs": 0, "llm_accept": 0, "llm_reject": 0, "llm_fallback": 0}
        for gi, grel in enumerate(golden):
            gfrom = self._endpoint_indices(grel, "from", g_typed, g_named)
            gto = self._endpoint_indices(grel, "to", g_typed, g_named)
            mapped_from = {entity_mapping[i] for i in gfrom if i in entity_mapping}
            mapped_to = {entity_mapping[i] for i in gto if i in entity_mapping}
            if not mapped_from or not mapped_to:
                continue
            for pj, prel in enumerate(predicted):
                pfrom, pto = pred_endpoints[pj]
                if not (mapped_from & pfrom) or not (mapped_to & pto):
                    continue
                score = _rel_similarity(grel, prel)
                stats["pairs_total"] += 1
                if _norm_type(grel.get("rel_type")) == _norm_type(prel.get("rel_type")):
                    auto.append((gi, pj, 1.0, "exact_rel_type"))
                else:
                    pending.append({
                        "pair_id": f"R-{gi}-{pj}",
                        "golden": _simple_relations([grel])[0],
                        "predicted": _simple_relations([prel])[0],
                        "rel_similarity": round(score, 4),
                        "_golden_index": gi,
                        "_predicted_index": pj,
                    })
        stats["auto"] = len(auto)
        pending.sort(key=lambda item: item["rel_similarity"], reverse=True)
        limited: list[dict[str, Any]] = []
        used_g: dict[int, int] = defaultdict(int)
        used_p: dict[int, int] = defaultdict(int)
        for item in pending:
            gi, pi = item["_golden_index"], item["_predicted_index"]
            if used_g[gi] >= _MAX_CANDIDATES_PER_ITEM or used_p[pi] >= _MAX_CANDIDATES_PER_ITEM:
                continue
            used_g[gi] += 1
            used_p[pi] += 1
            limited.append(item)
        verdicts, llm_stats = self._verify_pairs(limited, "relation")
        _merge_stats(stats, llm_stats)
        matched: list[tuple[int, int, float, str]] = list(auto)
        for item in limited:
            if verdicts.get(item["pair_id"]):
                matched.append((
                    item["_golden_index"],
                    item["_predicted_index"],
                    0.9 + float(item["rel_similarity"]) * 0.05,
                    "llm_accept",
                ))
        matched.sort(key=lambda row: (-row[2], -row[1], row[0]))
        used_g2: set[int] = set()
        used_p2: set[int] = set()
        final: list[tuple[int, int, float, str]] = []
        for gi, pi, score, reason in matched:
            if gi in used_g2 or pi in used_p2:
                continue
            used_g2.add(gi)
            used_p2.add(pi)
            final.append((gi, pi, score, reason))
        return final, stats

    # ----------------------------------------------------------------- case

    def score_case(
        self,
        golden_entities: list[dict[str, Any]],
        golden_relations: list[dict[str, Any]],
        predicted_entities: list[dict[str, Any]],
        predicted_relations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        entity_pairs, entity_stats = self._match_entities(golden_entities, predicted_entities)
        entity_mapping = {gi: pi for gi, pi, _, _ in entity_pairs}
        relation_pairs, relation_stats = self._match_relations(
            golden_relations,
            predicted_relations,
            entity_mapping,
            golden_entities,
            predicted_entities,
        )
        entity_metrics = _calculate_metrics(len(entity_pairs), len(golden_entities), len(predicted_entities))
        relation_metrics = _calculate_metrics(len(relation_pairs), len(golden_relations), len(predicted_relations))
        self.logger(
            "[fixed-judge] 实体 %d/%d->%d (auto=%d, llm=%d/%d, fallback=%d) | 关系 %d/%d->%d (auto=%d, llm=%d/%d)"
            % (
                len(entity_pairs), len(golden_entities), len(predicted_entities),
                entity_stats.get("auto", 0), entity_stats.get("llm_accept", 0), entity_stats.get("llm_pairs", 0),
                entity_stats.get("llm_fallback", 0),
                len(relation_pairs), len(golden_relations), len(predicted_relations),
                relation_stats.get("auto", 0), relation_stats.get("llm_accept", 0), relation_stats.get("llm_pairs", 0),
            )
        )
        return {
            "entities": entity_metrics,
            "relations": relation_metrics,
            "judge_pairs": {
                "entity_matches": [[gi, pi] for gi, pi, _, _ in sorted(entity_pairs)],
                "relation_matches": [[gi, pi] for gi, pi, _, _ in sorted(relation_pairs)],
            },
            "judge_mode": "fixed",
            "judge_stats": {
                "entities": entity_stats,
                "relations": relation_stats,
                "entity_reasons": {},
            },
        }