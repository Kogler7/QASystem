from __future__ import annotations
from docker.configuration import getenv as config_getenv

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable

from loguru import logger

from . import BaseProcessor
from ..page_edges import (
    DEFAULT_MIN_CHARS,
    extract_edge_lines,
    merge_page_edges,
    missing_lines,
    normalize_keep_page_edges,
    page_is_kept,
)
from ..table_json import convert_tables

# 4.0 档位；默认 standard（4.0 自身默认），部署可用 MINERU4_TIER 覆盖。
DEFAULT_TIER = "standard"
# 子进程解析超时（秒），大文档可调
DEFAULT_TIMEOUT_SECONDS = 1800
WORKER_NAME = "mineru4_worker.py"
TIERS = ("flash", "basic", "standard", "advanced")
# 我们的 ocr_mode -> 4.0 的 ocr_mode，语义保持一致
OCR_MODE_MAP = {"auto": "auto", "on": "ocr", "off": "txt"}
# MinerU 4.x 只对 PDF/图片提供分档解析；Office、OFD、HTML、CSV、EPUB 仅 flash 档
# （见 mineru/filetypes.py 的 FLASH_ONLY_PARSE_EXTENSIONS）。
FLASH_ONLY_SUFFIXES = frozenset({
    "doc", "docx", "ppt", "pptx", "xls", "xlsx", "rtf",
    "odt", "ods", "odp", "ofd", "html", "htm", "shtml", "csv", "tsv", "epub",
})
# 4.0 只对 PDF 支持 page_range
PAGE_RANGE_SUFFIXES = frozenset({"pdf"})


def mineru_major_version() -> int | None:
    """返回当前解释器里 MinerU 的安装主版本号；读不到版本时返回 None。"""
    try:
        from importlib.metadata import version as _dist_version

        return int(str(_dist_version("mineru")).split(".", 1)[0])
    except Exception:
        return None


def _normalized_index(text: str) -> tuple[str, list[int]]:
    """返回"去掉空白后的文本"及其中每个字符在原串中的下标，用于宽松定位。"""
    chars: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(text):
        if char.isspace():
            continue
        chars.append(char)
        positions.append(index)
    return "".join(chars), positions


def _page_anchor_texts(structured: Any) -> list[str]:
    """取每页第一个"会出现在 Markdown 里"的块文本，作为分页锚点。"""
    anchors: list[str] = []
    if not isinstance(structured, dict):
        return anchors
    for page in (structured or {}).get("pages", []) or []:
        anchor = ""
        for block in page.get("blocks", []) or []:
            if block.get("type") in {"header", "footer"}:
                continue
            content = str(block.get("content") or "").strip()
            if len(content) >= 4:
                anchor = content
                break
        anchors.append(anchor)
    return anchors


def _block_start(markdown: str, position: int) -> int:
    """把锚点位置回退到所在「块」的开头。

    锚点匹配到的是块内文字（例如 ``## 1 范围`` 里的 ``1 范围``），直接按字符位置拼接会把
    Markdown 结构前缀（``## ``、``- ``、``| `` 等）切开，所以这里回退到行首，并继续跨过空行，
    保证插入点落在块边界上。
    """
    if position <= 0:
        return 0
    line_start = markdown.rfind("\n", 0, position) + 1
    while line_start > 0:
        previous_end = line_start - 1
        previous_start = markdown.rfind("\n", 0, previous_end) + 1
        if markdown[previous_start:previous_end].strip():
            break
        line_start = previous_start
    return line_start


def _page_boundaries(markdown: str, anchors: list[str]) -> list[tuple[int, int]]:
    """把每页锚点定位到 Markdown 上，返回 [(start, end)]；定位不到的页退化为 (-1, -1)。"""
    normalized, positions = _normalized_index(markdown)
    boundaries: list[tuple[int, int]] = []
    cursor = 0
    for anchor in anchors:
        key = "".join(anchor.split())
        start = None
        if key:
            found = normalized.find(key, cursor)
            if found >= 0:
                start = _block_start(markdown, positions[found])
        boundaries.append((start if start is not None else -1, -1))
    # 每页的结束位置 = 下一个定位成功页的起点
    for index in range(len(boundaries)):
        if boundaries[index][0] < 0:
            continue
        end = len(markdown)
        for later in range(index + 1, len(boundaries)):
            if boundaries[later][0] >= 0:
                end = boundaries[later][0]
                break
        boundaries[index] = (boundaries[index][0], max(end, boundaries[index][0]))
    return boundaries


