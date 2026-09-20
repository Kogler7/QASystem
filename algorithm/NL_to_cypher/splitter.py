from __future__ import annotations

import json
import re

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownTextSplitter, RecursiveCharacterTextSplitter


# ======================== JSON 保护核心函数 ========================

def _extract_json_blocks(text: str) -> list[tuple[int, int, str]]:
    """提取文本中所有完整、合法的 JSON 块（对象 {...} 和数组 [...]）。

    使用括号匹配 + json.loads 双重验证，避免把代码中的花括号误判为 JSON。
    返回 list of (start_pos, end_pos, json_text)。
    """
    blocks: list[tuple[int, int, str]] = []
    i = 0
    while i < len(text):
        while i < len(text) and text[i] in ' \t\n\r':
            i += 1
        if i >= len(text):
            break

        ch = text[i]
        if ch not in ('{', '['):
            i += 1
            continue

        open_ch = ch
        close_ch = '}' if ch == '{' else ']'
        depth = 0
        in_string = False
        escape = False
        j = i

        while j < len(text):
            c = text[j]
            if escape:
                escape = False
            elif c == '\\' and in_string:
                escape = True
            elif c == '"':
                in_string = not in_string
            elif not in_string:
                if c == open_ch:
                    depth += 1
                elif c == close_ch:
                    depth -= 1
                    if depth == 0:
                        candidate = text[i:j + 1]
                        try:
                            json.loads(candidate)
                            blocks.append((i, j + 1, candidate))
                            i = j + 1
                            break
                        except json.JSONDecodeError:
                            i += 1
                            break
            j += 1
        else:
            i += 1

    return blocks


def _split_text_with_json_protection(text: str) -> list[tuple[str, bool]]:
    """将文本按 JSON 块边界拆分为 (片段, is_json) 列表。

    JSON 块作为独立片段且 is_json=True，非 JSON 部分 is_json=False。
    """
    json_blocks = _extract_json_blocks(text)
    if not json_blocks:
        return [(text, False)]

    result: list[tuple[str, bool]] = []
    last_end = 0
    for start, end, json_text in json_blocks:
        if start > last_end:
            non_json = text[last_end:start]
            if non_json and non_json.strip():
                result.append((non_json, False))
        result.append((json_text, True))
        last_end = end

    if last_end < len(text):
        remaining = text[last_end:]
        if remaining and remaining.strip():
            result.append((remaining, False))

    return result if result else [(text, False)]


# ======================== 工具函数 ========================

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
    """合并过短的 chunk 到相邻 chunk 中。"""
    if not chunks:
        return chunks

    min_len = int(chunk_size * min_fill_ratio)
    result: list[str] = []

    for chunk in chunks:
        if len(chunk) >= min_len:
            result.append(chunk)
        else:
            if result and len(result[-1]) + len(chunk) <= chunk_size:
                result[-1] += chunk
            else:
                result.append(chunk)

    return result


def _split_into_segments(text: str, separators: list[str] | None = None) -> list[str]:
    """Split text into atomic segments at sentence / paragraph boundaries."""
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
    """Split text at Markdown header boundaries for structure-aware chunking."""
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
    """Merge tiny segments into neighboring content segments."""
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
    """Detect semantic breakpoints between consecutive segments."""
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
    """Group segments into chunks using the Max-Min semantic chunking algorithm."""
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

        if i in breakpoints:
            chunks.append("".join(chunk_segs))
            chunk_segs = [seg]
            chunk_embs = [emb]
            chunk_minsim = 1.0
            continue

        pairwise_sims = [_cosine_similarity(emb, e) for e in chunk_embs]
        max_sim = max(pairwise_sims) if pairwise_sims else 0.0

        if current_len + len(seg) > chunk_size:
            chunks.append("".join(chunk_segs))
            chunk_segs = [seg]
            chunk_embs = [emb]
            chunk_minsim = 1.0
            continue

        if len(chunk_segs) == 1:
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
                for sim in pairwise_sims:
                    if sim < chunk_minsim:
                        chunk_minsim = sim
                chunk_segs.append(seg)
                chunk_embs.append(emb)
            elif len(chunk_segs) < min_segments and current_len + len(seg) <= chunk_size:
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

    result: list[str] = []
    for chunk_text in chunks:
        if len(chunk_text) <= chunk_size:
            result.append(chunk_text)
        else:
            for start in range(0, len(chunk_text), chunk_size):
                result.append(chunk_text[start: start + chunk_size])
    return result


