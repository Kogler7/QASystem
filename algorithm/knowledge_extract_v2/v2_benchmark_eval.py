#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""knowledge_extract_v2 benchmark test script (official + standard + simple).

该脚本用于在服务器上评测 knowledge_extract_v2 算法：

- 数据集：自动发现 benchmark/ 下所有数据集目录（默认含 official/standard/simple，
  新增如 tmp_problem 的数据集无需改代码，用 --datasets 指定或自动纳入）
  * 多 case 数据集：`benchmark/<dataset>/<case>/{docs,ontology,extract}`；
  * 单 case 数据集：`benchmark/<dataset>/{docs,ontology,extract}`（如 simple / tmp_problem）；
  * `extract/golden.json` 可选：缺失时仅抽取并保存预测，不参与指标统计。
- 输入：每个 case 的 ontology/*.puml|uml 与 docs/*.md
- 标准答案：每个 case 的 extract/golden.json
- 评测方式：默认使用大模型（LLM judge）判断 prediction 是否相对于 golden 抽对；
  也可以使用 --match-mode identity 做精确集合匹配。
  * LLM 判断：实体/关系允许同义表达、别名、规范名等语义等价。
  * identity：实体按 (type, name)，关系按 (from_type, from_name, rel_type, to_type, to_name) 精确匹配。
  * evidence 一律剔除、不参与评分（正常抽取结果不含 evidence）；
  * identity 模式按 type/name（关系按端点+rel_type）；LLM judge 还会比较 properties。
- 输出：实体 precision / recall / F1，关系 precision / recall / F1，
  并保存到带时间戳的目录中。

输入约定：
  - docs/ 下每个 Markdown 文件已经是上游切分好的一个 chunk；
  - 本脚本不再做任何 Markdown 切片/合并，每个 .md 文件直接包装成一个 ExtractionChunk。

默认抽取模型为 deepseek-v4-flash，可通过 --model 或环境变量覆盖。
脚本会自动读取同目录 .env 文件，常用键：
  KG_EXTRACT_LLM_MODEL
  KG_EXTRACT_LLM_BASE_URL
  KG_EXTRACT_LLM_API_KEY
  KG_EXTRACT_LLM_MAX_TOKENS
  KG_EXTRACT_LLM_MAX_CONCURRENCY
  KG_EXTRACT_LLM_TIMEOUT_SECONDS

示例：
    python v2_benchmark_eval.py
    python v2_benchmark_eval.py --datasets official standard
    python v2_benchmark_eval.py \
        --model deepseek-v4-flash \
        --base-url http://your-vllm-server:8000/v1 \
        --api-key not-needed
    python v2_benchmark_eval.py --output-dir ./my_results
"""

from __future__ import annotations

import argparse
import importlib
import logging
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# 使 knowledge_extract_v2 可导入。
# 兼容两种放置方式：
#   1. 独立目录: <任意>/knowledge_extract_v2/eval_...py
#   2. algorithm 包内: <根>/algorithm/knowledge_extract_v2/eval_...py
_SCRIPT_DIR = Path(__file__).resolve().parent
_PACKAGE_PARENT = _SCRIPT_DIR.parent


class _Tee:
    """把 stdout/stderr 同时写到控制台和输出目录的 run.log。"""

    def __init__(self, *streams: Any) -> None:
        self._streams = streams

    def write(self, data: str) -> int:
        for stream in self._streams:
            try:
                stream.write(data)
                stream.flush()
            except Exception:  # noqa: BLE001 - 日志失败不影响评测
                pass
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self) -> bool:
        return False


def _load_env_file(env_path: Path) -> None:
    """读取脚本同目录 .env，不覆盖进程已有环境变量。"""
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(_SCRIPT_DIR / ".env")

# 读取 .env 后再 import 算法包，确保 config.py 内部也能读到 LLM 配置。
try:
    # 如果运行环境中已经作为 algorithm 子包加载，优先使用同一个模块，
    # 避免出现 algorithm.knowledge_extract_v2 和 knowledge_extract_v2 两份实现。
    import algorithm.knowledge_extract_v2 as algorithm  # type: ignore[import-not-found]  # noqa: E402
    from algorithm.knowledge_extract_v2.chunk_context import ExtractionChunk  # noqa: E402
    from algorithm.knowledge_extract_v2.result_contract import to_public_result  # noqa: E402
except ImportError:
    # 若没有 algorithm 父包，则把 package 的父目录加入 sys.path 后按顶层包导入。
    if str(_PACKAGE_PARENT) not in sys.path:
        sys.path.insert(0, str(_PACKAGE_PARENT))
    import knowledge_extract_v2 as algorithm  # type: ignore[import-not-found]  # noqa: E402
    from knowledge_extract_v2.chunk_context import ExtractionChunk  # noqa: E402
    from knowledge_extract_v2.result_contract import to_public_result  # noqa: E402


_BENCHMARK_DIR = _SCRIPT_DIR / "benchmark"
_DEFAULT_MODEL = os.environ.get("KG_EXTRACT_LLM_MODEL") or "deepseek-v4-flash"
_DEFAULT_BASE_URL = os.environ.get("KG_EXTRACT_LLM_BASE_URL")
_DEFAULT_API_KEY = os.environ.get("KG_EXTRACT_LLM_API_KEY")
_DEFAULT_MAX_TOKENS = int(os.environ.get("KG_EXTRACT_LLM_MAX_TOKENS") or 32768)

_DOC_SUFFIXES = {".md", ".markdown"}
_ONTOLOGY_SUFFIXES = {".puml", ".uml", ".plantuml"}


def _safe_text(value: Any) -> str:
    """字符串化并去除首尾空白；None 归一为空字符串。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _entity_key(entity: Any) -> tuple[str, str] | None:
    """identity 级实体键：type + name。"""
    if not isinstance(entity, dict):
        return None
    etype = _safe_text(entity.get("type"))
    ename = _safe_text(entity.get("name"))
    if not etype or not ename:
        return None
    return (etype, ename)


def _relation_key(relation: Any) -> tuple[str, str, str, str, str] | None:
    """identity 级关系键：from_type + from_name + rel_type + to_type + to_name。"""
    if not isinstance(relation, dict):
        return None
    fields = (
        relation.get("from_type"),
        relation.get("from_name"),
        relation.get("rel_type"),
        relation.get("to_type"),
        relation.get("to_name"),
    )
    keys = tuple(_safe_text(field) for field in fields)
    if any(not key for key in keys):
        return None
    return keys  # type: ignore[return-value]


def _strip_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop ``evidence`` before scoring（properties 保留）。

    正常抽取结果不含 evidence，评测也从不比较 evidence；这里显式剥离，确保
    任何 scoring / judge 路径都看不到它。
    """
    return [
        {key: value for key, value in item.items() if key != "evidence"}
        for item in items
        if isinstance(item, dict)
    ]


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"JSON 顶层必须是对象: {path}")
    return data


def _read_text(path: Path) -> str:
    """读取 Markdown/文本，兼容 UTF-8 BOM 与 GB18030。"""
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"无法识别文档编码: {path}")


def _case_entry(group: str, root: Path) -> dict[str, Any] | None:
    """Build a case record when *root* has docs/ and ontology/.

    ``extract/golden.json`` is optional so an in-progress dataset can still be
    extracted; the case is then reported as "unscored" instead of scored.
    """
    docs_dir = root / "docs"
    ontology_dir = root / "ontology"
    if not (docs_dir.is_dir() and ontology_dir.is_dir()):
        return None
    golden_file = root / "extract" / "golden.json"
    return {
        "group": group,
        "case": root.name,
        "path": root,
        "docs_dir": docs_dir,
        "ontology_dir": ontology_dir,
        "golden_file": golden_file if golden_file.is_file() else None,
    }



def discover_cases(
    benchmark_dir: Path,
    datasets: list[str],
) -> list[dict[str, Any]]:
    """枚举 benchmark 下各数据集的完整 case（extract/golden.json 可选）。"""
    cases: list[dict[str, Any]] = []
    for group in datasets:
        group_dir = benchmark_dir / group
        if not group_dir.is_dir():
            print(f"[warning] 不存在数据集目录: {group_dir}")
            continue
        direct = _case_entry(group, group_dir)
        if direct is not None:
            # simple 等数据集本身就是单个 case（docs/ontology/extract 直接位于组目录下）。
            cases.append(direct)
            continue
        for child in sorted(group_dir.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith(".") or child.name.startswith("_"):
                continue
            entry = _case_entry(group, child)
            if entry is None:
                print(f"[warning] 目录结构不完整，跳过: {child}")
                continue
            cases.append(entry)
    return cases


def discover_datasets(benchmark_dir: Path) -> list[str]:
    """Return every dataset group under *benchmark_dir* that contains a case.

    A group is either a single case (``<group>/docs`` + ``<group>/ontology``) or
    a directory of cases (``<group>/<case>/...``).  New datasets therefore need
    no code change: dropping a directory under ``benchmark/`` is enough.
    """
    datasets: list[str] = []
    if not benchmark_dir.is_dir():
        return datasets
    for child in sorted(benchmark_dir.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        if _case_entry(child.name, child) is not None:
            datasets.append(child.name)
            continue
        if any(
            grand.is_dir() and _case_entry(child.name, grand) is not None
            for grand in child.iterdir()
        ):
            datasets.append(child.name)
    return datasets


def build_ontology_text(ontology_dir: Path) -> str:
    """拼接一个 case 下的全部 PlantUML 本体文件。"""
    ontology_files = sorted(
        p for p in ontology_dir.iterdir()
        if p.is_file() and p.suffix.lower() in _ONTOLOGY_SUFFIXES
    )
    if not ontology_files:
        ontology_files = sorted(
            p for p in ontology_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in _ONTOLOGY_SUFFIXES
        )
    if not ontology_files:
        raise FileNotFoundError(f"未找到本体文件: {ontology_dir}")
    return "\n\n".join(_read_text(path) for path in ontology_files).strip()


def collect_doc_files(docs_dir: Path) -> list[Path]:
    """返回该 case 下所有 Markdown 文档（按文件名排序）。"""
    doc_files = sorted(
        p for p in docs_dir.iterdir()
        if p.is_file() and p.suffix.lower() in _DOC_SUFFIXES
    )
    if not doc_files:
        doc_files = sorted(
            p for p in docs_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in _DOC_SUFFIXES
        )
    return doc_files


def build_chunks(
    doc_files: list[Path],
    *,
    case_name: str,
) -> list[ExtractionChunk]:
    """将 docs/ 下每个已切分好的 Markdown 文件包装成一个 ExtractionChunk。

    输入约定：上游已经按 chunk 粒度保存为 .md 文件，因此这里不再做任何切片逻辑。
    """
    chunks: list[ExtractionChunk] = []
    for doc_index, doc_path in enumerate(doc_files, start=1):
        text = _read_text(doc_path)
        if not text.strip():
            continue
        chunk = ExtractionChunk(
            chunk_id=f"{case_name}-d{doc_index:02d}",
            text=text,
            document_id=doc_path.name,
            metadata={"source_file": doc_path.name, "source_kind": "markdown"},
        )
        chunks.append(chunk)
    return chunks


def _result_entities_relations(result: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """从 extract_harness 的完整结果提取公开契约。"""
    # to_public_result 会校验 status/error_code，并返回稳定的 entities/relations。
    public = to_public_result(result)
    entities = public.get("entities") or []
    relations = public.get("relations") or []
    return entities, relations


def _result_entities_relations_partial(result: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """best_effort 模式下接受 partial 结果，直接从 raw 取已接受的事实。"""
    raw = result.get("raw") or {}
    entities: list[dict[str, Any]] = []
    for entity in raw.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        entities.append({
            "type": entity.get("type"),
            "name": entity.get("name"),
            "properties": {
                key: value
                for key, value in dict(entity.get("properties") or {}).items()
                if key != "name"
            },
        })
    relations: list[dict[str, Any]] = []
    for relation in raw.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        relations.append({
            "from_type": relation.get("from_type"),
            "from_name": relation.get("from_name"),
            "rel_type": relation.get("rel_type"),
            "to_type": relation.get("to_type"),
            "to_name": relation.get("to_name"),
        })
    return entities, relations


def call_extract(
    case: dict[str, Any],
    *,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
    max_tokens: int | None,
    max_workers: int | None,
    best_effort: bool = False,
    max_attempts: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """调用 knowledge_extract_v2 抽取一个 case，返回 (entities, relations)。"""
    ontology_text = build_ontology_text(case["ontology_dir"])
    doc_files = collect_doc_files(case["docs_dir"])
    if not doc_files:
        raise FileNotFoundError(f"未找到 Markdown 文档: {case['docs_dir']}")
    chunks = build_chunks(doc_files, case_name=case["case"])

    print(
        f"[extract] {case['group']}/{case['case']}: "
        f"ontology_len={len(ontology_text)}, docs={len(doc_files)}, chunks={len(chunks)}"
    )

    extract_kwargs: dict[str, Any] = {
        "ontology": ontology_text,
        "chunks": chunks,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "max_tokens": max_tokens,
        "max_workers": max_workers,
    }
    if best_effort:
        extract_kwargs["best_effort"] = True
    if max_attempts is not None:
        extract_kwargs["max_attempts"] = max_attempts

    result = algorithm.extract_harness(**extract_kwargs)

    status = str(result.get("status") or "")
    accept_partial = bool(best_effort and status == "partial")
    if (status != "succeeded" or result.get("error_code")) and not accept_partial:
        error = (
            result.get("_error")
            or result.get("error")
            or f"status={result.get('status')}, error_code={result.get('error_code')}"
        )
        attempts = result.get("attempts") or []
        terminal_attempts = [
            item for item in attempts
            if isinstance(item, dict) and item.get("terminal")
        ]
        detail = json.dumps(
            terminal_attempts[:5],
            ensure_ascii=False,
            default=str,
            indent=2,
        ) if terminal_attempts else ""
        raise RuntimeError(f"{error}\nterminal_attempts:\n{detail}")

    if accept_partial:
        entities, relations = _result_entities_relations_partial(result)
        print(
            f"[extract][partial] {case['group']}/{case['case']}: "
            f"repair 未完成，保留部分结果，实体={len(entities)}，关系={len(relations)}"
        )
        return entities, relations

    entities, relations = _result_entities_relations(result)
    if not entities and not relations:
        # 避免把全部失败但 status=succeeded 的异常结果静默当成空结果
        if result.get("attempts"):
            failures = [
                item for item in result["attempts"]
                if isinstance(item, dict) and item.get("status") in ("failed", "error")
            ]
            if failures:
                raise RuntimeError(
                    f"抽取内部失败: {json.dumps(failures[:3], ensure_ascii=False, default=str)}"
                )
    if not entities:
        warning_codes = sorted(
            {
                str(item.get("code"))
                for item in (result.get("warnings") or [])
                if isinstance(item, dict) and item.get("code")
            }
        )
        selection = dict((result.get("profile") or {}).get("candidate_selection") or {})
        print(
            f"[extract][diagnose] {case['group']}/{case['case']} 实体为空；"
            f"candidate_selection={selection}; warnings={', '.join(warning_codes[:15]) or '(none)'}"
        )
    return entities, relations


def build_sets(
    entities: list[dict[str, Any]],
    relations: list[dict[str, Any]],
) -> tuple[set[Any], set[Any]]:
    entity_set: set[Any] = set()
    for entity in _strip_evidence(entities):
        key = _entity_key(entity)
        if key is not None:
            entity_set.add(key)

    relation_set: set[Any] = set()
    for relation in _strip_evidence(relations):
        key = _relation_key(relation)
        if key is not None:
            relation_set.add(key)
    return entity_set, relation_set


def calculate_metrics(tp: int, fp: int, fn: int) -> dict[str, Any]:
    """由累加后的 TP/FP/FN 计算 P/R/F1。"""
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )
    return {
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "tp": int(tp),
        "fp": int(fp),
        "fn": int(fn),
    }


def score_case(
    golden_entities: list[dict[str, Any]],
    golden_relations: list[dict[str, Any]],
    predicted_entities: list[dict[str, Any]],
    predicted_relations: list[dict[str, Any]],
) -> dict[str, Any]:
    """计算单个 case 的 identity 指标。"""
    gold_ent, gold_rel = build_sets(golden_entities, golden_relations)
    pred_ent, pred_rel = build_sets(predicted_entities, predicted_relations)

    ent_tp = len(gold_ent & pred_ent)
    ent_fp = len(pred_ent - gold_ent)
    ent_fn = len(gold_ent - pred_ent)

    rel_tp = len(gold_rel & pred_rel)
    rel_fp = len(pred_rel - gold_rel)
    rel_fn = len(gold_rel - pred_rel)

    return {
        "entities": {
            "golden": len(gold_ent),
            "predicted": len(pred_ent),
            **calculate_metrics(ent_tp, ent_fp, ent_fn),
        },
        "relations": {
            "golden": len(gold_rel),
            "predicted": len(pred_rel),
            **calculate_metrics(rel_tp, rel_fp, rel_fn),
        },
    }

def _build_judge_client(
    *,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
    max_tokens: int | None,
) -> Any:
    """创建用于 LLM 评测的 OpenAI-compatible client。"""
    module = importlib.import_module(f"{algorithm.__name__}.llm_client")
    return module.create_llm_client(
        base_url=base_url or "",
        api_key=api_key or "",
        model=model or "",
        max_tokens=max_tokens,
    )


def _decode_judge_response(response: Any) -> dict[str, Any]:
    """把 LLM 返回解析成 dict；兼容 markdown code block。"""
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
        raise ValueError("LLM 评测返回必须是 JSON object")
    return data


def _judge_schema(collection_name: str) -> dict[str, Any]:
    """LLM 评测输出 schema：只包含单个 collection 的匹配 pair。"""
    pair_schema = {
        "type": "object",
        "properties": {
            "golden_index": {"type": "integer"},
            "predicted_index": {"type": "integer"},
        },
        "required": ["golden_index", "predicted_index"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            collection_name: {
                "type": "array",
                "items": pair_schema,
            },
        },
        "required": [collection_name],
        "additionalProperties": False,
    }


def _simple_entities(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """LLM judge 可见的实体字段：type / name / properties（不含 evidence）。"""
    simplified = []
    for entity in entities:
        simplified.append({
            "type": entity.get("type"),
            "name": entity.get("name"),
            "properties": dict(entity.get("properties") or {}),
        })
    return simplified


def _simple_relations(relations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """LLM judge 可见的关系字段：端点 / rel_type（若带 properties 也一并给出）。"""
    simplified = []
    for relation in relations:
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
        simplified.append(item)
    return simplified


def _build_judge_messages(
    collection_name: str,
    golden_items: list[dict[str, Any]],
    predicted_items: list[dict[str, Any]],
) -> list[dict[str, str]]:
    if collection_name == "entity_matches":
        system = """你是知识抽取评测员。请判断 predicted 实体是否相对 golden 算“抽对了”。

判断规则：
1. 预测实体的 type 与 golden type 指向同一个本体类型即可，不要求字符串完全一致。
2. name 指向同一实体即可，允许同义表达、别名、规范名、标点/空格/全半角差异。
3. 同时比较 properties：两侧都出现的属性，键与值应一致（值允许同义/规范化差异，如日期格式、全半角、数值 100 与 100.0）；golden 没有的属性不会因为多出而判错，但关键属性缺失或冲突会降低匹配置信度。
4. 不比较 evidence。
5. 匹配必须是一对一：每个 golden 最多匹配一个 predicted，每个 predicted 最多匹配一个 golden。
6. 只要输出匹配 pair；没有匹配到的一律不要列出。
7. 输出 JSON，不要输出解释。"""
    else:
        system = """你是知识抽取评测员。请判断 predicted 关系是否相对 golden 算“抽对了”。

判断规则：
1. 预测关系的 rel_type 与 golden rel_type 等价即可，允许同义关系表达。
2. from/to 两个端点必须分别对应到 golden 中同一实体，方向不能反；端点用 type/name 判断（允许别名/同义）。
3. 若关系带有 properties，两侧都出现的属性键与值应一致（允许同义/规范化差异）。
4. 不比较 evidence。
5. 匹配必须是一对一：每个 golden 最多匹配一个 predicted，每个 predicted 最多匹配一个 golden。
6. 只要输出匹配 pair；没有匹配到的一律不要列出。
7. 输出 JSON，不要输出解释。"""

    golden_items = (
        _simple_entities(golden_items)
        if collection_name == "entity_matches"
        else _simple_relations(golden_items)
    )
    predicted_items = (
        _simple_entities(predicted_items)
        if collection_name == "entity_matches"
        else _simple_relations(predicted_items)
    )

    user = f"""请判断下面的 predicted 是否在 golden 中存在“抽对了”的对应项。

## golden items
{json.dumps(golden_items, ensure_ascii=False, indent=2)}

## predicted items
{json.dumps(predicted_items, ensure_ascii=False, indent=2)}

请只输出：
{{
  "{collection_name}": [
    {{"golden_index": 0, "predicted_index": 1}}
  ]
}}

golden_index 和 predicted_index 都从 0 开始；没有匹配的不要列出来。"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _unique_matches(raw_matches: Any) -> list[tuple[int, int]]:
    """按 one-to-one 约束清洗 LLM 返回的匹配 pair。

    兼容两种输入：
    - LLM 返回的 dict 列表：{"golden_index": 0, "predicted_index": 0}
    - 内部 tuple 列表：(0, 0)
    """
    pairs: list[tuple[int, int]] = []
    seen_golden: set[int] = set()
    seen_predicted: set[int] = set()
    if not isinstance(raw_matches, list):
        return pairs
    for item in raw_matches:
        if isinstance(item, dict):
            try:
                golden_idx = int(item.get("golden_index"))
                predicted_idx = int(item.get("predicted_index"))
            except (TypeError, ValueError):
                continue
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            try:
                golden_idx = int(item[0])
                predicted_idx = int(item[1])
            except (TypeError, ValueError):
                continue
        else:
            continue
        if golden_idx < 0 or predicted_idx < 0:
            continue
        if golden_idx in seen_golden or predicted_idx in seen_predicted:
            continue
        seen_golden.add(golden_idx)
        seen_predicted.add(predicted_idx)
        pairs.append((golden_idx, predicted_idx))
    return pairs


def _judge_one_batch(
    judge_client: Any,
    collection_name: str,
    golden_items: list[dict[str, Any]],
    batch_predicted_items: list[dict[str, Any]],
    predicted_start: int,
) -> list[tuple[int, int]]:
    """让 LLM judge 一小批 predicted items，返回相对原始 predicted 的下标。"""
    messages = _build_judge_messages(
        collection_name,
        golden_items,
        batch_predicted_items,
    )
    schema = _judge_schema(collection_name)
    errors: list[str] = []

    # 优先 json_schema 保证结构合法；若服务不支持再退到 json_object/plain。
    for response_mode in ("json_schema", "json_object", "plain"):
        try:
            response = judge_client.generate_structured(
                schema=schema,
                messages=messages,
                run_context={
                    "schema_name": "knowledge_extract_v2_judge",
                    "response_mode": response_mode,
                    "prefer_chat_json_schema": True,
                },
            )
            data = _decode_judge_response(response)
            raw_matches = data.get(collection_name)
            if raw_matches is None:
                raise ValueError(f"响应缺少字段: {collection_name}")
            batch_pairs = _unique_matches(raw_matches)
            return [
                (g, predicted_start + p)
                for g, p in batch_pairs
                if g < len(golden_items) and p < len(batch_predicted_items)
            ]
        except Exception as exc:
            errors.append(f"{response_mode}: {type(exc).__name__}: {exc}")
    raise RuntimeError(
        f"LLM judge {collection_name} 第 {predicted_start // max(1, len(batch_predicted_items)) + 1} 批失败，"
        f"尝试 json_schema/json_object/plain 均未成功: " + " | ".join(errors)
    )


def _judge_collection(
    judge_client: Any,
    collection_name: str,
    golden_items: list[dict[str, Any]],
    predicted_items: list[dict[str, Any]],
    *,
    batch_size: int = 20,
) -> list[tuple[int, int]]:
    """分批让 LLM judge，避免单次输出大量匹配 pair 导致 JSON 截断/解析失败。"""
    all_pairs: list[tuple[int, int]] = []
    if not golden_items or not predicted_items:
        return all_pairs

    for start in range(0, len(predicted_items), batch_size):
        batch = predicted_items[start:start + batch_size]
        batch_pairs = _judge_one_batch(
            judge_client,
            collection_name,
            golden_items,
            batch,
            start,
        )
        all_pairs.extend(batch_pairs)

    # 全局 one-to-one 去重，防止不同批返回同一 golden/predicted 导致重复计数。
    return _unique_matches(all_pairs)


def llm_judge_score_case(
    golden_entities: list[dict[str, Any]],
    golden_relations: list[dict[str, Any]],
    predicted_entities: list[dict[str, Any]],
    predicted_relations: list[dict[str, Any]],
    judge_client: Any,
) -> dict[str, Any]:
    """使用大模型判断 semantic match，并计算实体的 P/R/F1 与关系的 P/R/F1。

    比较实体 type/name/properties、关系端点/rel_type（如有 properties 也参与）；
    evidence 一律剔除。
    """
    golden_entities = _strip_evidence(golden_entities)
    golden_relations = _strip_evidence(golden_relations)
    predicted_entities = _strip_evidence(predicted_entities)
    predicted_relations = _strip_evidence(predicted_relations)
    entity_pairs = [
        (g, p) for g, p in _judge_collection(
            judge_client,
            "entity_matches",
            golden_entities,
            predicted_entities,
        )
        if g < len(golden_entities) and p < len(predicted_entities)
    ]
    relation_pairs = [
        (g, p) for g, p in _judge_collection(
            judge_client,
            "relation_matches",
            golden_relations,
            predicted_relations,
        )
        if g < len(golden_relations) and p < len(predicted_relations)
    ]

    entity_metrics = {
        "golden": len(golden_entities),
        "predicted": len(predicted_entities),
        **calculate_metrics(len(entity_pairs), max(0, len(predicted_entities) - len(entity_pairs)), max(0, len(golden_entities) - len(entity_pairs))),
    }
    relation_metrics = {
        "golden": len(golden_relations),
        "predicted": len(predicted_relations),
        **calculate_metrics(len(relation_pairs), max(0, len(predicted_relations) - len(relation_pairs)), max(0, len(golden_relations) - len(relation_pairs))),
    }

    return {
        "entities": entity_metrics,
        "relations": relation_metrics,
        "judge_pairs": {
            "entity_matches": entity_pairs,
            "relation_matches": relation_pairs,
        },
    }



def aggregate_metrics(metric_list: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """micro 汇总：TP/FP/FN 与 golden/predicted 数量各自累加。"""
    counters = {
        "entities": {"tp": 0, "fp": 0, "fn": 0, "golden": 0, "predicted": 0},
        "relations": {"tp": 0, "fp": 0, "fn": 0, "golden": 0, "predicted": 0},
    }
    for metrics in metric_list:
        for name in ("entities", "relations"):
            item = metrics[name]
            counters[name]["tp"] += item["tp"]
            counters[name]["fp"] += item["fp"]
            counters[name]["fn"] += item["fn"]
            counters[name]["golden"] += item["golden"]
            counters[name]["predicted"] += item["predicted"]
    summary: dict[str, Any] = {}
    for name in ("entities", "relations"):
        counter = counters[name]
        summary[name] = {
            "golden": counter["golden"],
            "predicted": counter["predicted"],
            **calculate_metrics(counter["tp"], counter["fp"], counter["fn"]),
        }
    return summary


def save_predicted(
    output_dir: Path,
    case: dict[str, Any],
    entities: list[dict[str, Any]],
    relations: list[dict[str, Any]],
) -> Path:
    out_path = output_dir / "predictions" / case["group"] / f"{case['case']}.predicted.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"entities": entities, "relations": relations}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


def load_predicted(output_dir: Path, case: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    candidates = [
        output_dir / "predictions" / case["group"] / f"{case['case']}.predicted.json",
        output_dir / case["group"] / f"{case['case']}.predicted.json",
    ]
    for path in candidates:
        if path.is_file():
            data = _load_json(path)
            return (data.get("entities") or []), (data.get("relations") or [])
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="knowledge_extract_v2 benchmark 评测（数据集自动发现；identity 或 LLM judge）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--benchmark-dir",
        type=Path,
        default=_BENCHMARK_DIR,
        help=f"benchmark 根目录（默认: {_BENCHMARK_DIR}）",
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        default=None,
        help="要评测的数据集名（benchmark/<name>）；不传则自动发现 benchmark 下全部数据集（含 tmp_problem 等新数据集）",
    )
    parser.add_argument(
        "--match-mode",
        choices=["identity", "llm"],
        default="llm",
        help="评测匹配方式：identity=精确集合匹配，llm=大模型判断是否抽对（默认 llm）",
    )
    parser.add_argument(
        "--judge-mode",
        choices=["fixed"],
        default="fixed",
        help="LLM judge 实现：仅保留 fixed（本地短名单 + 名称级小批校验）",
    )
    parser.add_argument(
        "--model",
        default=_DEFAULT_MODEL,
        help="LLM 模型名；默认使用 KG_EXTRACT_LLM_MODEL 或 deepseek-v4-flash",
    )
    parser.add_argument(
        "--base-url",
        default=_DEFAULT_BASE_URL,
        help="LLM API base_url；默认使用 KG_EXTRACT_LLM_BASE_URL",
    )
    parser.add_argument(
        "--api-key",
        default=_DEFAULT_API_KEY,
        help="LLM API key；默认使用 KG_EXTRACT_LLM_API_KEY",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=_DEFAULT_MAX_TOKENS,
        help=f"LLM 输出最大 token 数；默认取 KG_EXTRACT_LLM_MAX_TOKENS（当前 {_DEFAULT_MAX_TOKENS}）",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=None,
        help="单个 LLM 调用的最大尝试次数；不传则使用算法默认",
    )
    parser.add_argument(
        "--best-effort",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="允许部分 chunk 抽取失败时返回已有结果（默认开启；可用 --no-best-effort 关闭）",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="并发调用 LLM 的 worker 数；不传则使用算法配置",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="保存目录；默认在 knowledge_extract_v2 下创建带时间戳目录",
    )
    parser.add_argument(
        "--skip-extract",
        action="store_true",
        help="如果已有预测 JSON，则只读取预测并评测；不调用抽取模型，但 LLM judge 模式仍会调用评测模型（需配合 --output-dir）",
    )
    args = parser.parse_args()
    if args.max_attempts is not None and args.max_attempts <= 0:
        parser.error("--max-attempts 必须大于 0")
    if args.skip_extract and args.output_dir is None:
        parser.error("--skip-extract 需要配合 --output-dir 使用")
    return args


def print_summary(summary: dict[str, Any]) -> None:
    def render(title: str, stats: dict[str, Any]) -> None:
        print(
            f"  {title:<8}"
            f" golden={stats['golden']:<5} predicted={stats['predicted']:<5} "
            f"precision={stats['precision']:.4f} recall={stats['recall']:.4f} f1={stats['f1']:.4f}"
        )

    mode_label = summary.get("match_mode", "identity")
    print(f"\n========== 总体指标（micro / {mode_label}） ==========")
    render("实体", summary["overall"]["entities"])
    render("关系", summary["overall"]["relations"])

    for group, group_metrics in summary.get("datasets", {}).items():
        print(f"\n---------- {group} ----------")
        render("实体", group_metrics["entities"])
        render("关系", group_metrics["relations"])

    print("\n---------- 单条 case ----------")
    for item in summary["per_case"]:
        ent = item["entities"]
        rel = item["relations"]
        print(
            f"  {item['group']}/{item['case']:<18}"
            f" 实体 P/R/F1 = {ent['precision']:.3f}/{ent['recall']:.3f}/{ent['f1']:.3f}"
            f" ({ent['tp']}/{ent['golden']}/{ent['predicted']})"
            f"  关系 P/R/F1 = {rel['precision']:.3f}/{rel['recall']:.3f}/{rel['f1']:.3f}"
            f" ({rel['tp']}/{rel['golden']}/{rel['predicted']})"
        )
    if summary.get("errors"):
        print("\n---------- 失败/跳过 ----------")
        for error in summary["errors"]:
            print(f"  - {error['case']}: {error['error']}")
    if summary.get("unscored"):
        print("\n---------- 未评测（缺少 golden） ----------")
        for item in summary["unscored"]:
            print(f"  - {item['group']}/{item['case']}")
    print()


def format_case_metrics(item: dict[str, Any]) -> str:
    """把单个已抽取 case 的评测指标渲染成一行。"""
    ent = item["entities"]
    rel = item["relations"]
    return (
        f"{item['group']}/{item['case']}: "
        f"实体 P/R/F1={ent['precision']:.3f}/{ent['recall']:.3f}/{ent['f1']:.3f} "
        f"({ent['tp']}/{ent['golden']}/{ent['predicted']}) | "
        f"关系 P/R/F1={rel['precision']:.3f}/{rel['recall']:.3f}/{rel['f1']:.3f} "
        f"({rel['tp']}/{rel['golden']}/{rel['predicted']})"
    )


def save_case_metrics(output_dir: Path, item: dict[str, Any]) -> Path:
    """每个抽取任务评测完成后，立即落盘该 case 的指标。"""
    out_path = output_dir / "metrics" / item["group"] / f"{item['case']}.metrics.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def format_running_overall(metric_list: list[dict[str, Any]], *, completed: int) -> str:
    """已完成评测 case 的 micro 累计指标（增量总览）。"""
    running = aggregate_metrics(metric_list)
    ent = running["entities"]
    rel = running["relations"]
    return (
        f"累计 {completed} 个 case："
        f"实体 P/R/F1={ent['precision']:.3f}/{ent['recall']:.3f}/{ent['f1']:.3f} | "
        f"关系 P/R/F1={rel['precision']:.3f}/{rel['recall']:.3f}/{rel['f1']:.3f}"
    )


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = parse_args()

    # 未显式指定数据集时，自动发现 benchmark 下所有可用的数据集目录，新数据集无需改代码。
    datasets = list(args.datasets) if args.datasets else discover_datasets(args.benchmark_dir)
    if not datasets:
        print(f"[error] 在 {args.benchmark_dir} 下没有发现任何数据集。")
        return 1
    args.datasets = datasets
    print(f"[info] 数据集: {', '.join(datasets)}")

    if args.output_dir is not None:
        output_dir = args.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = _SCRIPT_DIR / f"eval_results_{'_'.join(args.datasets)}_{timestamp}"
        output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "run.log"
    log_handle = log_path.open("a", encoding="utf-8")
    sys.stdout = _Tee(sys.stdout, log_handle)
    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(file_handler)
    # ensure_logging 会给每个模块 logger 挂 stderr handler 且 propagate=False，
    # 因此 run.log 的 FileHandler 也要挂到这些 logger 上才能收到步骤日志。
    for _logger in list(logging.root.manager.loggerDict.values()):
        if isinstance(_logger, logging.Logger) and getattr(_logger, "_qasystem_stage_handler", None) is not None:
            _logger.addHandler(file_handler)
    print(f"[info] 保存目录: {output_dir}")

    cases = discover_cases(args.benchmark_dir, args.datasets)
    if not cases:
        print("没有发现可评测的 benchmark case。")
        return 1
    print(
        "共发现 {} 个 benchmark case：{}".format(
            len(cases), ", ".join(f"{c['group']}/{c['case']}" for c in cases)
        )
    )

    judge_client: Any = None
    fixed_judge: Any = None
    if args.match_mode == "llm":
        try:
            judge_client = _build_judge_client(
                model=args.model,
                base_url=args.base_url,
                api_key=args.api_key,
                max_tokens=args.max_tokens,
            )
            print(f"[info] LLM 评测模式：使用 {args.model} 作为 judge")
            if args.judge_mode == "fixed":
                module = importlib.import_module(f"{algorithm.__name__}.v2_fixed_judge")
                fixed_judge = module.FixedJudge(judge_client)
                print("[info] judge 模式: fixed（短名单 + 名称级小批校验，输出 pair_id）")
            else:
                print("[info] judge 模式: legacy（旧版全量 index 配对，不推荐）")
        except Exception as exc:
            print(f"[error] 创建 LLM judge client 失败: {exc}")
            return 1

    per_case: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    unscored: list[dict[str, Any]] = []

    for idx, case in enumerate(cases, start=1):
        print(f"\n[{idx}/{len(cases)}] 开始 {case['group']}/{case['case']}")
        golden_file = case.get("golden_file")
        if golden_file is not None:
            golden = _load_json(golden_file)
            golden_entities = golden.get("entities") or []
            golden_relations = golden.get("relations") or []
        else:
            golden_entities = None
            golden_relations = None

        if args.skip_extract:
            loaded = load_predicted(output_dir, case)
            if loaded is None:
                message = f"找不到预测文件，跳过: {output_dir / 'predictions' / case['group'] / (case['case'] + '.predicted.json')}"
                print(f"[skip-extract] {message}")
                errors.append({"case": case["case"], "error": message})
                continue
            predicted_entities, predicted_relations = loaded
            print(f"[load] 读取已有预测: {case['case']}")
        else:
            try:
                t0 = time.time()
                predicted_entities, predicted_relations = call_extract(
                    case,
                    model=args.model,
                    base_url=args.base_url,
                    api_key=args.api_key,
                    max_tokens=args.max_tokens,
                    max_workers=args.max_workers,
                    best_effort=args.best_effort,
                    max_attempts=args.max_attempts,
                )
                print(
                    f"[extract] {case['group']}/{case['case']} 完成，"
                    f"耗时 {time.time() - t0:.1f}s，实体={len(predicted_entities)}，关系={len(predicted_relations)}"
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                details = getattr(exc, "details", None)
                if details:
                    # 例如 ontology strict validation failed 的 schema QA 明细。
                    message += "\n" + json.dumps(
                        details, ensure_ascii=False, default=str, indent=2
                    )
                print(f"[extract] 失败: {message}")
                errors.append({"case": case["case"], "error": message})
                continue
            try:
                saved = save_predicted(output_dir, case, predicted_entities, predicted_relations)
                print(f"[save] 预测结果已保存: {saved}")
            except Exception as exc:
                print(f"[warning] 保存预测结果失败: {exc}")

        if golden_entities is None:
            print(
                f"[eval] {case['group']}/{case['case']} 缺少 extract/golden.json，"
                "已抽取并保存预测，但不参与指标统计。"
            )
            unscored.append({"group": case["group"], "case": case["case"]})
            continue

        try:
            if args.match_mode == "llm":
                if fixed_judge is not None:
                    metrics = fixed_judge.score_case(
                        golden_entities,
                        golden_relations,
                        predicted_entities,
                        predicted_relations,
                    )
                else:
                    metrics = llm_judge_score_case(
                        golden_entities,
                        golden_relations,
                        predicted_entities,
                        predicted_relations,
                        judge_client,
                    )
            else:
                metrics = score_case(
                    golden_entities,
                    golden_relations,
                    predicted_entities,
                    predicted_relations,
                )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            print(f"[judge] {case['group']}/{case['case']} 评测失败: {message}")
            errors.append({"case": case["case"], "error": f"judge: {message}"})
            continue

        item: dict[str, Any] = {
            "group": case["group"],
            "case": case["case"],
            "path": str(case["path"]),
            "entities": metrics["entities"],
            "relations": metrics["relations"],
        }
        if "judge_pairs" in metrics:
            item["judge_pairs"] = metrics["judge_pairs"]
        per_case.append(item)

        # 每完成一个抽取任务就立即评测并输出该 case 的结果。
        print(f"[eval] {format_case_metrics(item)}")
        try:
            metrics_path = save_case_metrics(output_dir, item)
            print(f"[eval] 单条指标已保存: {metrics_path}")
        except Exception as exc:  # noqa: BLE001 - 指标落盘失败不应中断评测
            print(f"[warning] 保存单条指标失败: {exc}")
        print(f"[eval] {format_running_overall(per_case, completed=len(per_case))}")

    if not per_case and not unscored:
        print("没有获取到任何可评测的预测结果。")
        return 1

    summary: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "match_mode": args.match_mode,
        "level": "llm_semantic" if args.match_mode == "llm" else "identity",
        "datasets": list(args.datasets),
        "model": args.model,
        "base_url": args.base_url,
        "max_tokens": args.max_tokens,
        "max_attempts": args.max_attempts,
        "max_workers": args.max_workers,
        "best_effort": args.best_effort,
        "output_dir": str(output_dir),
        "overall": aggregate_metrics(per_case),
        "datasets": {},
        "per_case": per_case,
        "errors": errors,
        "unscored": unscored,
    }
    for group in args.datasets:
        group_cases = [item for item in per_case if item["group"] == group]
        if group_cases:
            summary["datasets"][group] = aggregate_metrics(group_cases)

    summary_path = output_dir / f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n评测结果已写入: {summary_path}")
    print_summary(summary)
    print(f"\n所有结果已保存到: {output_dir}")
    log_handle.flush()
    file_handler.flush()
    file_handler.close()
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
