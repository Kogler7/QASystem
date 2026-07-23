from __future__ import annotations

import re

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownTextSplitter, RecursiveCharacterTextSplitter


def _cosine_similarity(a, b) -> float:
    """Cosine similarity between two numpy vectors."""
    import numpy as np

    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
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
    """Pack fragments into overlapping chunks using a sliding window."""
    texts = [f.page_content for f in fragments]
    metadatas = [f.metadata for f in fragments]
    chunks: list[Document] = []
    text_buffer = ""
    meta = metadatas[0] if metadatas else {}

    for text in texts:
        text_buffer += text

    step = max(1, chunk_size - chunk_overlap)
    start = 0
    while start < len(text_buffer):
        end = min(start + chunk_size, len(text_buffer))
        chunk_text = text_buffer[start:end]
        if chunk_text:
            chunks.append(Document(page_content=chunk_text, metadata=dict(meta)))
        if end >= len(text_buffer):
            break
        start += step

    return chunks


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
            )

            for chunk_text in chunks:
                result.append(Document(page_content=chunk_text, metadata=dict(document.metadata)))

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
                current = ""
            current = seg
    if current:
        merged.append(current)

    return [s for s in merged if s.strip()]


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