def _tail_with_boundary(text: str, max_len: int) -> str:
    """从文本末尾截取最多 max_len 字符，对齐到句子/段落边界。"""
    if len(text) <= max_len:
        return text
    tail = text[-max_len:]
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
    """解析 Markdown 文本中的标题层级，构建文档树。"""
    lines = text.splitlines(keepends=True)
    root = {"level": 0, "title": "", "content": "", "children": []}
    stack = [root]
    current_content_lines: list[str] = []

    for line in lines:
        heading_match = re.match(r"^(#{1,6})\s+(.*)", line)
        if heading_match:
            if current_content_lines:
                stack[-1]["content"] += "".join(current_content_lines)
                current_content_lines = []
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            new_node = {
                "level": level,
                "title": title,
                "content": line,
                "children": [],
            }
            while len(stack) > 1 and stack[-1]["level"] >= level:
                stack.pop()
            stack[-1]["children"].append(new_node)
            stack.append(new_node)
        else:
            current_content_lines.append(line)

    if current_content_lines:
        stack[-1]["content"] += "".join(current_content_lines)

    return root


# ======================== 切片方法（全部集成 JSON 保护） ========================

def split_by_characters(
    documents: list[Document],
    chunk_size: int = 500,
    separators: list[str] | None = None,
    chunk_overlap: int = 0,
) -> list[Document]:
    """Split documents through every configured separator, then pack chunks.
    JSON 块受保护，不会从中间断开。"""
    active_separators = [separator for separator in (separators or ["\n"]) if separator]
    if not active_separators:
        active_separators = ["\n"]

    fragments: list[Document] = []
    for document in documents:
        text = document.page_content
        protected = _split_text_with_json_protection(text)

        for part_text, is_json in protected:
            if is_json:
                # JSON 块直接作为完整 fragment
                fragments.append(Document(
                    page_content=part_text,
                    metadata={**dict(document.metadata), "_is_json": True},
                ))
            else:
                # 非 JSON 走原有 separator 拆分
                texts = [part_text]
                for separator in active_separators:
                    next_texts: list[str] = []
                    for t in texts:
                        parts = t.split(separator)
                        next_texts.extend(
                            p + (separator if idx < len(parts) - 1 else "")
                            for idx, p in enumerate(parts)
                            if p or idx < len(parts) - 1
                        )
                    texts = next_texts
                for t in texts:
                    if t:
                        fragments.append(Document(
                            page_content=t,
                            metadata={**dict(document.metadata), "_is_json": False},
                        ))

    if chunk_overlap > 0:
        return _pack_with_overlap(fragments, chunk_size, chunk_overlap)

    # 打包：JSON 块不拆分
    chunks: list[Document] = []
    buf_texts: list[str] = []
    buf_len = 0

    for fragment in fragments:
        frag_text = fragment.page_content
        is_json = fragment.metadata.get("_is_json", False)
        frag_len = len(frag_text)

        if is_json and frag_len > chunk_size:
            if buf_texts:
                chunks.append(Document(page_content="".join(buf_texts), metadata=dict(fragment.metadata)))
                buf_texts = []
                buf_len = 0
            chunks.append(Document(page_content=frag_text, metadata=dict(fragment.metadata)))
            continue

        if buf_len + frag_len > chunk_size and buf_texts:
            chunks.append(Document(page_content="".join(buf_texts), metadata=dict(fragment.metadata)))
            buf_texts = [frag_text]
            buf_len = frag_len
        else:
            buf_texts.append(frag_text)
            buf_len += frag_len

    if buf_texts:
        chunks.append(Document(page_content="".join(buf_texts), metadata=dict(fragments[-1].metadata)))

    for c in chunks:
        c.metadata.pop("_is_json", None)

    return chunks