class Mineru4Processor(BaseProcessor):
    """MinerU 4.x（本地推理）处理器，契约与 ``OCRProcessor`` 一致。"""

    def __init__(self, tier: str | None = None, engine_name: str = "mineru4") -> None:
        self.tier = tier
        # engine=mineru / vlm 在 4.x 环境下复用本处理器，元数据里保留调用方请求的引擎名
        self.engine_name = engine_name

    # ------------------------------------------------------------ 内部工具

    def _run_in_subprocess(
        self,
        file_path: str,
        *,
        tier: str,
        ocr_mode: str,
        page_range: str,
    ) -> tuple[str, dict, dict]:
        """在隔离子进程解析，返回 (markdown, structured, info)。"""
        python = sys.executable
        worker = Path(__file__).with_name(WORKER_NAME)
        if not worker.is_file():
            raise RuntimeError(f"缺少 4.x 子进程 worker：{worker}")

        timeout = int(config_getenv("MINERU4_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS)
        workdir = tempfile.mkdtemp(prefix="mineru4-")
        try:
            cmd = [
                python, str(worker),
                "--pdf", str(file_path),
                "--out-dir", workdir,
                "--tier", tier,
                "--ocr-mode", OCR_MODE_MAP.get(ocr_mode, ocr_mode),
                "--page-range", page_range,
            ]
            completed = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, env=os.environ.copy()
            )
            info_path = Path(workdir) / "result.json"
            info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.is_file() else {}
            if completed.returncode != 0 or not info.get("ok"):
                detail = info.get("error") or (completed.stderr or "").strip().splitlines()[-1:] or "未知错误"
                raise RuntimeError(f"MinerU 4.x 子进程解析失败：{detail}")
            markdown = (Path(workdir) / "markdown.md").read_text(encoding="utf-8")
            structured_path = Path(workdir) / "structured.json"
            structured = (
                json.loads(structured_path.read_text(encoding="utf-8"))
                if structured_path.is_file() else {}
            )
            return markdown, structured, info
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _resolve_tier(explicit: str | None) -> str:
        tier = (explicit or config_getenv("MINERU4_TIER") or DEFAULT_TIER).strip().lower()
        return tier if tier in TIERS else DEFAULT_TIER

    @staticmethod
    def _page_range(start_page_id: int, end_page_id: int | None) -> str:
        """0 基闭区间 -> 4.0 的 1 基闭区间字符串；未限定范围时返回空串。"""
        if not start_page_id and end_page_id is None:
            return ""
        first = max(int(start_page_id), 0) + 1
        last = (int(end_page_id) + 1) if end_page_id is not None else first
        return f"{first}-{last}" if last > first else str(first)

    def _inject_page_edges(
        self,
        *,
        markdown: str,
        structured: Any,
        source_path: str,
        start_page_id: int,
        selection: Any,
        min_chars: int,
    ) -> tuple[str, list[dict]]:
        """按与其它引擎相同的规则补回页眉/页脚。

        页内位置用 4.0 自己的 structured_content 锚点定位（宽松匹配，忽略空白差异），
        文本仍取自 PDF 文本层，保证与 docling/mineru 路线输出一致。
        """
        anchors = _page_anchor_texts(structured)
        boundaries = _page_boundaries(markdown, anchors)
        injections: list[dict] = []
        # 从后往前插入，避免前面的插入影响后面的偏移
        for index in range(len(anchors) - 1, -1, -1):
            page_number = index + 1  # 解析范围内 1 基页号
            if not page_is_kept(selection, page_number, 1):
                continue
            absolute_page = int(start_page_id) + page_number
            start, end = boundaries[index]
            if start < 0:
                start, end = 0, len(markdown)
            chunk = markdown[start:end]
            edges = extract_edge_lines(source_path, absolute_page, min_chars=min_chars)
            top = missing_lines(edges["top"], chunk)
            bottom = missing_lines(edges["bottom"], chunk)
            if not top and not bottom:
                continue
            replacement = merge_page_edges(chunk, top, bottom)
            # merge_page_edges 会裁剪首尾空白，这里保持与原片段一致的边界
            markdown = markdown[:start] + replacement + markdown[end:]
            injections.append({"page": page_number, "top": top, "bottom": bottom})
        injections.reverse()
        return markdown, injections

    # ------------------------------------------------------------ 主流程

    def process(self, file_path: str, **kwargs) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {
            "file_type": "pdf",
            "processor": "mineru4",
            "backend": "mineru-4",
            "engine": self.engine_name,
            "supported_formats": [".pdf", ".ofd", ".docx", ".doc", ".pptx", ".ppt", ".png", ".jpg", ".jpeg"],
        }

        if not os.path.exists(file_path):
            return {"content": "", "metadata": {**metadata, "error": f"文件不存在 - {file_path}"}}

        ocr_mode = str(kwargs.get("ocr_mode") or "auto").strip().lower()
        start_page_id = int(kwargs.get("start_page_id") or 0)
        end_page_id = kwargs.get("end_page_id")
        selection = normalize_keep_page_edges(kwargs.get("keep_page_edges"))
        page_edge_min_chars = kwargs.get("page_edge_min_chars")
        min_chars = DEFAULT_MIN_CHARS if page_edge_min_chars is None else int(page_edge_min_chars)
        suffix = Path(file_path).suffix.lower().lstrip(".")
        tier = self._resolve_tier(kwargs.get("tier") or self.tier)
        tier_reason = None
        if suffix in FLASH_ONLY_SUFFIXES and tier != "flash":
            # 4.0 对这些格式只提供 flash 档，强行用其它档位会被拒绝
            tier_reason = f"{suffix} 在 MinerU 4.x 仅支持 flash 档"
            tier = "flash"

        ignored = {
            key: kwargs.get(key)
            for key in ("lang", "table_enable", "formula_enable")
            if kwargs.get(key) is not None
        }

        try:
            page_range = (
                self._page_range(start_page_id, end_page_id)
                if suffix in PAGE_RANGE_SUFFIXES else ""
            )
            # Native model initialization can hold the GIL longer than Uvicorn's
            # heartbeat timeout. Always isolate it, even when MinerU shares Python.
            content, structured, info = self._run_in_subprocess(
                file_path, tier=tier, ocr_mode=ocr_mode, page_range=page_range
            )
            mode = "subprocess"
            version = str(info.get("mineru_version") or "")

            metadata["mineru4_options"] = {
                "tier": tier,
                "ocr_mode": ocr_mode,
                "page_range": page_range,
                "version": version,
                "mode": mode,
                "file_suffix": suffix,
                **({"tier_reason": tier_reason} if tier_reason else {}),
                # 4.0 无这些开关/参数，这里如实记录，避免调用方以为生效了
                "ignored_options": ignored,
            }

            # 表格按形状分流：行列式数据表出 columns/rows JSON，稀疏表出键值对 JSON。
            content, table_stats = convert_tables(content, source_name=file_path)
            metadata["tables_json"] = {
                "count": table_stats["tables"],
                "forms": table_stats["forms"],
            }
            if table_stats["forms"]:
                logger.info("稀疏表转键值对 JSON: {} 张", table_stats["forms"])

            # 页眉/页脚按需补回（同一套 page_edges 规则）；关闭时也记录，便于确认是有意不补回
            injected: list[dict] = []
            if selection is not None and suffix in PAGE_RANGE_SUFFIXES:
                content, injected = self._inject_page_edges(
                    markdown=content,
                    structured=structured,
                    source_path=file_path,
                    start_page_id=start_page_id,
                    selection=selection,
                    min_chars=min_chars,
                )
                if injected:
                    logger.info("保留页眉/页脚: {} 页补回边缘文本 {}", len(injected), injected)
            metadata["page_edges"] = {"mode": str(kwargs.get("keep_page_edges")), "injected": injected}

            return {"content": content, "metadata": metadata}
        except Exception as exc:  # 与 OCRProcessor 一致：失败信息进 metadata['error']
            logger.error("MinerU 4.x 解析失败: {}", exc)
            return {"content": "", "metadata": {**metadata, "error": str(exc)}}
