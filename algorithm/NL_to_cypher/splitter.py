from __future__ import annotations

import re

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownTextSplitter, RecursiveCharacterTextSplitter


def _cosine_similarity(a, b) -> float:
    """Cosine similarity between two numpy vectors."""
    import numpy as np

    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.shape != b.shape:
        return 0.0

    a_norm = np.linalg.norm(a)
    b_norm = np.linalg.norm(b)
    if a_norm == 0.0 or b_norm == 0.0:
        return 0.0
    return float(np.dot(a, b) / (a_norm * b_norm))


def split_by_characters(
    documents: list[Document],
    chunk_size: int = 500,
    separators: list[str] | None = None,
    chunk_overlap: int = 0,
) -> list[Document]:
    """Split documents through every configured separator, then pack chunks."""
    active_separators = [separator for separator in (separators or ["\n"]) if separator]
    if not active_separators:
        active_separators = ["\n"]

    fragments: list[Document] = []
    for document in documents:
        texts = [document.page_content]
        for separator in active_separators:
            next_texts: list[str] = []
            for text in texts:
                parts = text.split(separator)
                next_texts.extend(
                    part + (separator if index < len(parts) - 1 else "")
                    for index, part in enumerate(parts)
                    if part or index < len(parts) - 1
                )
            texts = next_texts
        fragments.extend(
            Document(page_content=text, metadata=dict(document.metadata))
            for text in texts
            if text
        )

    if chunk_overlap > 0:
        return _pack_with_overlap(fragments, chunk_size, chunk_overlap)

    chunks: list[Document] = []
    for fragment in fragments:
        if len(fragment.page_content) <= chunk_size:
            if chunks and len(chunks[-1].page_content) + len(fragment.page_content) <= chunk_size:
                chunks[-1].page_content += fragment.page_content
            else:
                chunks.append(fragment)
            continue
        chunks.extend(
            Document(
                page_content=fragment.page_content[start:start + chunk_size],
                metadata=dict(fragment.metadata),
            )
            for start in range(0, len(fragment.page_content), chunk_size)
        )
    return chunks


def _pack_with_overlap(
    fragments: list[Document],
    chunk_size: int,
    chunk_overlap: int,
) -> list[Document]:
    if not fragments:
        return []

    chunks: list[Document] = []
    # 以 fragment 为粒度累积，避免切断句子
    buf_frags: list[str] = []
    buf_len = 0
    meta = dict(fragments[0].metadata)

    def flush(force: bool = False) -> None:
        nonlocal buf_frags, buf_len
        if not buf_frags:
            return
        text = "".join(buf_frags)
        if len(text) <= chunk_size or force:
            if text.strip():
                chunks.append(Document(page_content=text, metadata=dict(meta)))
            buf_frags, buf_len = [], 0

    for frag in fragments:
        content = frag.page_content
        if not content:
            continue
        # 若加入后超长：先输出当前缓冲，再把当前 frag 作为新缓冲起点
        if buf_len + len(content) > chunk_size and buf_frags:
            flush(force=True)
        buf_frags.append(content)
        buf_len += len(content)

    flush(force=True)

    # 应用 overlap：在相邻块之间复制尾部若干【完整 fragment】
    if chunk_overlap <= 0 or len(chunks) < 2:
        return chunks

    overlapped: list[Document] = [chunks[0]]
    for k in range(1, len(chunks)):
        prev_text = overlapped[-1].page_content
        tail = _tail_with_boundary(prev_text, chunk_overlap)
        if tail and tail != prev_text:
            new_text = tail + chunks[k].page_content
            if len(new_text) > chunk_size + chunk_overlap:
                # 截断 tail 使总长度不超过 chunk_size + chunk_overlap
                allowed_tail_len = max(0, (chunk_size + chunk_overlap) - len(chunks[k].page_content))
                tail = tail[-allowed_tail_len:] if allowed_tail_len > 0 else ""
                new_text = tail + chunks[k].page_content
            overlapped.append(Document(page_content=new_text, metadata=dict(chunks[k].metadata)))
        else:
            overlapped.append(chunks[k])
    return overlapped