def _pack_with_overlap(
    fragments: list[Document],
    chunk_size: int,
    chunk_overlap: int,
) -> list[Document]:
    if not fragments:
        return []

    chunks: list[Document] = []
    buf_frags: list[str] = []
    buf_len = 0
    meta = {k: v for k, v in dict(fragments[0].metadata).items() if k != "_is_json"}

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
        is_json = frag.metadata.get("_is_json", False)
        if not content:
            continue

        if is_json and len(content) > chunk_size:
            flush(force=True)
            chunks.append(Document(
                page_content=content,
                metadata={k: v for k, v in dict(frag.metadata).items() if k != "_is_json"},
            ))
            continue

        if buf_len + len(content) > chunk_size and buf_frags:
            flush(force=True)
        buf_frags.append(content)
        buf_len += len(content)

    flush(force=True)

    if chunk_overlap <= 0 or len(chunks) < 2:
        return chunks

    overlapped: list[Document] = [chunks[0]]
    for k in range(1, len(chunks)):
        prev_text = overlapped[-1].page_content
        tail = _tail_with_boundary(prev_text, chunk_overlap)
        if tail and tail != prev_text:
            new_text = tail + chunks[k].page_content
            if len(new_text) > chunk_size + chunk_overlap:
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
    """递归切片 + JSON 保护。"""
    result: list[Document] = []

    for document in documents:
        text = document.page_content
        protected = _split_text_with_json_protection(text)

        for part_text, is_json in protected:
            if is_json:
                result.append(Document(page_content=part_text, metadata=dict(document.metadata)))
            else:
                sub_doc = Document(page_content=part_text, metadata=dict(document.metadata))
                result.extend(
                    RecursiveCharacterTextSplitter(
                        separators=separators or ["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""],
                        chunk_size=chunk_size,
                        chunk_overlap=chunk_overlap,
                    ).split_documents([sub_doc])
                )

    return result


def split_by_markdown(
    documents: list[Document],
    chunk_size: int = 500,
    chunk_overlap: int = 0,
) -> list[Document]:
    """Markdown 切片 + JSON 保护。"""
    result: list[Document] = []

    for document in documents:
        text = document.page_content
        protected = _split_text_with_json_protection(text)

        for part_text, is_json in protected:
            if is_json:
                result.append(Document(page_content=part_text, metadata=dict(document.metadata)))
            else:
                sub_doc = Document(page_content=part_text, metadata=dict(document.metadata))
                result.extend(
                    MarkdownTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
                    .split_documents([sub_doc])
                )

    return result


def split_semantically(
    documents: list[Document],
    chunk_size: int = 500,
    similarity: float = 0.6,
    chunk_overlap: int = 0,
    model_path: str | None = None,
    breakpoint_method: str = "percentile",
    min_segments_per_chunk: int = 2,
) -> list[Document]:
    """语义切片 + JSON 保护。"""
    import numpy as np

    try:
        from algorithm.NL_to_cypher.vector import _load_model, _model_path
        resolved_path = _model_path(model_path)
        model = _load_model(resolved_path)
        has_model = True
    except Exception:
        has_model = False
        model = None

    min_similarity = max(0.3, similarity - 0.15) if has_model else 0.0

    result: list[Document] = []
    for document in documents:
        text = document.page_content
        if not text.strip():
            continue

        protected = _split_text_with_json_protection(text)

        for part_text, is_json in protected:
            if is_json:
                result.append(Document(page_content=part_text, metadata=dict(document.metadata)))
                continue

            if not has_model or not part_text.strip():
                if len(part_text) <= chunk_size:
                    result.append(Document(page_content=part_text, metadata=dict(document.metadata)))
                else:
                    for start in range(0, len(part_text), chunk_size):
                        result.append(Document(
                            page_content=part_text[start:start + chunk_size],
                            metadata=dict(document.metadata),
                        ))
                continue

            sections = _split_by_markdown_headers(part_text)
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

            all_embeddings = model.encode(all_segments, convert_to_numpy=True)

            for start, end in section_spans:
                section_segs = all_segments[start:end]
                section_embs = all_embeddings[start:end]
                merged_segs, merged_embs = _merge_tiny_segments(section_segs, section_embs)
                if len(merged_segs) == 0:
                    continue
                bps = _detect_breakpoints(merged_segs, merged_embs, method=breakpoint_method)
                chunks = _group_segments_maxmin(
                    merged_segs, merged_embs, chunk_size,
                    tau=similarity, min_similarity=min_similarity,
                    breakpoints=bps, min_segments=min_segments_per_chunk,
                )
                chunks = _merge_short_chunks(chunks, chunk_size, min_fill_ratio=0.5)
                for chunk_text in chunks:
                    result.append(Document(page_content=chunk_text, metadata=dict(document.metadata)))

    if chunk_overlap > 0 and len(result) > 1:
        result = _apply_overlap_to_chunks(result, chunk_size, chunk_overlap)

    return result


def split_by_tree(
    documents: list[Document],
    chunk_size: int = 12000,
) -> list[Document]:
    """树状切片 + JSON 保护。"""
    results: list[Document] = []

    for doc in documents:
        text = doc.page_content
        if not text.strip():
            continue

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

        protected = _split_text_with_json_protection(text)

        chunks_to_flush: list[tuple[list[str], str]] = []
        current_path: list[str] = [doc_title]
        current_parts: list[str] = []
        current_len: int = 0

        def _flush() -> None:
            nonlocal current_parts, current_len
            if current_parts:
                combined = "".join(current_parts)
                if combined.strip():
                    chunks_to_flush.append((list(current_path), combined))
                current_parts = []
                current_len = 0

        for part_text, is_json in protected:
            if is_json:
                if current_parts:
                    _flush()
                marker = f"[文档位置: {' > '.join(current_path)}]"
                marked_content = f"{marker}\n\n{part_text}"
                results.append(Document(page_content=marked_content, metadata=dict(doc.metadata)))
                continue

            tree = _build_heading_tree(part_text)
            if tree["content"].strip():
                if current_len + len(tree["content"]) > chunk_size and current_parts:
                    _flush()
                current_parts.append(tree["content"])
                current_len += len(tree["content"])

            def _traverse(node: dict, path: list[str]) -> None:
                nonlocal current_len
                node_text = node["content"]
                if current_len + len(node_text) > chunk_size and current_parts:
                    _flush()
                current_parts.append(node_text)
                current_len += len(node_text)
                for child in node["children"]:
                    if current_len + len(child["content"]) > chunk_size and current_parts:
                        _flush()
                    _traverse(child, path + [child["title"]])

            for child in tree["children"]:
                if current_len + len(child["content"]) > chunk_size and current_parts:
                    _flush()
                _traverse(child, [doc_title, child["title"]])

        _flush()

        for path, content in chunks_to_flush:
            if len(path) > 1:
                marker = f"[文档位置: {' > '.join(path)}]"
            else:
                marker = f"[文档位置: {path[0]}]"
            marked_content = f"{marker}\n\n{content}"
            results.append(Document(page_content=marked_content, metadata=dict(doc.metadata)))

    return results


# ======================== 测试文档生成 ========================

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

传统固定配时信号控制已无法适应动态变化的交通流。自适应信号控制系统通过地磁检测器、视频AI分析等手段实时感知路口各方向车流量，动态调整绿灯时长和相位差。最新的AI信号优化方案引入深度强化学习算法，使路口平均排队长度缩短22%，通行效率提升15%以上。

## 三、数据治理与隐私保护

### 3.1 多源数据融合挑战

交通数据来源广泛、格式多样、标准不一。公安交管部门的卡口数据、交通运输部门的公交GPS数据、互联网企业的导航数据各自独立存储，缺乏统一的数据交换标准和共享机制。数据孤岛现象严重制约了跨部门协同分析能力的发挥。

### 3.2 个人隐私保护

交通轨迹数据具有高度敏感性，能够精准还原个人的出行规律、常驻地点、社交关系等隐私信息。差分隐私、联邦学习等隐私计算技术正在逐步应用于交通数据开放共享场景。

## 四、未来发展趋势

### 4.1 MaaS出行即服务

MaaS理念将多种交通方式整合到统一平台，用户只需输入起终点，系统自动规划包含地铁、公交、共享单车、网约车等组合的最优出行方案，并提供一站式支付服务。

### 4.2 交通与能源融合

新能源汽车的普及使交通系统从单纯的能源消耗者转变为移动储能单元。车网互动（V2G）技术允许电动汽车在电网负荷低谷时充电、高峰时向电网放电，参与电力调峰。

## 结语

城市智慧交通建设是一项复杂的系统工程，需要政府、企业、科研机构多方协同。技术驱动固然重要，但体制机制创新、标准规范完善、公众意识提升同样不可或缺。唯有统筹规划、分步实施、持续迭代，才能构建真正便民、高效、可持续的城市交通未来。
"""


def _generate_json_test_document() -> str:
    """生成包含 JSON 结构的测试文档，用于验证 JSON 保护功能。"""
    return """# API 接口文档与配置说明

## 概述

本文档包含交通数据平台的接口定义和配置示例。

## 用户信息接口

获取用户基本信息，请求示例：
{
"method": "GET",
"path": "/api/v1/users/info",
"headers": {
"Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xxx",
"Content-Type": "application/json"
},
"query_params": {
"user_id": "U12345678",
"fields": ["name", "email", "role", "department"]
},
"timeout_ms": 5000,
"retry_count": 3
}
上述接口返回的用户数据格式如下：
{
"code": 0,
"message": "success",
"data": {
"user_id": "U12345678",
"name": "张三",
"email": "zhangsan@traffic-platform.com",
"role": "admin",
"department": "交通规划部",
"permissions": ["read:traffic", "write:traffic", "admin:users"],
"created_at": "2023-06-15T08:30:00Z",
"last_login": "2024-01-20T14:22:00Z",
"preferences": {
"language": "zh-CN",
"timezone": "Asia/Shanghai",
"dashboard_layout": "compact"
}
},
"request_id": "req_abc_789"
}
## 交通信号配置
路口信号配时参数以 JSON 数组形式下发：
[
{
"intersection_id": "ITS-001",
"name": "中关村大街-知春路",
"phases": [
{"phase": 1, "direction": "南北直行", "green": 45, "yellow": 3, "red": 52},
{"phase": 2, "direction": "南北左转", "green": 25, "yellow": 3, "red": 72},
{"phase": 3, "direction": "东西直行", "green": 40, "yellow": 3, "red": 57},
{"phase": 4, "direction": "东西左转", "green": 20, "yellow": 3, "red": 77}
],
"cycle_length": 120,
"offset": 15,
"coordinate_system": "absolute",
"last_updated": "2024-03-01T06:00:00Z"
},
{
"intersection_id": "ITS-002",
"name": "学院路-清华东路",
"phases": [
{"phase": 1, "direction": "南北直行", "green": 50, "yellow": 3, "red": 47},
{"phase": 2, "direction": "南北左转", "green": 30, "yellow": 3, "red": 67},
{"phase": 3, "direction": "东西直行", "green": 35, "yellow": 3, "red": 62},
{"phase": 4, "direction": "东西左转", "green": 25, "yellow": 3, "red": 72}
],
"cycle_length": 120,
"offset": 30,
"coordinate_system": "absolute",
"last_updated": "2024-03-01T06:00:00Z"
}
]
## 总结

以上配置和接口定义构成了交通数据平台的核心交互规范。在实际部署中，所有 JSON 配置需通过配置中心统一管理，支持热更新和版本回滚。
"""


# ======================== main 测试入口 ========================

def main():
    """测试入口：逐一验证所有切片方法，包含普通文档和 JSON 文档。"""
    import sys
    import os

    print("=" * 80)
    print("  文档切片方法综合测试报告（含 JSON 保护验证）")
    print("  chunk_size = 500")
    print("=" * 80)

    # ==================== 测试一：普通交通文档 ====================
    print("\n" + "=" * 80)
    print("  测试一：普通交通文档（无 JSON）")
    print("=" * 80)

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

    # split_by_characters
    print(f"\n{'='*80}")
    print("  方法一: split_by_characters")
    print(f"{'='*80}")
    chunks1 = split_by_characters(documents, chunk_size=500)
    for i, c in enumerate(chunks1):
        print(f"  Chunk {i+1}: {len(c.page_content)} 字符 | 前50字: {c.page_content[:50]}...")
    print(f"  → 共 {len(chunks1)} 块")

    # split_recursively
    print(f"\n{'='*80}")
    print("  方法二: split_recursively")
    print(f"{'='*80}")
    chunks2 = split_recursively(documents, chunk_size=500)
    for i, c in enumerate(chunks2):
        print(f"  Chunk {i+1}: {len(c.page_content)} 字符 | 前50字: {c.page_content[:50]}...")
    print(f"  → 共 {len(chunks2)} 块")

    # split_by_markdown
    print(f"\n{'='*80}")
    print("  方法三: split_by_markdown")
    print(f"{'='*80}")
    chunks3 = split_by_markdown(documents, chunk_size=500)
    for i, c in enumerate(chunks3):
        print(f"  Chunk {i+1}: {len(c.page_content)} 字符 | 前50字: {c.page_content[:50]}...")
    print(f"  → 共 {len(chunks3)} 块")

    # split_by_tree
    print(f"\n{'='*80}")
    print("  方法四: split_by_tree")
    print(f"{'='*80}")
    chunks4 = split_by_tree(documents, chunk_size=500)
    for i, c in enumerate(chunks4):
        content = c.page_content
        marker_end = content.find("\n\n")
        if marker_end > 0:
            marker = content[:marker_end]
            rest = content[marker_end+2:]
        else:
            marker = "(无标记)"
            rest = content
        print(f"  Chunk {i+1}: {len(content)} 字符 | {marker}")
        print(f"    内容前60字: {rest[:60]}...")
    print(f"  → 共 {len(chunks4)} 块")

    # ==================== 测试二：包含 JSON 的文档 ====================
    print("\n" + "=" * 80)
    print("  测试二：包含 JSON 结构的文档（验证 JSON 保护）")
    print("=" * 80)

    json_text = _generate_json_test_document()
    print(f"\n📄 JSON 测试文档信息:")
    print(f"   字符数: {len(json_text)}")
    print(f"   JSON 块数: {len(_extract_json_blocks(json_text))}")

    json_doc = Document(
        page_content=json_text,
        metadata={"title": "API 接口文档与配置说明", "source": "test_json_doc.md"},
    )
    json_documents = [json_doc]

    # split_by_characters (JSON)
    print(f"\n{'='*80}")
    print("  [JSON] split_by_characters")
    print(f"{'='*80}")
    jchunks1 = split_by_characters(json_documents, chunk_size=500)
    for i, c in enumerate(jchunks1):
        # 检查是否包含完整 JSON
        is_valid = _check_json_integrity(c.page_content)
        status = "✅" if is_valid else "❌ JSON被截断"
        print(f"  Chunk {i+1}: {len(c.page_content)} 字符 {status} | 前50字: {c.page_content[:50]}...")
    print(f"  → 共 {len(jchunks1)} 块")

    # split_recursively (JSON)
    print(f"\n{'='*80}")
    print("  [JSON] split_recursively")
    print(f"{'='*80}")
    jchunks2 = split_recursively(json_documents, chunk_size=500)
    for i, c in enumerate(jchunks2):
        is_valid = _check_json_integrity(c.page_content)
        status = "✅" if is_valid else "❌ JSON被截断"
        print(f"  Chunk {i+1}: {len(c.page_content)} 字符 {status} | 前50字: {c.page_content[:50]}...")
    print(f"  → 共 {len(jchunks2)} 块")

    # split_by_markdown (JSON)
    print(f"\n{'='*80}")
    print("  [JSON] split_by_markdown")
    print(f"{'='*80}")
    jchunks3 = split_by_markdown(json_documents, chunk_size=500)
    for i, c in enumerate(jchunks3):
        is_valid = _check_json_integrity(c.page_content)
        status = "✅" if is_valid else "❌ JSON被截断"
        print(f"  Chunk {i+1}: {len(c.page_content)} 字符 {status} | 前50字: {c.page_content[:50]}...")
    print(f"  → 共 {len(jchunks3)} 块")

    # split_by_tree (JSON)
    print(f"\n{'='*80}")
    print("  [JSON] split_by_tree")
    print(f"{'='*80}")
    jchunks4 = split_by_tree(json_documents, chunk_size=500)
    for i, c in enumerate(jchunks4):
        content = c.page_content
        is_valid = _check_json_integrity(content)
        status = "✅" if is_valid else "❌ JSON被截断"
        marker_end = content.find("\n\n")
        if marker_end > 0:
            marker = content[:marker_end]
            rest = content[marker_end+2:]
        else:
            marker = "(无标记)"
            rest = content
        print(f"  Chunk {i+1}: {len(content)} 字符 {status} | {marker}")
        print(f"    内容前60字: {rest[:60]}...")
    print(f"  → 共 {len(jchunks4)} 块")

    # ==================== 汇总 ====================
    print(f"\n{'='*80}")
    print("  汇总对比")
    print(f"{'='*80}")
    methods = [
        ("split_by_characters (普通)", chunks1),
        ("split_recursively (普通)", chunks2),
        ("split_by_markdown (普通)", chunks3),
        ("split_by_tree (普通)", chunks4),
        ("split_by_characters (JSON)", jchunks1),
        ("split_recursively (JSON)", jchunks2),
        ("split_by_markdown (JSON)", jchunks3),
        ("split_by_tree (JSON)", jchunks4),
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

    # JSON 完整性汇总
    print(f"\n{'='*80}")
    print("  JSON 完整性验证汇总")
    print(f"{'='*80}")
    all_json_chunks = [
        ("split_by_characters", jchunks1),
        ("split_recursively", jchunks2),
        ("split_by_markdown", jchunks3),
        ("split_by_tree", jchunks4),
    ]
    for method_name, chunks in all_json_chunks:
        violations = 0
        for c in chunks:
            if not _check_json_integrity(c.page_content):
                violations += 1
        if violations == 0:
            print(f"  ✅ {method_name:<30} 所有 chunk JSON 完整")
        else:
            print(f"  ❌ {method_name:<30} {violations} 个 chunk 存在 JSON 截断")

    print(f"\n{'='*80}")
    print("  测试完成!")
    print(f"{'='*80}")


def _check_json_integrity(text: str) -> bool:
    """检查文本中的 JSON 块是否完整（没有被截断）。
    
    返回 True 表示所有 JSON 块都完整，False 表示存在截断。
    """
    # 找到文本中所有 { 和 [ 开头的 JSON 块
    json_blocks = _extract_json_blocks(text)
    if not json_blocks:
        # 没有 JSON，自然不存在截断
        return True

    # 检查文本中是否有未闭合的 { 或 [
    stack = []
    in_string = False
    escape = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escape:
            escape = False
        elif ch == '\\' and in_string:
            escape = True
        elif ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch == '{' or ch == '[':
                stack.append(ch)
            elif ch == '}':
                if not stack or stack[-1] != '{':
                    return False
                stack.pop()
            elif ch == ']':
                if not stack or stack[-1] != '[':
                    return False
                stack.pop()
        i += 1

    # 如果栈非空，说明有未闭合的 JSON
    return len(stack) == 0


if __name__ == "__main__":
    main()