from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from algorithm.knowledge_pipeline import generate_understanding, store_vectors, upload_file
from algorithm.logging_setup import ensure_logging
from algorithm.preproc.preproc_src.main_processor import DocumentProcessor
from external_services.application import get_application_service_url

from backend.app import observability
from backend.app.responses import ErrorCode
from backend.app.schemas import PipelineOptions, TextChunk, TextEmbeddingItem
from backend.app.services.algorithm_capabilities import (
    CapabilityError,
    DocumentChunkingService,
    DocumentParsingService,
    EmbeddingService,
    GraphExtractionService,
)

#: 本模块日志一定可见：不依赖 root 是否已有 handler（worker/子进程里 root 通常
#: 既无 handler、级别也是 WARNING，模块级 INFO 会被 lastResort 直接丢弃）。
logger = ensure_logging(logging.getLogger(__name__))


#: 抽取结果快照目录：每次运行落盘一份 JSON、失败时同目录写 *_error.log，供后续比对。
#: 本地是 backend/data/，容器内挂载在可写的 backend-data 卷（/app/backend/data）。
EXTRACTION_SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / "data" / "extraction_snapshots"

#: 最近一次运行的阶段产物目录：流水线失败时把报错现场也写进同一个目录（见 _save_error_log）。
_LAST_RUN_DIR: Path | None = None

#: 日志里最多展开的本体行数；完整原文始终原样写进运行目录的 00-ontology.puml。
ONTOLOGY_LOG_LINES = 200


def _log_prefix(document: str | None = None) -> str:
    """服务层日志前缀：已绑定 request_id 时附 ``request_id=<id>``，未绑定不附。

    document 传文档指纹（file_name + 内容 sha256 前 8 位）时一并附加。
    """
    return observability.context_prefix(
        request_id=observability.get_request_id(),
        document=document,
    )


def _content_fingerprint(file_name: str, content: bytes) -> str:
    """文档指纹：file_name + 内容 sha256 前 8 位，用于日志与告警串联同一文档。"""
    return f"{Path(file_name or 'upload.bin').name}#{hashlib.sha256(content).hexdigest()[:8]}"


def _ontology_log_text(text: str, limit: int = ONTOLOGY_LOG_LINES) -> str:
    """日志用的本体文本：最多前 limit 行，超出部分只报行数并指向产物文件。"""
    lines = str(text or "").splitlines()
    if len(lines) <= limit:
        return str(text or "")
    head = "\n".join(lines[:limit])
    return f"{head}\n…（共 {len(lines)} 行，日志只展开前 {limit} 行；完整原文见运行目录 00-ontology.puml）"


@dataclass(slots=True)
class KnowledgeIngestionRequest:
    file_name: str
    content: bytes
    ontology: str
    options: PipelineOptions = field(default_factory=PipelineOptions)


@dataclass(slots=True)
class FileItem:
    file_name: str
    content: bytes


@dataclass(slots=True)
class BatchKnowledgeIngestionRequest:
    files: list[FileItem]
    ontology: str
    options: PipelineOptions = field(default_factory=PipelineOptions)


class PipelineError(Exception):
    def __init__(self, code: ErrorCode, info: str, stage: str, details: Any = None, partial_data: Any = None):
        super().__init__(info)
        self.code = code
        self.info = info
        self.stage = stage
        self.details = details
        self.partial_data = partial_data


def _ontology_digest(ontology: str) -> dict[str, Any]:
    """回报本次请求实际收到的本体，供前端确认参数是否送达、能否解析。

    前端选了本体却抽不出数据时，第一件要确认的就是"服务端到底收到了什么"。
    这里把原始文本、长度、指纹和解析结果一并带出；本体为空也如实回报，
    这样"没收到本体"和"收到了但解析不出类"能一眼区分开。
    """
    text = ontology or ""
    info: dict[str, Any] = {
        "received": bool(text.strip()),
        "length": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else "",
        "classes": [],
        "property_count": 0,
        "parse_error": None,
        "text": text,
    }
    if not text.strip():
        return info
    try:
        from algorithm.knowledge_extract.v2.ontology_schema import OntologySchema

        schema = OntologySchema.parse(text)
        info["classes"] = [item.identifier for item in schema.classes]
        info["property_count"] = sum(len(item.properties) for item in schema.classes)
    except Exception as exc:  # noqa: BLE001 - 回报解析失败本身就是这个字段的用途
        info["parse_error"] = f"{type(exc).__name__}: {exc}"
    return info


