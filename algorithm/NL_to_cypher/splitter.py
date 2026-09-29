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
    chunk_size: int = 10000,
    min_fill_ratio: float = 0.5,
    merge_reserve: int = 2000,
) -> list[str]:
    """双向合并：把切片约束到 [min_absolute, max_absolute] 的软区间内。

    核心策略：放宽上限 + 合并余量，让碎片有空间并入。
    - 合并判定用 max_absolute - merge_reserve 作为软上限，
      保证并入后块内仍有余量吸收后续碎片
    - 超长块（> max_absolute）：优先按句子/段落边界切分，找不到边界则强制硬切
    - 碎片块（< min_absolute）：优先向前并入，其次向后并入
    - 正常块（区间内）：直接保留

    最终块长可能略高于 max_absolute 或略低于 min_absolute，但不会出现
    无意义的碎片与严重越界。
    """
    min_absolute: int = 500
    max_absolute: int = 10000
    if not chunks:
        return chunks

    min_len = min(int(chunk_size * min_fill_ratio), min_absolute)
    merge_limit = max_absolute + merge_reserve  # 合并上限允许 2000 字余量
    _SPLIT_POINTS = ["\n\n", "\n", "。", "！", "？", ". ", "! ", "? ", "; ", " "]

    def _split_long(text: str) -> list[str]:
        """按 chunk_size 宽度切分超长文本，优先对齐到句子/段落边界。

        关键：分隔符真实长度必须计入 end，否则多字符分隔符（如 ". "）
        会被吞掉，导致切分后文本错位或重复。
        """
        if len(text) <= chunk_size:
            return [text]
        pieces: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            if end < len(text):
                best = -1
                best_len = 0
                for sep in _SPLIT_POINTS:
                    if not sep:
                        continue
                    idx = text.rfind(sep, start, end)
                    if idx != -1 and idx > best:
                        best = idx
                        best_len = len(sep)
                if best != -1:
                    end = best + best_len  # 包含分隔符本身
                    if end <= start:  # 防御：单字符分隔符退化
                        end = best + 1
                if end <= start or end > len(text):  # 兜底：保证每次循环都前进
                    end = min(start + chunk_size, len(text))
            pieces.append(text[start:end])
            start = end
        return pieces

    result: list[str] = []
    for idx, chunk in enumerate(chunks):
        L = len(chunk)
        if L > max_absolute:
            pieces = _split_long(chunk)
            for p in pieces:
                if result and len(result[-1]) + len(p) <= merge_limit:
                    result[-1] += p
                else:
                    result.append(p)
            continue
        if L >= min_len:
            result.append(chunk)
            continue
        # 向前并入（保留 2000 字余量）
        if result and len(result[-1]) + L <= merge_limit:
            result[-1] += chunk
            continue
        # 头部碎片（无前块）：尝试向后并入
        # 注意：nxt 可能是超长块，需先用 _split_long 预览其首段长度
        if not result:
            nxt = chunks[idx + 1] if idx + 1 < len(chunks) else None
            if nxt is not None:
                nxt_head = _split_long(nxt)[0] if len(nxt) > max_absolute else nxt
                if len(nxt_head) + L <= merge_limit:
                    result.append("__MERGE_NEXT__" + chunk)
                    continue
        # 向后并入（保留 2000 字余量）
        nxt = chunks[idx + 1] if idx + 1 < len(chunks) else None
        if nxt is not None and len(nxt) + L <= merge_limit:
            result.append("__MERGE_NEXT__" + chunk)
            continue
        result.append(chunk)

    after_forward: list[str] = []
    i = 0
    while i < len(result):
        piece = result[i]
        if piece.startswith("__MERGE_NEXT__"):
            content = piece[len("__MERGE_NEXT__"):]
            if i + 1 < len(result) and len(result[i + 1]) + len(content) <= merge_limit:
                result[i + 1] = content + result[i + 1]
                i += 1
                continue
        after_forward.append(piece)
        i += 1

    cleaned: list[str] = []
    for piece in after_forward:
        if piece.startswith("__MERGE_NEXT__"):
            piece = piece[len("__MERGE_NEXT__"):]
        if len(piece) < min_len and cleaned and len(cleaned[-1]) + len(piece) <= merge_limit:
            cleaned[-1] += piece
        else:
            cleaned.append(piece)
    return cleaned


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
            doc_chunks: list[str] = []

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
                doc_chunks.extend(chunks)
            doc_chunks = _merge_short_chunks(
                doc_chunks, chunk_size, min_fill_ratio=0.5
            )
            for chunk_text in doc_chunks:
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