def split_recursively(
    documents: list[Document],
    chunk_size: int = 500,
    separators: list[str] | None = None,
    chunk_overlap: int = 0,
) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(
        separators=separators or ["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""],
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    return splitter.split_documents(documents)


def split_by_markdown(
    documents: list[Document],
    chunk_size: int = 500,
    chunk_overlap: int = 0,
) -> list[Document]:
    splitter = MarkdownTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    return splitter.split_documents(documents)


def split_semantically(
    documents: list[Document],
    chunk_size: int = 500,
    similarity: float = 0.6,
    chunk_overlap: int = 0,
    model_path: str | None = None,
    breakpoint_method: str = "percentile",
    min_segments_per_chunk: int = 2, 
) -> list[Document]:
    """Split text by semantic boundaries using Max-Min chunking + breakpoint detection.

    Pipeline:
    1. Pre-split by Markdown headers (#, ##, ...) for structure awareness.
    2. Within each section, split into sentence-level segments.
    3. Batch-encode all segments with SentenceTransformer.
    4. Merge tiny (<20 char) segments into neighbors.
    5. Detect semantic breakpoints (valleys in consecutive similarity).
    6. Group segments using the Max-Min algorithm.
    7. Apply character-level overlap if configured.

    *similarity* serves as *tau* (initial threshold) in Max-Min.
    *breakpoint_method* controls how hard semantic boundaries are detected:
    ``"percentile"`` (default), ``"stddev"``, or ``"none"``.
    """
    import numpy as np

    from algorithm.NL_to_cypher.vector import _load_model, _model_path

    resolved_path = _model_path(model_path)
    model = _load_model(resolved_path)

    # Derive min_similarity from tau (lower bound for MaxSim).
    # Keeps chunks from collapsing when MinSim drops below a reasonable floor.
    min_similarity = max(0.3, similarity - 0.15)

    result: list[Document] = []
    for document in documents:
        text = document.page_content
        if not text.strip():
            continue

        # Step 1: Pre-split by Markdown headers for structure awareness
        sections = _split_by_markdown_headers(text)

        # Step 2-3: Split each section into segments + collect for batch encoding
        all_segments: list[str] = []
        section_spans: list[tuple[int, int]] = []
        for section in sections:
            segs = _split_into_segments(section, list(_SENTENCE_SEPARATORS))
            if not segs:
                continue
            start = len(all_segments)
            all_segments.extend(segs)
            section_spans.append((start, len(all_segments)))

        if not all_segments:
            continue

        # Batch-encode all segments (single model call for efficiency)
        all_embeddings = model.encode(all_segments, convert_to_numpy=True)

        # Step 4-6: Process each section independently
        for start, end in section_spans:
            section_segs = all_segments[start:end]
            section_embs = all_embeddings[start:end]

            # Merge tiny segments
            merged_segs, merged_embs = _merge_tiny_segments(section_segs, section_embs)
            if len(merged_segs) == 0:
                continue

            # Detect breakpoints (valleys in consecutive similarity)
            bps = _detect_breakpoints(merged_segs, merged_embs, method=breakpoint_method)

            # Max-Min grouping
            chunks = _group_segments_maxmin(
                merged_segs,
                merged_embs,
                chunk_size,
                tau=similarity,
                min_similarity=min_similarity,
                breakpoints=bps,
                min_segments=min_segments_per_chunk, 
            )
            chunks = _merge_short_chunks(
                chunks, chunk_size, min_fill_ratio=0.5
            )

            for chunk_text in chunks:
                result.append(Document(page_content=chunk_text, metadata=dict(document.metadata)))

    result_texts = [d.page_content for d in result]
    result_texts = _merge_short_chunks(result_texts, chunk_size, min_fill_ratio=0.5)
    result = [Document(page_content=t, metadata=dict(result[i].metadata))
              for i, t in enumerate(result_texts)]
    if chunk_overlap > 0 and len(result) > 1:
        result = _apply_overlap_to_chunks(result, chunk_size, chunk_overlap)

    return result


_SENTENCE_SEPARATORS: list[str] = [
    "\n\n",
    "。",
    "！",
    "？",
    "；",
    ". ",
    "! ",
    "? ",
    "; ",
]

_HEADER_PATTERN = re.compile(r"^(#{1,6}\s)", re.MULTILINE)

def _merge_short_chunks(
    chunks: list[str],
    chunk_size: int,
    min_fill_ratio: float = 0.5,
) -> list[str]:
    """合并过短的 chunk 到相邻 chunk 中。
    
    如果 chunk 长度 < chunk_size * min_fill_ratio，
    尝试合并到前一个（优先）或后一个 chunk，只要不超 chunk_size。
    """
    if not chunks:
        return chunks
    
    min_len = int(chunk_size * min_fill_ratio)
    result: list[str] = []
    
    for chunk in chunks:
        if len(chunk) >= min_len:
            # 够长，直接保留
            result.append(chunk)
        else:
            # 太短了，尝试合并
            if result and len(result[-1]) + len(chunk) <= chunk_size:
                # 合并到前一个
                result[-1] += chunk
            else:
                result.append(chunk)
    
    return result


def _split_into_segments(text: str, separators: list[str] | None = None) -> list[str]:
    """Split text into atomic segments at sentence / paragraph boundaries.

    Returns cleaned segments with separators merged into preceding content.
    Short segments (< *min_segment_len*) are kept separate and will be
    merged back by the caller based on semantic similarity.
    """
    if separators is None:
        separators = list(_SENTENCE_SEPARATORS)

    segments: list[str] = []
    buf = ""
    i = 0
    while i < len(text):
        matched = False
        for sep in separators:
            if text[i:].startswith(sep):
                if buf:
                    segments.append(buf)
                    buf = ""
                segments.append(sep)
                buf = ""
                i += len(sep)
                matched = True
                break
        if not matched:
            buf += text[i]
            i += 1
    if buf:
        segments.append(buf)

    merged: list[str] = []
    current = ""
    for seg in segments:
        if seg in separators:
            current += seg
        else:
            if current:
                merged.append(current)
            current = seg
    if current:
        merged.append(current)
    cleaned = [m for m in merged if m and m.strip()]
    if not cleaned and text.strip():
        return [text.strip()]

    return [m for m in merged if m and m.strip()]


def _split_by_markdown_headers(text: str) -> list[str]:
    """Split text at Markdown header boundaries for structure-aware chunking.

    Headers (#, ##, ###, etc.) indicate section boundaries. Each section
    (including its header line) is returned as an independent unit that
    will be semantically chunked separately, preventing chunks from
    spanning across unrelated sections.
    """
    if not text.strip():
        return [text]

    matches = list(_HEADER_PATTERN.finditer(text))
    if not matches:
        return [text]

    sections: list[str] = []
    for i, match in enumerate(matches):
        start = match.start()
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            end = len(text)
        section = text[start:end].strip()
        if section:
            sections.append(section)

    if matches and matches[0].start() > 0:
        prefix = text[: matches[0].start()].strip()
        if prefix:
            sections.insert(0, prefix)
    if len(sections) >= 2:
        def _is_title_only(s):
            body = "\n".join(s.splitlines()[1:]).strip()
            return bool(re.match(r"^#{1,6}\s+\S", s.splitlines()[0])) and not body
        if _is_title_only(sections[0]):
            sections[1] = sections[0] + "\n" + sections[1]
            sections = sections[1:]

    return sections if sections else [text]


def _merge_tiny_segments(
    segments: list[str],
    embeddings: "numpy.ndarray",
    min_segment_len: int = 20,
) -> tuple[list[str], "numpy.ndarray"]:
    """Merge tiny segments into neighboring content segments.

    Segments shorter than *min_segment_len* (YAML frontmatter, delimiters,
    empty markers) are merged into adjacent content so they don't produce
    low-quality singleton embeddings that distort the grouping step.
    """
    import numpy as np

    merged_segs: list[str] = []
    merged_embs: list[np.ndarray] = []
    buf_text = ""
    buf_embs: list[np.ndarray] = []

    for seg, emb in zip(segments, embeddings):
        if len(seg.strip()) < min_segment_len:
            buf_text += seg
            buf_embs.append(emb)
        else:
            if buf_text:
                buf_text += seg
                buf_embs.append(emb)
                merged_segs.append(buf_text)
                merged_embs.append(np.mean(buf_embs, axis=0))
                buf_text = ""
                buf_embs = []
            else:
                merged_segs.append(seg)
                merged_embs.append(emb)
    if buf_text:
        if merged_segs:
            merged_segs[-1] += buf_text
            merged_embs[-1] = np.mean([merged_embs[-1]] + buf_embs, axis=0)
        else:
            merged_segs.append(buf_text)
            merged_embs.append(np.mean(buf_embs, axis=0))

    if not merged_segs:
        return [], np.array([])

    return merged_segs, np.array(merged_embs)


def _detect_breakpoints(
    segments: list[str],
    embeddings: "numpy.ndarray",
    method: str = "percentile",
    percentile: float = 15.0,
) -> set[int]:
    """Detect semantic breakpoints between consecutive segments.

    Computes cosine similarity between consecutive segments and identifies
    valleys (sharp drops) as natural semantic boundaries. These breakpoints
    act as hard boundaries that the Max-Min grouper will not cross.

    *method='percentile'*: similarities below the given percentile are breakpoints.
    *method='stddev'*: similarities below (mean - 1.5 * std) are breakpoints.
    *method='none'*: no breakpoint detection.
    """
    import numpy as np

    if len(segments) < 2 or method == "none":
        return set()

    similarities = np.array([
        _cosine_similarity(embeddings[i], embeddings[i + 1])
        for i in range(len(segments) - 1)
    ])

    if method == "percentile":
        threshold = float(np.percentile(similarities, percentile))
    elif method == "stddev":
        mean = float(np.mean(similarities))
        std = float(np.std(similarities))
        threshold = mean - 1.5 * std
    else:
        return set()

    return {int(i) + 1 for i in np.where(similarities < threshold)[0]}


def _group_segments_maxmin(
    segments: list[str],
    embeddings: "numpy.ndarray",
    chunk_size: int,
    tau: float = 0.65,
    min_similarity: float = 0.45,
    breakpoints: set[int] | None = None,
    min_segments: int = 1,
) -> list[str]:
    """Group segments into chunks using the Max-Min semantic chunking algorithm.

    Unlike centroid-based grouping, this uses pairwise similarity comparisons
    to eliminate centroid drift and produce semantically tighter chunks.

    Algorithm (Bhat et al., 2025):
    1. When the current chunk has only 1 segment s₀:
       merge sᵢ if cosine(s₀, sᵢ) >= *tau*.
    2. When the chunk has ≥2 segments:
       - MinSim = minimum pairwise cosine similarity within the chunk
       - MaxSim = maximum cosine similarity between sᵢ and any segment in the chunk
       - Merge if MaxSim >= MinSim AND MaxSim >= *min_similarity*.
    3. *breakpoints* are hard boundaries that always force a new chunk.
    """
    import numpy as np

    breakpoints = breakpoints or set()

    if len(segments) == 0:
        return []

    chunks: list[str] = []
    chunk_segs: list[str] = [segments[0]]
    chunk_embs: list[np.ndarray] = [embeddings[0]]
    chunk_minsim: float = 1.0

    for i in range(1, len(segments)):
        seg = segments[i]
        emb = embeddings[i]
        current_len = sum(len(s) for s in chunk_segs)

        # Hard breakpoint → force new chunk
        if i in breakpoints:
            chunks.append("".join(chunk_segs))
            chunk_segs = [seg]
            chunk_embs = [emb]
            chunk_minsim = 1.0
            continue

        # Compute all pairwise similarities between new segment and chunk once.
        # We need both MaxSim (for the decision rule) and the individual values
        # (for updating MinSim). A single pass avoids redundant norm calls.
        pairwise_sims = [_cosine_similarity(emb, e) for e in chunk_embs]
        max_sim = max(pairwise_sims) if pairwise_sims else 0.0

        # Size overflow → close current chunk, start new one
        if current_len + len(seg) > chunk_size:
            chunks.append("".join(chunk_segs))
            chunk_segs = [seg]
            chunk_embs = [emb]
            chunk_minsim = 1.0
            continue

        # Max-Min decision rule
        if len(chunk_segs) == 1:
            # First merge: use tau as the initial threshold.
            # max_sim == the only pairwise similarity in a 2-element set,
            # which is the correct MinSim after merging.
            if max_sim >= tau and max_sim >= min_similarity:
                chunk_segs.append(seg)
                chunk_embs.append(emb)
                chunk_minsim = max_sim
            else:
                chunks.append("".join(chunk_segs))
                chunk_segs = [seg]
                chunk_embs = [emb]
                chunk_minsim = 1.0
        else:
            if max_sim >= chunk_minsim and max_sim >= min_similarity:
                # Update MinSim: check if any new pair is weaker than current minimum
                for sim in pairwise_sims:
                    if sim < chunk_minsim:
                        chunk_minsim = sim
                chunk_segs.append(seg)
                chunk_embs.append(emb)
            elif len(chunk_segs) < min_segments and current_len + len(seg) <= chunk_size:
                #新增：凑不够最少段数时强制并入，防碎片化
                for sim in pairwise_sims:
                    if sim < chunk_minsim:
                        chunk_minsim = sim
                chunk_segs.append(seg)
                chunk_embs.append(emb)
            else:
                chunks.append("".join(chunk_segs))
                chunk_segs = [seg]
                chunk_embs = [emb]
                chunk_minsim = 1.0

    if chunk_segs:
        chunks.append("".join(chunk_segs))

    # Final pass: split any oversized chunks by character fallback
    result: list[str] = []
    for chunk_text in chunks:
        if len(chunk_text) <= chunk_size:
            result.append(chunk_text)
        else:
            for start in range(0, len(chunk_text), chunk_size):
                result.append(chunk_text[start : start + chunk_size])
    return result

def _tail_with_boundary(text: str, max_len: int) -> str:
    """从文本末尾截取最多 max_len 字符，对齐到句子/段落边界。"""
    if len(text) <= max_len:
        return text
    tail = text[-max_len:]
    # 尝试从第一个句子分隔符开始（避免截断半句）
    for sep in ["。", "！", "？", ". ", "! ", "? ", "\n\n", "\n"]:
        idx = tail.find(sep)
        if idx != -1:
            return tail[idx + len(sep):]
    return tail
def _apply_overlap_to_chunks(
    chunks: list[Document],
    chunk_size: int,
    chunk_overlap: int,
) -> list[Document]:
    """Apply text-level overlap between consecutive chunks."""
    result: list[Document] = []
    step = max(1, chunk_size - chunk_overlap)
    for chunk in chunks:
        text = chunk.page_content
        if len(text) <= chunk_size:
            result.append(chunk)
            continue
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            piece = text[start:end]
            if piece:
                result.append(Document(page_content=piece, metadata=dict(chunk.metadata)))
            if end >= len(text):
                break
            start += step
    return result

def _build_heading_tree(text: str) -> dict:
    """解析 Markdown 文本中的标题层级，构建文档树。

    返回根节点 dict，结构：
    {
        "level": 0,
        "title": "",
        "content": "第一个标题前的文本",
        "children": [子节点...]
    }
    每个子节点包含 level(1-6), title, content(含标题行及下属文本), children。
    """
    lines = text.splitlines(keepends=True)
    root = {"level": 0, "title": "", "content": "", "children": []}
    stack = [root]
    current_content_lines: list[str] = []

    for line in lines:
        heading_match = re.match(r"^(#{1,6})\s+(.*)", line)
        if heading_match:
            # 将之前累积的内容挂到当前栈顶节点
            if current_content_lines:
                stack[-1]["content"] += "".join(current_content_lines)
                current_content_lines = []
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            new_node = {
                "level": level,
                "title": title,
                "content": line,  # 标题行本身作为节点内容的开头
                "children": [],
            }
            # 弹出栈中层级 >= 当前层级的节点，找到父节点
            while len(stack) > 1 and stack[-1]["level"] >= level:
                stack.pop()
            stack[-1]["children"].append(new_node)
            stack.append(new_node)
        else:
            current_content_lines.append(line)

    # 剩余内容挂到最后一个栈顶节点
    if current_content_lines:
        stack[-1]["content"] += "".join(current_content_lines)

    return root


def split_by_tree(
    documents: list[Document],
    chunk_size: int = 12000,
) -> list[Document]:
    """树状切分：按 Markdown 标题层级构建文档树，以标题节点为最小单元打包。

    - 不在段落/标题节点中断开（最小不可拆单位是标题节点及其下属内容）。
    - 累积内容接近 chunk_size 时切分，允许最后一块少于阈值，
      若单个节点超阈值则单独成块（不拆分）。
    - 每个输出 chunk 前添加段落位置标记（面包屑路径）。

    标记格式示例：
        [文档位置: 文章标题 > 一级标题 > 二级标题]

    Args:
        documents: 输入的 Document 列表。
        chunk_size: 每个 chunk 的目标最大字符数，默认 12000。

    Returns:
        list[Document]: 带位置标记的树状切分结果。
    """
    results: list[Document] = []

    for doc in documents:
        text = doc.page_content
        if not text.strip():
            continue

        # 确定文章标题：优先 metadata，其次第一个 # 标题，最后兜底
        doc_title = (
            doc.metadata.get("title")
            or doc.metadata.get("source")
            or ""
        )
        if not doc_title:
            first_h1 = re.search(r"^#\s+(.*)", text, re.MULTILINE)
            if first_h1:
                doc_title = first_h1.group(1).strip()
            else:
                doc_title = "未命名文档"

        # 构建文档树
        tree = _build_heading_tree(text)

        # 收集待输出的块：(路径列表, 内容文本)
        chunks_to_flush: list[tuple[list[str], str]] = []

        current_path: list[str] = [doc_title]
        current_parts: list[str] = []
        current_len: int = 0

        def _flush() -> None:
            """将当前累积的内容作为一个 chunk 记录。"""
            nonlocal current_parts, current_len
            if current_parts:
                combined = "".join(current_parts)
                if combined.strip():
                    chunks_to_flush.append((list(current_path), combined))
                current_parts = []
                current_len = 0

        def _traverse(node: dict, path: list[str]) -> None:
            nonlocal current_len
            node_text = node["content"]

            # 如果加入当前节点会超阈值，且已有累积，先 flush
            if current_len + len(node_text) > chunk_size and current_parts:
                _flush()

            # 加入当前节点内容
            current_parts.append(node_text)
            current_len += len(node_text)

            # 遍历子节点
            for child in node["children"]:
                child_path = path + [child["title"]]
                # 如果子节点加入会超且当前有内容，先 flush 再进入子树
                if current_len + len(child["content"]) > chunk_size and current_parts:
                    _flush()
                _traverse(child, child_path)

        # 处理根节点前置内容（第一个标题之前的文本）
        if tree["content"].strip():
            current_parts.append(tree["content"])
            current_len += len(tree["content"])

        # 遍历所有顶级标题节点
        for child in tree["children"]:
            if current_len + len(child["content"]) > chunk_size and current_parts:
                _flush()
            _traverse(child, [doc_title, child["title"]])

        # 剩余内容 flush
        _flush()

        # 生成带位置标记的 Document
        for path, content in chunks_to_flush:
            if len(path) > 1:
                marker = f"[文档位置: {' > '.join(path)}]"
            else:
                marker = f"[文档位置: {path[0]}]"
            marked_content = f"{marker}\n\n{content}"
            results.append(
                Document(
                    page_content=marked_content,
                    metadata=dict(doc.metadata),
                )
            )

    return results

# ======================== 以下为新增的 main 测试函数 ========================

def _generate_test_document() -> str:
    """生成一篇约3000字的交通领域测试文档（Markdown格式）。"""
    return """# 城市智慧交通系统建设与发展白皮书

## 摘要

随着城市化进程的不断推进，城市交通拥堵问题日益严峻。本文从智慧交通系统的架构设计、关键技术应用、数据平台建设以及未来发展趋势等维度，全面探讨城市交通治理的现代化路径。通过引入人工智能、大数据分析、物联网感知等前沿技术，构建高效、绿色、安全的城市交通生态体系。

## 一、城市交通发展现状

### 1.1 机动车保有量持续增长

近年来，我国城市机动车保有量保持年均8%以上的增速。以北京市为例，截至2023年底，全市机动车保有量已突破750万辆，其中私人轿车占比超过65%。机动车数量的快速增长直接导致早晚高峰时段主干道平均车速降至每小时18公里以下，部分核心商圈周边道路拥堵指数长期维持在2.5以上（严重拥堵级别）。

### 1.2 公共交通分担率有待提升

尽管各地政府持续加大轨道交通和公交线路投入，但公共交通整体分担率仍不理想。一线城市地铁网络虽已初具规模，但"最后一公里"接驳问题尚未完全解决。公交专用道利用率参差不齐，部分路段公交车辆运行速度甚至低于社会车辆。非机动车道被占用、步行空间品质不高等问题也制约了绿色出行比例的进一步提升。

### 1.3 停车资源供需矛盾突出

城市核心区停车位缺口普遍在30%以上。以商业中心为例，高峰时段一位难求现象普遍存在。同时，停车资源信息不对称导致大量车位闲置与部分区域过度集中停车并存。智能停车引导系统的覆盖率不足20%，远不能满足市民实时查询和预约停车的需求。

## 二、智慧交通关键技术

### 2.1 交通大数据平台

交通大数据平台是智慧交通系统的核心底座。通过整合卡口过车数据、GPS浮动车数据、地铁闸机数据、手机信令数据等多源异构数据，构建城市级交通运行监测与研判平台。平台日均处理数据量可达TB级别，支持实时路况发布、拥堵溯源分析、交通事件自动检测等功能。

关键技术包括：流式计算引擎（如Flink、Kafka）实现毫秒级数据接入与处理；分布式存储（如HBase、Elasticsearch）支撑海量轨迹数据的高效检索；图计算技术用于路网拓扑分析与最短路径动态规划。

### 2.2 智能信号控制系统

传统固定配时信号控制已无法适应动态变化的交通流。自适应信号控制系统（如SCATS、SCOOT的升级版本）通过地磁检测器、视频AI分析等手段实时感知路口各方向车流量，动态调整绿灯时长和相位差。

最新的AI信号优化方案引入深度强化学习算法，将每个路口视为一个智能体，通过多智能体协同学习实现区域路网信号配时的全局最优。试点数据显示，AI信号优化可使路口平均排队长度缩短22%，通行效率提升15%以上。

### 2.3 车路协同与自动驾驶

车路协同（V2X）技术通过"车-车-路-云"的全方位连接，弥补单车智能在感知盲区、超视距等方面的不足。路侧单元（RSU）部署激光雷达和高清摄像头，将感知结果通过5G网络实时广播给周边车辆。自动驾驶车辆在接收到路侧感知信息后，可提前做出减速、变道等决策。

目前，国内已有超过20个城市开展了车路协同示范项目建设。北京亦庄、上海嘉定、长沙湘江新区等区域已实现特定路段L4级自动驾驶公交和Robotaxi的常态化运营。

### 2.4 交通数字孪生

数字孪生技术通过构建与物理世界1:1映射的虚拟交通系统，支持交通方案的在线仿真与预评估。孪生模型集成路网几何数据、实时交通流数据、气象数据、事件数据等多维信息，可模拟不同管控策略下的交通运行状态。

应用场景包括：新开通道路的信号配时预优化、大型活动交通组织方案仿真评估、交通事故应急响应推演等。某特大城市在亚运会期间利用数字孪生平台对开闭幕式散场交通进行预演，提前发现3处瓶颈节点并优化疏导方案，实际散场时间较预期缩短40%。

## 三、数据治理与隐私保护

### 3.1 多源数据融合挑战

交通数据来源广泛、格式多样、标准不一。公安交管部门的卡口数据、交通运输部门的公交GPS数据、互联网企业的导航数据各自独立存储，缺乏统一的数据交换标准和共享机制。数据孤岛现象严重制约了跨部门协同分析能力的发挥。

### 3.2 个人隐私保护

交通轨迹数据具有高度敏感性，能够精准还原个人的出行规律、常驻地点、社交关系等隐私信息。在数据采集、传输、存储、使用全生命周期中，必须落实脱敏处理、访问控制、审计追溯等安全措施。差分隐私、联邦学习等隐私计算技术正在逐步应用于交通数据开放共享场景。

## 四、典型应用案例

### 4.1 某超大城市交通大脑项目

该项目构建了覆盖全市域的交通感知、分析、决策、控制闭环系统。接入2.8万路视频监控、1.5万个地磁检测器、5000余套电子警察设备。系统上线一年后，全市工作日高峰拥堵指数下降11.3%，平均车速提升9.7%，交通事故发现时间从平均5分钟缩短至30秒以内。

### 4.2 智慧高速公路建设

以某省智慧高速示范路段为例，全长120公里，全线部署毫米波雷达、气象站、可变信息标志等设备。实现恶劣天气自动预警、异常停车自动检测、分车道动态限速等功能。运营数据显示，示范路段事故率同比下降35%，通行效率提升18%。

## 五、未来发展趋势

### 5.1 MaaS出行即服务

MaaS（Mobility as a Service）理念将多种交通方式整合到统一平台，用户只需输入起终点，系统自动规划包含地铁、公交、共享单车、网约车等组合的最优出行方案，并提供一站式支付服务。欧洲赫尔辛基的Whim应用和国内高德、百度等平台的"门到门"规划功能已初具MaaS雏形。

### 5.2 低空交通与城市空中出行

随着eVTOL（电动垂直起降飞行器）技术的成熟，城市空中出行（UAM）正从概念走向现实。预计2030年前后，部分城市将开通空中出租车航线，用于商务通勤和紧急医疗转运。低空空域管理、起降场规划、空中交通管制等配套体系正在加速布局。

### 5.3 交通与能源融合

新能源汽车的普及使交通系统从单纯的能源消耗者转变为移动储能单元。车网互动（V2G）技术允许电动汽车在电网负荷低谷时充电、高峰时向电网放电，参与电力调峰。交通与能源系统的深度融合将推动城市向"零碳交通"目标迈进。

## 结语

城市智慧交通建设是一项复杂的系统工程，需要政府、企业、科研机构多方协同。技术驱动固然重要，但体制机制创新、标准规范完善、公众意识提升同样不可或缺。唯有统筹规划、分步实施、持续迭代，才能构建真正便民、高效、可持续的城市交通未来。
"""


def main():
    """测试入口：用交通领域文档逐一验证所有切片方法。"""
    import sys
    import os

    print("=" * 80)
    print("  文档切片方法综合测试报告")
    print("  chunk_size = 500")
    print("=" * 80)

    # 准备测试文档
    test_text = _generate_test_document()
    print(f"\n📄 测试文档信息:")
    print(f"   字符数: {len(test_text)}")
    print(f"   行数: {len(test_text.splitlines())}")
    heading_pattern = r'^#{1,6}\s'
    print(f"   标题数: {len(re.findall(heading_pattern, test_text, re.MULTILINE))}")

    doc = Document(
        page_content=test_text,
        metadata={"title": "城市智慧交通系统建设与发展白皮书", "source": "test_traffic_doc.md"},
    )
    documents = [doc]

    # ---------- 1. split_by_characters ----------
    print(f"\n{'='*80}")
    print("  方法一: split_by_characters (无 overlap)")
    print(f"{'='*80}")
    chunks1 = split_by_characters(documents, chunk_size=500)
    for i, c in enumerate(chunks1):
        print(f"  Chunk {i+1}: {len(c.page_content)} 字符 | 前50字: {c.page_content[:50]}...")
    print(f"  → 共 {len(chunks1)} 块, 平均 {sum(len(c.page_content) for c in chunks1)//max(len(chunks1),1)} 字符")

    print(f"\n  方法一-b: split_by_characters (overlap=50)")
    chunks1b = split_by_characters(documents, chunk_size=500, chunk_overlap=50)
    for i, c in enumerate(chunks1b):
        print(f"  Chunk {i+1}: {len(c.page_content)} 字符 | 前50字: {c.page_content[:50]}...")
    print(f"  → 共 {len(chunks1b)} 块")

    # ---------- 2. split_recursively ----------
    print(f"\n{'='*80}")
    print("  方法二: split_recursively (无 overlap)")
    print(f"{'='*80}")
    chunks2 = split_recursively(documents, chunk_size=500)
    for i, c in enumerate(chunks2):
        print(f"  Chunk {i+1}: {len(c.page_content)} 字符 | 前50字: {c.page_content[:50]}...")
    print(f"  → 共 {len(chunks2)} 块, 平均 {sum(len(c.page_content) for c in chunks2)//max(len(chunks2),1)} 字符")

    print(f"\n  方法二-b: split_recursively (overlap=50)")
    chunks2b = split_recursively(documents, chunk_size=500, chunk_overlap=50)
    for i, c in enumerate(chunks2b):
        print(f"  Chunk {i+1}: {len(c.page_content)} 字符 | 前50字: {c.page_content[:50]}...")
    print(f"  → 共 {len(chunks2b)} 块")

    # ---------- 3. split_by_markdown ----------
    print(f"\n{'='*80}")
    print("  方法三: split_by_markdown (无 overlap)")
    print(f"{'='*80}")
    chunks3 = split_by_markdown(documents, chunk_size=500)
    for i, c in enumerate(chunks3):
        print(f"  Chunk {i+1}: {len(c.page_content)} 字符 | 前50字: {c.page_content[:50]}...")
    print(f"  → 共 {len(chunks3)} 块, 平均 {sum(len(c.page_content) for c in chunks3)//max(len(chunks3),1)} 字符")

    print(f"\n  方法三-b: split_by_markdown (overlap=50)")
    chunks3b = split_by_markdown(documents, chunk_size=500, chunk_overlap=50)
    for i, c in enumerate(chunks3b):
        print(f"  Chunk {i+1}: {len(c.page_content)} 字符 | 前50字: {c.page_content[:50]}...")
    print(f"  → 共 {len(chunks3b)} 块")

    # ---------- 4. split_semantically (真实模型) ----------
    print(f"\n{'='*80}")
    print("  方法四: split_semantically (语义切分, bge-small-zh-v1.5)")
    print(f"{'='*80}")
    try:
        chunks4 = split_semantically(
            documents, 
            chunk_size=500,
            #model_path="/home/swang/project/models/bge-small-zh-v1.5",
        )
        for i, c in enumerate(chunks4):
            print(f"  Chunk {i+1}: {len(c.page_content)} 字符 | 前50字: {c.page_content[:50]}...")
        print(f"  → 共 {len(chunks4)} 块")
    except Exception as e:
        print(f"  ❌ 语义切分失败: {e}")
        chunks4 = []

    # ---------- 5. split_by_tree ----------
    print(f"\n{'='*80}")
    print("  方法五: split_by_tree (树状切分, chunk_size=500)")
    print(f"{'='*80}")
    chunks5 = split_by_tree(documents, chunk_size=500)
    for i, c in enumerate(chunks5):
        content = c.page_content
        # 提取标记
        marker_end = content.find("\n\n")
        if marker_end > 0:
            marker = content[:marker_end]
            rest = content[marker_end+2:]
        else:
            marker = "(无标记)"
            rest = content
        print(f"  Chunk {i+1}: {len(content)} 字符 | {marker}")
        print(f"    内容前60字: {rest[:60]}...")
    print(f"  → 共 {len(chunks5)} 块, 平均 {sum(len(c.page_content) for c in chunks5)//max(len(chunks5),1)} 字符")

    # ---------- 汇总 ----------
    print(f"\n{'='*80}")
    print("  汇总对比")
    print(f"{'='*80}")
    methods = [
        ("split_by_characters (无overlap)", chunks1),
        ("split_by_characters (overlap=50)", chunks1b),
        ("split_recursively (无overlap)", chunks2),
        ("split_recursively (overlap=50)", chunks2b),
        ("split_by_markdown (无overlap)", chunks3),
        ("split_by_markdown (overlap=50)", chunks3b),
        ("split_semantically (fake model)", chunks4),
        ("split_by_tree", chunks5),
    ]
    print(f"  {'方法':<35} {'块数':>6} {'平均长度':>10} {'最大块':>8} {'最小块':>8}")
    print(f"  {'-'*35} {'-'*6} {'-'*10} {'-'*8} {'-'*8}")
    for name, chunks in methods:
        if chunks:
            lengths = [len(c.page_content) for c in chunks]
            avg = sum(lengths) // len(lengths)
            mx = max(lengths)
            mn = min(lengths)
            print(f"  {name:<35} {len(chunks):>6} {avg:>10} {mx:>8} {mn:>8}")
        else:
            print(f"  {name:<35} {'0':>6}")

    print(f"\n{'='*80}")
    print("  测试完成!")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()