def _ontology_catalog(ontology: str) -> dict[str, Any]:
    """本体在抽取器眼里的样子：类 / 属性 / identity / 关系。

    前端传进来的 PlantUML 原文和抽取器实际编译出来的结构经常不是一回事
    （别名、stereotype 会决定 type id，``名称 (标识)`` 的第二段才是属性 identifier），
    所以抽取前把解析视图一并记录下来。
    """
    text = ontology or ""
    catalog: dict[str, Any] = {
        "received": bool(text.strip()),
        "characters": len(text),
        "classes": [],
        "relations": [],
        "parse_error": None,
    }
    if not text.strip():
        return catalog
    try:
        from algorithm.knowledge_extract.v2.ontology_schema import OntologySchema

        schema = OntologySchema.parse(text)
        for item in schema.classes:
            catalog["classes"].append(
                {
                    "type": item.identifier,
                    "display_name": item.display_name,
                    "stereotypes": list(item.stereotypes),
                    "identity": list(schema.identity_properties_for(item.identifier)),
                    "properties": [
                        {
                            "identifier": prop.identifier,
                            "display_name": prop.display_name,
                            "data_type": prop.data_type,
                        }
                        for prop in item.properties
                    ],
                }
            )
        catalog["relations"] = [
            {"identifier": rel.identifier, "source": rel.source, "target": rel.target}
            for rel in schema.relations
        ]
    except Exception as exc:  # noqa: BLE001 - 解析失败本身就是要看的信息
        catalog["parse_error"] = f"{type(exc).__name__}: {exc}"
    return catalog


def _snapshot_prefix(label: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", label)[:80] or "upload"
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}_{safe}"


def _save_stage_artifact(label: str, stage: str, content: str) -> None:
    """把解析全文 / 切片文本另存一份时间戳文件（兼容旧的使用习惯）。

    文件名形如 ``<时间戳>_<文件名>_parsed.md`` / ``..._chunks.json``；
    同一次运行的这三份产物另外还落在 ``<run_id>_<文件名>/`` 目录里。
    抽取及其之后的阶段不落盘。
    """
    try:
        EXTRACTION_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = EXTRACTION_SNAPSHOT_DIR / f"{_snapshot_prefix(label)}_{stage}"
        path.write_text(content, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - 落盘失败不应让流水线失败
        logger.error("%s[知识沉淀] 阶段产物写入失败: %s", _log_prefix(), exc)


def _save_error_log(label: str, exc: Exception) -> None:
    """流水线任意阶段失败时，把报错现场写进快照目录。"""
    try:
        EXTRACTION_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = EXTRACTION_SNAPSHOT_DIR / f"{_snapshot_prefix(label)}_error.log"
        lines = [
            f"[{datetime.now().isoformat(timespec='seconds')}] 知识沉淀处理失败",
            f"file: {label}",
        ]
        if isinstance(exc, PipelineError):
            lines += [
                f"stage: {exc.stage}",
                f"code: {exc.code.name}({exc.code})",
                f"info: {exc.info}",
                f"details: {json.dumps(exc.details, ensure_ascii=False, default=str) if exc.details is not None else None}",
            ]
        lines += [f"error: {type(exc).__name__}: {exc}", "", "traceback:", traceback.format_exc()]
        payload = "\n".join(lines)
        path.write_text(payload, encoding="utf-8")
        if _LAST_RUN_DIR is not None:
            # 同一次运行的目录里也留一份，便于"整个流程"在一个目录里看全。
            (_LAST_RUN_DIR / "run-error.log").write_text(payload, encoding="utf-8")
    except Exception as write_exc:  # noqa: BLE001 - 写日志失败不能吞掉原始异常
        logger.error("%s[知识沉淀] 报错日志写入失败: %s", _log_prefix(), write_exc)


def _log_pipeline_failure(method):
    """process / process_batch 的统一失败出口：先落盘报错日志再原样抛出。"""

    @functools.wraps(method)
    def wrapper(self, request):
        try:
            return method(self, request)
        except Exception as exc:
            if isinstance(request, BatchKnowledgeIngestionRequest):
                label = (
                    Path(request.files[0].file_name).stem + f"_等{len(request.files)}个文件"
                    if request.files
                    else "batch"
                )
            else:
                label = request.file_name
            _save_error_log(label, exc)
            raise

    return wrapper


def _summarize_warnings(warnings: Any, *, limit: int = 30) -> dict[str, Any]:
    """把抽取告警按 code 归类，便于一眼看出"没提议实体"还是"提议了被打掉"。"""
    rows = [item for item in (warnings or []) if isinstance(item, dict)]
    counter: dict[str, int] = {}
    for row in rows:
        code = str(row.get("code") or "UNKNOWN")
        counter[code] = counter.get(code, 0) + 1
    return {
        "total": len(rows),
        "by_code": dict(sorted(counter.items(), key=lambda item: (-item[1], item[0]))),
        "details": rows[:limit],
    }


def _graph_summary(
    final_graph: dict[str, Any] | None,
    final_graphs: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """抽取产物的 nodes/relationships 摘要：总数 + 按 label/type 分布，供 03-extract.json。"""
    graphs = [final_graph] if final_graph is not None else list(final_graphs or [])
    nodes = [
        node
        for graph in graphs
        for node in (graph.get("nodes") or [])
        if isinstance(node, dict)
    ]
    relationships = [
        relationship
        for graph in graphs
        for relationship in (graph.get("relationships") or [])
        if isinstance(relationship, dict)
    ]
    by_label: dict[str, int] = {}
    for node in nodes:
        label = str(node.get("label") or "UNKNOWN")
        by_label[label] = by_label.get(label, 0) + 1
    by_type: dict[str, int] = {}
    for relationship in relationships:
        rel_type = str(relationship.get("type") or "UNKNOWN")
        by_type[rel_type] = by_type.get(rel_type, 0) + 1
    return {
        "graph_count": len(graphs),
        "nodes": {
            "count": len(nodes),
            "by_label": dict(sorted(by_label.items(), key=lambda item: (-item[1], item[0]))),
        },
        "relationships": {
            "count": len(relationships),
            "by_type": dict(sorted(by_type.items(), key=lambda item: (-item[1], item[0]))),
        },
    }


class StageRecorder:
    """把知识沉淀流水线的每一步输出落成独立文档，并在日志里回执。

    每次运行建一个目录 ``<run_id>_<文件名>/``：``00-ontology.puml``（前端本体）、
    ``01-parse.md``（解析输出）、``02-chunking.json``（切片输出）、
    ``03-extract.json``（抽取的 nodes/relationships 摘要）、
    ``04-quality.json``（stats 质量块）。向量与存储阶段的结果只通过接口响应返回。

    所有落盘都吞掉异常：阶段产物写不进去不能让整条流水线失败。
    """

    def __init__(self, label: str, *, directory: Path | None = None) -> None:
        global _LAST_RUN_DIR
        safe = re.sub(r'[\\/:*?"<>|\s]+', "_", str(label or ""))[:60].strip("_")
        self.label = safe or "upload"
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.directory = (directory or EXTRACTION_SNAPSHOT_DIR) / f"{self.run_id}_{self.label}"
        _LAST_RUN_DIR = self.directory
        self.started = datetime.now()
        self._last = self.started
        self._available = self._prepare()

    def _prepare(self) -> bool:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            return True
        except Exception as exc:  # noqa: BLE001 - 产物目录创建失败不应中断流程
            logger.error("%s[知识沉淀] 阶段产物目录创建失败: %s", _log_prefix(), exc)
            return False

    def write(
        self,
        stage: str,
        filename: str,
        content: str,
        *,
        title: str = "",
        summary: str = "",
    ) -> str | None:
        """写一份步骤输出文档并打日志；返回落盘路径，未落盘时返回 None。"""
        now = datetime.now()
        elapsed = (now - self._last).total_seconds()
        self._last = now
        written: str | None = None
        path = self.directory / filename
        if self._available:
            try:
                path.write_text(content, encoding="utf-8")
                written = str(path)
            except Exception as exc:  # noqa: BLE001 - 单步落盘失败不影响流程
                logger.error("%s[知识沉淀] 步骤产物写入失败 stage=%s: %s", _log_prefix(), stage, exc)
        logger.info(
            "%s[知识沉淀] 步骤 %s 输出文档: %s（%d 字符, 耗时 %.2fs）→ %s%s",
            _log_prefix(),
            stage,
            filename,
            len(content),
            elapsed,
            written or "未落盘",
            f"｜{summary}" if summary else "",
        )
        return written

class KnowledgePrecipitationService:
    def __init__(
        self,
        processor_factory=DocumentProcessor,
        parsing: DocumentParsingService | None = None,
        chunking: DocumentChunkingService | None = None,
        extraction: GraphExtractionService | None = None,
        embeddings: EmbeddingService | None = None,
    ):
        self.parsing = parsing or DocumentParsingService(processor_factory)
        self.chunking = chunking or DocumentChunkingService()
        self.extraction = extraction or GraphExtractionService()
        self.embeddings = embeddings or EmbeddingService()

    @staticmethod
    def _parser_summary(metadata: dict[str, Any]) -> dict[str, Any]:
        return DocumentParsingService.parser_summary(metadata)

    @staticmethod
    def _upload_markdown(
        content: bytes,
        file_name: str,
        content_type: str,
        request: KnowledgeIngestionRequest,
        stage: str,
    ) -> dict[str, Any] | None:
        file_service_url = get_application_service_url()
        if not file_service_url:
            return None
        result = upload_file(
            content,
            file_name,
            content_type,
            file_service_url,
            request.options.storage.knowledge_space_id,
        )
        if not result.get("success"):
            raise PipelineError(ErrorCode.FILE_SERVICE_FAILED, f"{stage}产物存储失败", stage, result)
        normalized = {key: result.get(key) for key in ("id", "name", "path", "url")}
        if not normalized["id"]:
            raise PipelineError(ErrorCode.FILE_SERVICE_FAILED, "文件服务未返回文件 id", stage, result)
        return normalized

    def _run_pipeline(
        self,
        markdown: str,
        upload_stem: str,
        ontology: str,
        options: PipelineOptions,
        origin: dict[str, Any] | None,
        started: datetime,
        stats: dict[str, Any],
        recorder: StageRecorder | None = None,
        source_metadata: dict[str, Any] | None = None,
        document_segments: list[dict[str, Any]] | None = None,
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        chunk_started = time.monotonic()
        threshold = options.chunking.chunk_threshold
        if document_segments:
            # Batch input is one artifact for storage, but remains a sequence of
            # independent documents for extraction.  Chunk each segment before
            # combining it so a threshold bypass cannot create a cross-file
            # chunk (and so all ranges remain offsets into combined markdown).
            chunks = []
            for segment in document_segments:
                text = str(segment["text"])
                base = int(segment["start"])
                if len(text) <= threshold:
                    segment_items = [{"text": text, "range": [0, len(text)]}]
                else:
                    segment_items = self.chunking.chunk(text, options.chunking)["items"]
                segment_metadata = dict(segment.get("metadata") or {})
                for item in segment_items:
                    local_start, local_end = item["range"]
                    item = dict(item)
                    item["range"] = [base + local_start, base + local_end]
                    item["metadata"] = {
                        **segment_metadata,
                        **dict(item.get("metadata") or {}),
                    }
                    chunks.append(item)
            chunked = {"count": len(chunks), "items": chunks}
        elif len(markdown) <= threshold:
            chunked = {
                "count": 1,
                "items": [{"text": markdown, "range": [0, len(markdown)]}],
            }
            chunks = chunked["items"]
        else:
            chunked = self.chunking.chunk(markdown, options.chunking)
            chunks = chunked["items"]
        for item in chunks:
            item_metadata = dict(source_metadata or {})
            item_metadata.update(dict(item.get("metadata") or {}))
            item["metadata"] = item_metadata
        logger.info(
            "%s[知识沉淀] 切片完成: count=%d 总字符=%d threshold=%d",
            _log_prefix(fingerprint),
            len(chunks),
            sum(len(item["text"]) for item in chunks),
            threshold,
        )
        observability.record_stage_duration(
            "chunk",
            time.monotonic() - chunk_started,
            document=fingerprint,
            extra={"chunk_count": len(chunks)},
        )
        _save_stage_artifact(
            upload_stem,
            "chunks.json",
            json.dumps(chunks, ensure_ascii=False, indent=2, default=str),
        )
        if recorder is not None:
            recorder.write(
                "02-chunking",
                "02-chunking.json",
                json.dumps(chunks, ensure_ascii=False, indent=2, default=str),
                title="切片输出",
                summary=(
                    f"count={len(chunks)} 总字符={sum(len(item['text']) for item in chunks)}"
                    f" threshold={threshold}"
                ),
            )

        # 兼容两种抽取模式：传元模型走本体图谱抽取；不传元模型走非结构化摘要（切片+概要，不产图谱）
        has_ontology = bool((ontology or "").strip())
        if has_ontology:
            # 抽取前先把"前端传进来的本体长什么样"完整记下来：原文 + 抽取器的解析视图。
            ontology_catalog = _ontology_catalog(ontology)
            logger.info(
                "%s[知识沉淀] 抽取输入本体（前端原样传入）: %d字符 sha256=%s",
                _log_prefix(fingerprint),
                len(ontology),
                hashlib.sha256(ontology.encode("utf-8")).hexdigest()[:16],
            )
            logger.info(
                "%s[知识沉淀] 抽取输入本体原文（日志最多 %d 行）:\n%s",
                _log_prefix(fingerprint),
                ONTOLOGY_LOG_LINES,
                _ontology_log_text(ontology),
            )
            logger.info(
                "%s[知识沉淀] 本体解析视图（type / identity / 属性）:\n%s",
                _log_prefix(fingerprint),
                _ontology_log_text(
                    json.dumps(ontology_catalog["classes"], ensure_ascii=False, indent=2)
                ),
            )
            logger.info(
                "%s[知识沉淀] 本体解析视图（关系）: %s",
                _log_prefix(fingerprint),
                _ontology_log_text(
                    json.dumps(ontology_catalog["relations"], ensure_ascii=False)
                ),
            )
            if ontology_catalog["parse_error"]:
                # 本体解析错误提前阻断：不再流到抽取阶段才爆 4006。
                logger.error(
                    "%s[知识沉淀] 本体解析失败，已阻断: %s",
                    _log_prefix(fingerprint),
                    ontology_catalog["parse_error"],
                )
                observability.increment_counter("ontology_invalid_total")
                observability.send_alert(
                    "ontology_invalid",
                    {
                        "request_id": observability.get_request_id(),
                        "document": fingerprint,
                        "stage": "ontology",
                        "reason": ontology_catalog["parse_error"],
                    },
                )
                raise CapabilityError(
                    ErrorCode.ONTOLOGY_INVALID,
                    f"本体解析失败: {ontology_catalog['parse_error']}",
                    "ontology",
                    {"parse_error": ontology_catalog["parse_error"]},
                )
            if not ontology_catalog["classes"]:
                # 本体文本非空但一个类都解析不出来（流到抽取也只能 ONTOLOGY_EMPTY），
                # 同样提前按 ONTOLOGY_INVALID(4006) 失败。
                logger.error(
                    "%s[知识沉淀] 本体未解析出任何类声明，已阻断: %d字符",
                    _log_prefix(fingerprint),
                    len(ontology),
                )
                observability.increment_counter("ontology_invalid_total")
                observability.send_alert(
                    "ontology_invalid",
                    {
                        "request_id": observability.get_request_id(),
                        "document": fingerprint,
                        "stage": "ontology",
                        "reason": "本体未解析出任何类声明",
                    },
                )
                raise CapabilityError(
                    ErrorCode.ONTOLOGY_INVALID,
                    "本体未解析出任何类声明",
                    "ontology",
                    {"characters": len(ontology)},
                )
            extract_started = time.monotonic()
            extracted_batch = self.extraction.extract(
                ontology=ontology,
                items=[
                    TextChunk(
                        text=item["text"],
                        ranges=item["range"],
                        metadata=item.get("metadata") or {},
                    )
                    for item in chunks
                ],
                model=options.extraction.model,
                merge=options.extraction.merge,
            )
            extracted_items = extracted_batch.items
            extracted_result = extracted_batch.pipeline_data()
            final_graph = extracted_batch.graph
            final_graphs = extracted_batch.graphs
            node_count = (
                len(final_graph.get("nodes", []))
                if final_graph is not None
                else sum(len(graph.get("nodes", [])) for graph in final_graphs or [])
            )
            relationship_count = (
                len(final_graph.get("relationships", []))
                if final_graph is not None
                else sum(len(graph.get("relationships", [])) for graph in final_graphs or [])
            )
            stats["extraction"] = {
                "chunks_extracted": len(extracted_items),
                "merge": options.extraction.merge,
                "nodes_extracted": node_count,
                "relationships_extracted": relationship_count,
                "attempts": extracted_batch.attempts,
                "scheduler": extracted_batch.scheduler,
                "quality": extracted_batch.quality or {},
            }
            observability.record_stage_duration(
                "extract",
                time.monotonic() - extract_started,
                document=fingerprint,
                extra={
                    "chunk_count": len(extracted_items),
                    "node_count": node_count,
                    "relationship_count": relationship_count,
                },
            )
            empty_reason = extracted_batch.empty_reason
            if empty_reason and node_count == 0 and relationship_count == 0:
                # 空结果仅在此时给出分类：是"模型没提议"还是"提议了被校验打掉"。
                stats["extraction"]["empty_reason"] = empty_reason
                observability.increment_counter("empty_extraction_total")
            logger.info(
                "%s[知识沉淀] 抽取完成: model=%s nodes=%d relationships=%d merge=%s attempts=%d%s",
                _log_prefix(fingerprint),
                options.extraction.model or "env默认",
                node_count,
                relationship_count,
                options.extraction.merge,
                len(extracted_batch.attempts),
                f" empty_reason={empty_reason}" if empty_reason else "",
            )
            # 图谱全文与 attempts 全量不再打日志：抽取结果通过接口响应的
            # data.extraction / data.stats.extraction 返回，算法层另有 [步骤][LLM] 明细。

            # 抽取诊断：warnings 按 code 归类 + profile 计数。这两项此前既不在日志也不在
            # 快照里，导致"0 节点"无法区分"模型没提议实体"还是"提议了被校验打掉"。
            diagnostics = extracted_items[0] if extracted_items else {}
            extracted_warnings = list(diagnostics.get("warnings") or [])
            extracted_profile = dict(diagnostics.get("profile") or {})
            warning_summary = _summarize_warnings(extracted_warnings)
            logger.info(
                "%s[知识沉淀] 抽取诊断: warnings=%d by_code=%s empty_reason=%s",
                _log_prefix(fingerprint),
                warning_summary["total"],
                json.dumps(warning_summary["by_code"], ensure_ascii=False),
                empty_reason or "-",
            )
            logger.info(
                "%s[知识沉淀] 抽取诊断 profile: %s",
                _log_prefix(fingerprint),
                json.dumps(extracted_profile, ensure_ascii=False, default=str),
            )
            stats["extraction"]["warnings"] = warning_summary
            stats["extraction"]["profile"] = extracted_profile
            if empty_reason and node_count == 0 and relationship_count == 0:
                logger.warning(
                    "%s[知识沉淀] 抽取结果为空 empty_reason=%s: nodes=0 relationships=0 "
                    "chunks=%d warnings=%s",
                    _log_prefix(fingerprint),
                    empty_reason,
                    len(extracted_items),
                    json.dumps(warning_summary["by_code"], ensure_ascii=False),
                )
                observability.send_alert(
                    "empty_extraction",
                    {
                        "request_id": observability.get_request_id(),
                        "document": fingerprint,
                        "stage": "extract",
                        "empty_reason": empty_reason,
                        "node_count": node_count,
                        "relationship_count": relationship_count,
                        "chunk_count": len(extracted_items),
                        "warnings_by_code": warning_summary["by_code"],
                    },
                )
            if recorder is not None:
                # 03/04 只落摘要与质量块：完整图谱与诊断仍走接口响应，避免快照膨胀。
                recorder.write(
                    "03-extract",
                    "03-extract.json",
                    json.dumps(
                        _graph_summary(final_graph, final_graphs),
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    ),
                    title="抽取输出摘要（预测图 nodes/relationships 计数）",
                    summary=f"nodes={node_count} relationships={relationship_count}",
                )
                recorder.write(
                    "04-quality",
                    "04-quality.json",
                    json.dumps(
                        extracted_batch.quality or {},
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    ),
                    title="抽取质量块（stats.extraction.quality）",
                    summary=str((extracted_batch.quality or {}).get("status") or "-"),
                )

            if options.storage.persist_graph:
                raise PipelineError(
                    ErrorCode.EXTERNAL_CAPABILITY_UNAVAILABLE,
                    "知识图谱写入接口尚未开通",
                    "graph_storage",
                    {"capability": "graph.write"},
                    {"artifacts": {"markdown": origin}} if origin is not None else None,
                )
        else:
            from algorithm.knowledge_extract.v1.batch import summarize_chunks
            from algorithm.knowledge_extract.v2.config import get_llm_config as _get_llm_config

            summaries = summarize_chunks(
                [item["text"] for item in chunks],
                model=options.extraction.model,
            )
            logger.info(
                "%s[知识沉淀] 摘要完成: chunks=%d model=%s",
                _log_prefix(fingerprint),
                len(summaries),
                options.extraction.model or "env默认",
            )
            # 逐片摘要不打日志：完整内容通过响应返回，不再落盘。
            scheduler_cfg = _get_llm_config()
            final_graph = None
            final_graphs = None
            node_count = 0
            relationship_count = 0
            extracted_result = {
                "mode": "unstructured",
                "chunks": [
                    {
                        "index": item["index"],
                        "text": item["text"],
                        "range": chunks[item["index"]]["range"],
                        "metadata": chunks[item["index"]].get("metadata") or {},
                        "summary": item["summary"],
                    }
                    for item in summaries
                ],
            }
            stats["extraction"] = {
                "mode": "unstructured",
                "chunks_summarized": len(summaries),
                "attempts": [
                    {
                        "position": item["index"],
                        "error": item.get("error"),
                        "error_code": item.get("error_code"),
                        "attempts": item.get("attempts", []),
                    }
                    for item in summaries
                    if item.get("error") or item.get("attempts")
                ],
                "scheduler": {
                    "max_concurrency": scheduler_cfg["max_concurrency"],
                    "qps": scheduler_cfg["qps"],
                    "qpm": scheduler_cfg["qpm"],
                },
            }
            # 摘要结果同样不再落盘 03-extraction 产物。


        vector_ids = None
        vector_dimension = None
        if origin is not None:
            embedded = self.embeddings.texts(
                [TextEmbeddingItem(text=item["text"]) for item in chunks]
            )
            # 抽取之后不再产生任何日志与产物文件（向量化结果只在本次响应里返回）。
            vector_items = [
                {"text": chunk["text"], "vector": embedded_item["vector"]}
                for chunk, embedded_item in zip(chunks, embedded["items"])
            ]
            vector_result = store_vectors(
                vector_items,
                get_application_service_url(),
                options.storage.knowledge_space_id,
                origin["id"],
            )
            # 存储入库是"抽取结果展示之后"的独立步骤：这里失败不再抛错作废整篇，
            # 否则前端连抽取结果都看不到（异常虽带 extraction，但任务已判失败）。
            # 失败原因记入 stats 与日志，前端可按需重试入库。
            vector_ids = None
            inserted_count = None
            vector_dimension = embedded["dimension"]
            if not vector_result.get("success"):
                logger.error(
                    "%s[知识沉淀] 向量存储失败，已降级返回抽取结果: %s",
                    _log_prefix(fingerprint),
                    vector_result,
                )
                stats["storage"]["vector_error"] = {
                    "code": "VECTOR_STORE_FAILED",
                    "detail": vector_result,
                }
            else:
                vector_ids = vector_result.get("ids")
                inserted_count = vector_result.get("inserted_count")
                if (
                    not isinstance(vector_ids, list)
                    or inserted_count != len(vector_items)
                    or len(vector_ids) != len(vector_items)
                ):
                    logger.error(
                        "%s[知识沉淀] 向量存储返回 ids 数量不一致，已降级: input=%d inserted=%s ids=%s",
                        _log_prefix(fingerprint),
                        len(vector_items),
                        inserted_count,
                        len(vector_ids) if isinstance(vector_ids, list) else None,
                    )
                    stats["storage"]["vector_error"] = {
                        "code": "VECTOR_ID_COUNT_MISMATCH",
                        "input_count": len(vector_items),
                        "inserted_count": inserted_count,
                        "id_count": len(vector_ids) if isinstance(vector_ids, list) else None,
                    }
                    vector_ids = None
                    inserted_count = None
                else:
                    stats["storage"]["vectors"] = {"inserted_count": inserted_count}

        stats["chunking"] = {
            "chunk_count": chunked["count"],
            "strategy": options.chunking.strategy,
            "size": options.chunking.size,
            "separators": options.chunking.separators,
            "threshold":threshold,
            "vector_storage_enabled": origin is not None,
            "vector_dimension": vector_dimension,
            "vector_source": "text_chunk",
        }
        stats["total_time_seconds"] = round((datetime.now() - started).total_seconds(), 3)

        if not has_ontology:
            summary_text = f"共生成 {len(chunks)} 个切片及摘要。"
        elif final_graph is not None:
            summary_text = generate_understanding(final_graph)
        else:
            summary_text = f"共抽取 {len(final_graphs or [])} 个分片图谱，包含 {node_count} 个节点实例和 {relationship_count} 条关系实例。"

        return {
            "name": upload_stem,
            "summary": summary_text,
            "artifacts": (
                {"markdown": origin, "vector_ids": vector_ids}
                if origin is not None
                else None
            ),
            "ontology": _ontology_digest(ontology),
            "extraction": extracted_result,
            "stats": stats,
        }

    @_log_pipeline_failure
    def process(self, request: KnowledgeIngestionRequest) -> dict[str, Any]:
        if not request.content:
            raise PipelineError(ErrorCode.INVALID_REQUEST, "上传文件不能为空", "validation")

        started = datetime.now()
        file_name = Path(request.file_name or "upload.bin").name
        stem = Path(file_name).stem
        suffix = Path(file_name).suffix.lower()
        fingerprint = _content_fingerprint(file_name, request.content)
        # 本次运行的阶段产物目录：解析/切片/抽取/向量/存储/摘要 每步一份输出文档。
        recorder = StageRecorder(stem)
        stats: dict[str, Any] = {
            "document": {"file_name": file_name, "file_size": len(request.content), "file_type": suffix.lstrip(".")},
            "extraction": {},
            "chunking": {},
            "storage": {"enabled": bool(get_application_service_url())},
        }
        logger.info(
            "%s[知识沉淀] 开始处理: file=%s bytes=%d ontology=%s model=%s",
            _log_prefix(fingerprint),
            file_name,
            len(request.content),
            f"{len(request.ontology or '')}字符" if (request.ontology or "").strip() else "未提供",
            request.options.extraction.model or "env默认",
        )
        # 本体原文只在抽取阶段打一次（见 _run_pipeline），这里不再重复。
        logger.info(
            "%s[知识沉淀] 本次运行阶段产物目录: %s",
            _log_prefix(fingerprint),
            recorder.directory,
        )
        if (request.ontology or "").strip():
            # 本体是本次抽取的输入，作为第 0 步产物原样留档。
            recorder.write(
                "00-ontology",
                "00-ontology.puml",
                request.ontology,
                title="前端传入的本体（抽取输入原文）",
                summary=f"{len(request.ontology)}字符 sha256={hashlib.sha256(request.ontology.encode('utf-8')).hexdigest()[:16]}",
            )
        try:
            parse_started = time.monotonic()
            parsed = self.parsing.parse(file_name, request.content, request.options.parser)
            observability.record_stage_duration(
                "parse",
                time.monotonic() - parse_started,
                document=fingerprint,
                extra={"markdown_chars": len(parsed.get("markdown") or "")},
            )
            markdown = parsed["markdown"]
            logger.info(
                "%s[知识沉淀] 解析完成: file=%s parser=%s markdown=%d字符 sha256=%s",
                _log_prefix(fingerprint),
                file_name,
                parsed["parser"],
                len(markdown),
                hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:16],
            )
            # 解析全文不打日志：完整内容见运行目录 01-parse.md。
            _save_stage_artifact(stem, "parsed.md", markdown)
            recorder.write(
                "01-parse",
                "01-parse.md",
                markdown,
                title="解析输出（切片阶段的输入）",
                summary=f"parser={parsed['parser']} {len(markdown)}字符",
            )
            stats["document"].update(
                {"content_length": parsed["content_length"], "parser": parsed["parser"]}
            )
            origin = self._upload_markdown(
                markdown.encode("utf-8"), f"{stem}.md", "text/markdown", request, "origin_storage"
            )
            if origin is not None:
                stats["storage"]["origin"] = origin
                # 落盘细节已由算法层 [步骤][存储] 原文上传输出 打印，此处不再重复。

            return self._run_pipeline(
                markdown, stem, request.ontology, request.options, origin, started, stats,
                recorder=recorder,
                source_metadata={
                    "document_id": file_name,
                    "source_file": file_name,
                    "source_type": parsed["file_type"],
                    "signal_source": "parser",
                    "source_sheets": (parsed.get("parser") or {}).get("sheets") or [],
                },
                fingerprint=fingerprint,
            )
        except CapabilityError as exc:
            partial = None
            if stats["storage"].get("origin"):
                partial = {"artifacts": {"markdown": stats["storage"]["origin"]}}
            raise PipelineError(exc.code, exc.info, exc.stage, exc.details, partial) from exc

    @_log_pipeline_failure
    def process_batch(self, request: BatchKnowledgeIngestionRequest) -> dict[str, Any]:
        if not request.files:
            raise PipelineError(ErrorCode.INVALID_REQUEST, "至少需要上传一个文件", "validation")

        started = datetime.now()
        file_parsed: list[tuple[int, str, dict[str, Any]]] = []

        parse_started = time.monotonic()
        max_workers = min(len(request.files), os.cpu_count() or 4)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_file = {
                pool.submit(self.parsing.parse, f.file_name, f.content, request.options.parser): (index, f)
                for index, f in enumerate(request.files)
            }
            for future in as_completed(future_to_file):
                index, f = future_to_file[future]
                try:
                    parsed = future.result()
                except CapabilityError as exc:
                    raise PipelineError(
                        exc.code, f"文件 {f.file_name} 解析失败: {exc.info}", exc.stage, exc.details
                    ) from exc
                file_parsed.append((index, f.file_name, parsed))

        # Keep the historical filename ordering for distinct names, but make
        # repeated names stable according to the request order.
        file_parsed.sort(key=lambda x: (x[1], x[0]))

        size_by_index = {index: len(file.content) for index, file in enumerate(request.files)}
        combined_parts: list[str] = []
        file_stats: list[dict[str, Any]] = []
        for input_index, file_name, parsed in file_parsed:
            combined_parts.append(f"\n## 文件: {file_name}\n\n{parsed['markdown']}")
            file_stats.append({
                "file_name": file_name,
                "file_size": size_by_index.get(input_index, 0),
                "file_type": Path(file_name).suffix.lower().lstrip("."),
                "content_length": parsed["content_length"],
                "parser": parsed["parser"],
            })

        # 本体原文只在抽取阶段打一次（见 _run_pipeline），这里不再重复。
        combined_markdown = "\n".join(combined_parts)
        # Resolve offsets after joining, rather than reconstructing them from
        # parser lengths: the synthetic batch header and join newlines are part
        # of the public combined Markdown/range contract.
        document_segments: list[dict[str, Any]] = []
        cursor = 0
        for (input_index, file_name, parsed), part in zip(file_parsed, combined_parts):
            start = combined_markdown.find(part, cursor)
            if start < 0:  # defensive; parts were used to build this string
                raise PipelineError(ErrorCode.INVALID_REQUEST, "批量文档边界定位失败", "chunking")
            end = start + len(part)
            cursor = end
            # The request index is part of the internal ID so duplicate names,
            # and names that already contain a ``#2`` suffix, cannot collide.
            document_id = f"batch-{input_index + 1}:{file_name}"
            source_type = Path(file_name).suffix.lower().lstrip(".")
            document_segments.append(
                {
                    "text": part,
                    "start": start,
                    "end": end,
                    "metadata": {
                        "document_id": document_id,
                        "source_file": file_name,
                        "source_type": "batch",
                        "source_file_type": source_type,
                        "signal_source": "parser",
                        "source_sheets": (parsed.get("parser") or {}).get("sheets") or [],
                    },
                }
            )
        # 注意：combined_stem 必须先赋值再使用（此前先调用后赋值会在批量入口直接 NameError）。
        combined_stem = Path(request.files[0].file_name).stem + f"_等{len(request.files)}个文件"
        combined_fingerprint = _content_fingerprint(f"{combined_stem}.md", combined_markdown.encode("utf-8"))
        logger.info(
            "%s[知识沉淀] 批量解析完成: files=%d combined=%d字符 sha256=%s",
            _log_prefix(combined_fingerprint),
            len(request.files),
            len(combined_markdown),
            hashlib.sha256(combined_markdown.encode("utf-8")).hexdigest()[:16],
        )
        observability.record_stage_duration(
            "parse",
            time.monotonic() - parse_started,
            document=combined_fingerprint,
            extra={"file_count": len(request.files)},
        )
        # 批量合并全文不打日志：完整内容见运行目录 01-parse.md。
        recorder = StageRecorder(combined_stem)
        logger.info(
            "%s[知识沉淀] 本次运行阶段产物目录: %s",
            _log_prefix(combined_fingerprint),
            recorder.directory,
        )
        if (request.ontology or "").strip():
            recorder.write(
                "00-ontology",
                "00-ontology.puml",
                request.ontology,
                title="前端传入的本体（抽取输入原文）",
                summary=f"{len(request.ontology)}字符 sha256={hashlib.sha256(request.ontology.encode('utf-8')).hexdigest()[:16]}",
            )
        _save_stage_artifact(combined_stem, "parsed.md", combined_markdown)
        recorder.write(
            "01-parse",
            "01-parse.md",
            combined_markdown,
            title="批量解析输出（切片阶段的输入）",
            summary=f"files={len(request.files)} 合并后 {len(combined_markdown)}字符",
        )
        stats: dict[str, Any] = {
            "documents": file_stats,
            "extraction": {},
            "chunking": {},
            "storage": {"enabled": bool(get_application_service_url())},
        }

        # Build a dummy single-file request for _upload_markdown compatibility
        dummy_request = KnowledgeIngestionRequest(
            file_name=f"{combined_stem}.md",
            content=combined_markdown.encode("utf-8"),
            ontology=request.ontology,
            options=request.options,
        )
        origin = self._upload_markdown(
            combined_markdown.encode("utf-8"),
            f"{combined_stem}.md",
            "text/markdown",
            dummy_request,
            "origin_storage",
        )
        if origin is not None:
            stats["storage"]["origin"] = origin
            # 落盘细节已由算法层 [步骤][存储] 原文上传输出 打印，此处不再重复。

        result = self._run_pipeline(
            combined_markdown, combined_stem, request.ontology, request.options, origin, started, stats,
            recorder=recorder,
            source_metadata={
                "source_type": "batch",
                "signal_source": "parser",
                "source_files": [item["file_name"] for item in file_stats],
            },
            document_segments=document_segments,
            fingerprint=combined_fingerprint,
        )
        # Augment summary with file count
        fc = len(request.files)
        if result["summary"]:
            result["summary"] += f"，来源：{fc} 个文件"
        return result
