"""文档切片模块。

提供五种切片方法：
- split_by_characters    ：按配置分隔符逐级切分后打包
- split_recursively      ：自定义递归切分（语义边界优先，无碎片、无过长段）
- split_by_markdown      ：Markdown 结构切片
- split_semantically     ：语义切片（需 embedding 模型）
- split_by_tree          ：树状切片

所有方法都会先做 JSON 块识别（_split_text_with_json_protection）：对象与含对象的
数组受保护（标量数组如 [1] 文献角标不算），JSON 块连同 ```json 围栏独立成片、
不被切坏；其中 split_recursively 对超长 JSON 也做结构切分兜底。

核心约束（统一适用于 JSON 与纯文本）：
- 小于阈值（chunk_size * min_block_ratio）的片一律视为碎片，必须并入相邻片
- 超过长度上限（chunk_size + max_overflow）的片一律切分
- 合并与切分对 JSON 一视同仁：JSON 既可以被合并，也可以在超长时按结构硬切
"""
from __future__ import annotations

import json
import logging
import re
from algorithm.logging_setup import ensure_logging

from langchain_core.documents import Document

logger = ensure_logging(logging.getLogger(__name__))

# ======================== 常量 ========================

# 分隔符优先级：从高（粗粒度）到低（细粒度）。
# 递归切分先用靠前的分隔符，一旦某级切不动，再降级到下级。
DEFAULT_SEPARATORS: list[str] = [
    "\n\n", "\n",
    "。", "！", "？",
    ". ", "! ", "? ",
    "；", ";",
    "，", ",",
    " ",
]

# 兜底硬切时优先对齐的边界（按优先级尝试）。
_HARD_SPLIT_POINTS: list[str] = [
    "\n\n", "\n", "。", "！", "？", ". ", "! ", "? ", "; ", " ",
]

_HEADER_PATTERN = re.compile(r"^(#{1,6}\s)", re.MULTILINE)

# 句子切分时使用的分隔符。
_SENTENCE_SEPARATORS: list[str] = [
    "\n\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ",
]


# ======================== JSON 识别与结构切分 ========================

def _is_protectable_json(data: object) -> bool:
    """只有对象、或「含对象的数组」值得保护。

    标量数组（`[1]`、`["参考文献"]`）多是正文里的文献角标或列举，保护它们会把
    `[1] 标准号…` 撕成 `"[1]"` + 正文两片，确定性参考文献抽取也会因此失效。
    口径与结构化文本判定（structured_text._is_record_payload）保持一致。
    """
    if isinstance(data, dict):
        return True
    return isinstance(data, list) and any(isinstance(item, dict) for item in data)


def _extract_json_blocks(text: str) -> list[tuple[int, int, str]]:
    """提取文本中所有完整、合法的顶层 JSON 块（对象 {...} 和含对象的数组 [...]）。

    用括号匹配 + json.loads 双重验证，避免把正文里的花括号误判为 JSON。
    只识别顶层块，嵌套结构（如 {"a": [1,2,3]} 里的 [1,2,3]）不再拆出；
    标量数组不算 JSON 块。
    返回的是纯 JSON 文本（不含围栏标记）。
    """
    blocks: list[tuple[int, int, str]] = []
    n = len(text)
    i = 0

    while i < n:
        ch = text[i]
        if ch not in '{[':
            i += 1
            continue

        open_ch = ch
        close_ch = '}' if ch == '{' else ']'

        # 向前检查：是否为"独立"的 JSON 起点。
        if i > 0:
            prev_ch = text[i - 1]
            is_ascii_id_prev = (
                prev_ch in '_'
                or ('a' <= prev_ch <= 'z')
                or ('A' <= prev_ch <= 'Z')
                or ('0' <= prev_ch <= '9')
            )
            if is_ascii_id_prev:
                i += 1
                continue

        # 括号匹配找最外层闭合位置
        depth = 0
        in_string = False
        escape = False
        j = i
        matched_end = -1
        while j < n:
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
                        matched_end = j
                        break
            j += 1

        if matched_end == -1:
            i += 1
            continue

        candidate_text = text[i:matched_end + 1]
        try:
            data = json.loads(candidate_text)
        except json.JSONDecodeError:
            i = matched_end + 1
            continue
        if not _is_protectable_json(data):
            # 标量数组不保护：跳过开头字符继续向后扫描
            i += 1
            continue

        # 向后检查：闭合后必须是合理边界。
        after_pos = matched_end + 1
        if after_pos < n:
            after_ch = text[after_pos]
            is_ascii_id = (
                after_ch in '_"\'`'
                or ('a' <= after_ch <= 'z')
                or ('A' <= after_ch <= 'Z')
                or ('0' <= after_ch <= '9')
            )
            if is_ascii_id:
                i = after_pos
                continue

        blocks.append((i, matched_end + 1, candidate_text))
        i = after_pos

    return blocks


#: 围栏行：```json 开行 / ``` 闭行。围栏必须和 JSON 同属一个片段，否则会被切成
#: 孤立的 "```json" / "```" 小片（白占一次 LLM 调用，且没有任何信息量）。
_FENCE_OPENER = re.compile(r"`{3,}\s*(?:json)?", re.IGNORECASE)
_FENCE_CLOSER = re.compile(r"`{3,}\s*")


def _absorb_fence_lines(text: str, start: int, end: int) -> tuple[int, int]:
    """把紧贴 JSON 两侧的围栏行并入 (start, end)；裸 JSON 边界原样返回。"""
    head = start
    while head > 0 and text[head - 1] in " \t\r\n":
        head -= 1
    line_start = text.rfind("\n", 0, head) + 1
    opener = text[line_start:head]
    if opener and _FENCE_OPENER.fullmatch(opener):
        start = line_start

    tail = end
    while tail < len(text) and text[tail] in " \t\r\n":
        tail += 1
    line_end = text.find("\n", tail)
    if line_end == -1:
        line_end = len(text)
    closer = text[tail:line_end]
    if closer and _FENCE_CLOSER.fullmatch(closer):
        end = line_end

    return start, end


def _strip_fence_edges(text: str) -> tuple[str, str, str]:
    """剥离首尾的围栏行，返回 (核心文本, 开头围栏, 结尾围栏)；无围栏原样返回。"""
    lines = text.split("\n")
    opener = ""
    closer = ""
    if lines and _FENCE_OPENER.fullmatch(lines[0]):
        opener = lines[0] + "\n"
        lines = lines[1:]
    if lines and _FENCE_CLOSER.fullmatch(lines[-1]):
        closer = "\n" + lines[-1]
        lines = lines[:-1]
    if not opener and not closer:
        return text, "", ""
    return "\n".join(lines), opener, closer


def _split_text_with_json_protection(text: str) -> list[tuple[str, bool]]:
    """把文本按 JSON 块边界拆成 (片段, is_json) 列表。

    JSON 块连同紧贴的 ```json / ``` 围栏作为独立片段（is_json=True）；
    非 JSON 部分 is_json=False，且所有片段按序拼接后与原文逐字一致。
    """
    if not text:
        return []

    json_blocks = _extract_json_blocks(text)
    if not json_blocks:
        return [(text, False)]

    result: list[tuple[str, bool]] = []
    last_end = 0
    for start, end, _json_text in json_blocks:
        start, end = _absorb_fence_lines(text, start, end)
        if start > last_end:
            result.append((text[last_end:start], False))
        result.append((text[start:end], True))
        last_end = end
    if last_end < len(text):
        result.append((text[last_end:], False))

    return result if result else [(text, False)]


def _split_json_object(obj: dict, chunk_size: int) -> list[str]:
    """按顶层键值对把一个 JSON 对象切成多个合法 JSON 对象。"""
    min_piece = max(1, int(chunk_size * 0.3))
    pieces: list[str] = []
    current: dict = {}
    current_len = 2  # "{}"

    for key, value in obj.items():
        kv_text = json.dumps({key: value}, ensure_ascii=False)
        kv_len = len(kv_text)
        add_len = kv_len if not current else kv_len + 1

        if current and current_len + add_len > chunk_size:
            pieces.append(json.dumps(current, ensure_ascii=False))
            current = {key: value}
            current_len = kv_len
        else:
            current[key] = value
            current_len += add_len

    if current:
        current_str = json.dumps(current, ensure_ascii=False)
        # 如果只有一块，直接返回
        if not pieces:
            return [current_str]
        # 如果当前块太小，并入前一块（允许略微超长）
        if len(current_str) < min_piece:
            prev = json.loads(pieces[-1])
            # 合并到最后一块：把 current 的键值对追加到 prev
            for k, v in current.items():
                prev[k] = v
            pieces[-1] = json.dumps(prev, ensure_ascii=False)
        else:
            pieces.append(current_str)

    return pieces


def _split_json_array(arr: list, chunk_size: int) -> list[str]:
    """按顶层元素把一个 JSON 数组切成多个合法 JSON 数组。

    如果最后一块太小（< chunk_size*0.3），则将其并入前一块。
    """
    min_piece = max(1, int(chunk_size * 0.3))
    pieces: list[str] = []
    current: list = []
    current_len = 2  # "[]"

    for item in arr:
        item_text = json.dumps(item, ensure_ascii=False)
        add_len = len(item_text) + (1 if current else 0)

        if current and current_len + add_len > chunk_size:
            pieces.append(json.dumps(current, ensure_ascii=False))
            current = [item]
            current_len = 2 + len(item_text)
        else:
            current.append(item)
            current_len += add_len

    if current:
        current_str = json.dumps(current, ensure_ascii=False)
        if not pieces:
            return [current_str]
        if len(current_str) < min_piece:
            prev = json.loads(pieces[-1])
            prev.extend(current)
            pieces[-1] = json.dumps(prev, ensure_ascii=False)
        else:
            pieces.append(current_str)

    return pieces


def _split_json_by_structure(text: str, chunk_size: int) -> list[str] | None:
    """按顶层结构切分 JSON。返回 None 表示无法/不需切分。

    计算长度时使用完整的 JSON 序列化结果，确保切割后的每块都是合法 JSON。
    带 ```json 围栏的块先剥离围栏、切完再把围栏贴回首尾两块，
    围栏既不阻挡结构切分，也不会被中间块带走。
    """
    stripped = text.strip()
    if not stripped:
        return None

    core, opener, closer = _strip_fence_edges(stripped)
    if not core or core[0] not in '{[':
        return None
    try:
        obj = json.loads(core)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, (dict, list)):
        return None
    if len(stripped) <= chunk_size:
        return None

    if isinstance(obj, dict):
        pieces = _split_json_object(obj, chunk_size)
    else:
        pieces = _split_json_array(obj, chunk_size)

    # 验证每块都是合法 JSON 且非空
    valid_pieces = []
    for p in pieces:
        try:
            json.loads(p)
            if len(p) > 2:  # 排除 "{}" 或 "[]" 空壳
                valid_pieces.append(p)
        except json.JSONDecodeError:
            continue

    if len(valid_pieces) <= 1:
        return None

    if opener or closer:
        valid_pieces[0] = opener + valid_pieces[0]
        valid_pieces[-1] = valid_pieces[-1] + closer
    return valid_pieces


def _hard_split_json(
    text: str, chunk_size: int, max_len: int | None = None
) -> list[str]:
    """对无法按结构切分的超长 JSON 做硬切（允许破坏结构）。

    策略：直接按 max_len 固定宽度切分，得到多块。宁可破坏 JSON 结构，
    也不允许输出超长片。不做尾部合并，保证一定被切开。

    返回的块不一定是合法 JSON（这是结构切分失败时的兜底）。
    """
    if max_len is None:
        max_len = chunk_size + 2000

    # 未超上限：直接返回整块
    if len(text) <= max_len:
        return [text]

    # 直接按 max_len 固定宽度切分
    pieces = [text[i:i + max_len] for i in range(0, len(text), max_len)]
    return pieces


# ======================== 通用打包与硬切 ========================

def _hard_split(text: str, chunk_size: int) -> list[str]:
    """按固定宽度硬切。"""
    if len(text) <= chunk_size:
        return [text]
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]


def _hard_split_at_boundary(text: str, chunk_size: int) -> list[str]:
    """兜底硬切，优先对齐到句子/段落边界（多字符分隔符计入 end，避免被吞）。"""
    if len(text) <= chunk_size:
        return [text]
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            best, best_len = -1, 0
            for sep in _HARD_SPLIT_POINTS:
                if not sep:
                    continue
                idx = text.rfind(sep, start, end)
                if idx != -1 and idx > best:
                    best, best_len = idx, len(sep)
            if best != -1:
                new_end = best + best_len
                if new_end > start and new_end <= len(text):
                    end = new_end
        if end <= start:
            end = min(start + chunk_size, len(text))
        pieces.append(text[start:end])
        start = end
    return pieces


def _merge_no_leftover(
    fragments: list[tuple[str, bool]],
    chunk_size: int,
    min_block_ratio: float = 0.3,
) -> list[tuple[str, bool]]:
    """把带类型的片段打包，保证无碎片、无过长段。

    规则：
    - 同类型片段能装就装进同一缓冲块；
    - 类型切换（JSON ↔ 文本）或超长时无条件落盘，保护 JSON 结构不被破坏；
    - 遍历结束后，若最后一块偏短则直接并入前一块（允许略微超长），彻底消除碎片；
    - 若最后一块与前一块类型不同无法并入，则保留（类型隔离优先）。
    """
    fragments = [(t, j) for t, j in fragments if t]
    if not fragments:
        return []

    min_len = max(1, int(chunk_size * min_block_ratio))
    blocks: list[tuple[str, bool]] = []
    buf = fragments[0][0]
    buf_type = fragments[0][1]

    for text, is_json in fragments[1:]:
        if is_json != buf_type or len(buf) + len(text) > chunk_size:
            blocks.append((buf, buf_type))
            buf = text
            buf_type = is_json
        else:
            buf += text

    if len(buf) < min_len and blocks and blocks[-1][1] == buf_type:
        last_text, last_type = blocks[-1]
        blocks[-1] = (last_text + buf, last_type)
    else:
        blocks.append((buf, buf_type))

    # 最终兜底：扫一遍，把所有 < min_len 的碎片强制并入相邻块
    return _eliminate_leftovers(blocks, min_len)


def _eliminate_leftovers(
    blocks: list[tuple[str, bool]],
    min_len: int,
) -> list[tuple[str, bool]]:
    """扫一遍 blocks，把所有 < min_len 的碎片并入相邻块。

    优先级：并入前一块（同类型优先）→ 并入前一块（允许类型混合）
    → 若已是最后一块，则并入前一块。
    返回结果中保证不存在 < min_len 的块。
    """
    if len(blocks) <= 1:
        return blocks

    # 正向扫：把每个碎片并入前一块
    cleaned: list[tuple[str, bool]] = []
    for text, is_json in blocks:
        if not cleaned:
            cleaned.append((text, is_json))
            continue
        if len(text) < min_len:
            prev_text, prev_type = cleaned[-1]
            cleaned[-1] = (prev_text + text, prev_type)
        else:
            cleaned.append((text, is_json))

    # 若最后一块仍是碎片（前一块无法并入的情况），再扫一次尾部
    if len(cleaned) > 1 and len(cleaned[-1][0]) < min_len:
        last_text, last_type = cleaned[-1]
        prev_text, prev_type = cleaned[-2]
        cleaned[-2] = (prev_text + last_text, prev_type)
        cleaned.pop()

    return cleaned


def _merge_blocks_greedy(
    blocks: list[tuple[str, bool]],
    chunk_size: int,
) -> list[tuple[str, bool]]:
    """对同类型块做贪心打包，在不超过 chunk_size 的前提下尽量合并相邻块。

    类型不同（JSON ↔ 文本）时不合并，保证 JSON 块与文本块不相互污染。
    """
    if len(blocks) <= 1:
        return blocks

    merged: list[tuple[str, bool]] = []
    for text, is_json in blocks:
        if not merged:
            merged.append((text, is_json))
            continue
        prev_text, prev_is_json = merged[-1]
        if is_json == prev_is_json and len(prev_text) + len(text) <= chunk_size:
            merged[-1] = (prev_text + text, is_json)
        else:
            merged.append((text, is_json))
    return merged


def _cleanup_blocks(
    blocks: list,
    chunk_size: int,
    min_len: int,
    max_len: int,
) -> list[tuple[str, bool, bool]]:
    """清扫块列表：碎片必合并、超长必切分，对 JSON 与纯文本一视同仁。

    输入块为 (text, is_json, is_hard_split_json) 三元组。
    is_hard_split_json=True 仅表示"这块是某次 JSON 硬切的产物"，用于日志/标记，
    不再享有"不可分割原子块"的特权：硬切 JSON 块既可以被合并进相邻 JSON 块，
    也会在超长时被继续切分。

    规则：
    - 任何 < min_len 的块（无论 JSON 还是文本，无论天然短还是切出来的碎片）
      都算碎片，必须并入相邻块
    - 任何 > max_len 的块都必须切分，JSON 允许结构切分，结构切分失败时硬切
    - 优先并入同类型块（不超 chunk_size 就合），无同类型邻居则并入最近的
      文本块（允许略微超长）；碎片绝不并入 JSON 块，避免污染 JSON/围栏结构
    - 类型隔离只在"可合法并入"的前提下维持；若并入必然超长，才保留原块
    - 若碎片没有可并入的对象（仅剩自己，或邻居全是 JSON 块），则保留
    """
    # 归一化：补齐第三元素
    norm: list[tuple[str, bool, bool]] = []
    for item in blocks:
        if len(item) == 3:
            norm.append((item[0], item[1], item[2]))
        else:
            norm.append((item[0], item[1], False))

    if not norm:
        return norm

    # 注意：不再对"只有一块"做早退。单块也可能超长，必须走切分流程，
    # 否则一个超大的 JSON/文本块会被原样返回，违反"超长必切分"的约束。

    # ---- 第零遍：先把所有超长块切开 ----
    # 这是本函数最关键的一步：无论后面如何合并，都不能让超长片存活下来。
    # JSON 优先结构切分，结构切分失败时（或仍超长时）硬切，允许破坏 JSON 结构。
    pre_split: list[tuple[str, bool, bool]] = []
    for text, is_json, is_hard in norm:
        if len(text) <= max_len:
            pre_split.append((text, is_json, is_hard))
            continue

        if is_json:
            pieces = _split_json_by_structure(text, chunk_size)
            candidate: list[tuple[str, bool, bool]] = []
            if pieces and len(pieces) > 1:
                for p in pieces:
                    candidate.append((p, True, False))
            else:
                for h in _hard_split_json(text, chunk_size, max_len):
                    candidate.append((h, True, True))

            for p_text, p_json, p_hard in candidate:
                if len(p_text) > max_len:
                    for h in _hard_split(p_text, max_len):
                        pre_split.append((h, True, True))
                else:
                    pre_split.append((p_text, p_json, p_hard))
        else:
            for h in _hard_split_at_boundary(text, max_len):
                pre_split.append((h, False, False))
    norm = pre_split

    if len(norm) <= 1:
        return norm

    # ---- 第一遍：合并相邻同类型块（贪心打包），消除相邻的碎片 ----
    # JSON 块之间、文本块之间都可以合并；只有类型切换时不合并。
    # 这是新需求的核心：JSON 块也可以被合并。
    merged: list[tuple[str, bool, bool]] = []
    for text, is_json, is_hard in norm:
        if not merged:
            merged.append((text, is_json, is_hard))
            continue

        prev_text, prev_is_json, prev_is_hard = merged[-1]

        if is_json == prev_is_json:
            # 同类型：能装就装（JSON 合并 JSON，文本合并文本）
            new_text = prev_text + text
            if len(new_text) <= chunk_size:
                merged[-1] = (new_text, is_json, False)
            else:
                merged.append((text, is_json, is_hard))
        else:
            merged.append((text, is_json, is_hard))

    # ---- 第二遍：超长块切分（JSON 与文本一视同仁） ----
    # 先把所有超长块切开，避免后面的碎片合并逻辑遇到不可能存在的超长片。
    # 关键：结构切分只切顶层（一个超大顶层对象是切不动的），切不动或切完
    # 仍有块超长，必须用 _hard_split_json 兜底，宁可破坏 JSON 结构也不留超长片。
    splitted: list[tuple[str, bool, bool]] = []
    for text, is_json, is_hard in merged:
        if len(text) <= max_len:
            splitted.append((text, is_json, is_hard))
            continue

        if is_json:
            # 先尝试结构切分（输出合法 JSON）；切不动则硬切（允许破坏结构）。
            pieces = _split_json_by_structure(text, chunk_size)
            candidate: list[tuple[str, bool, bool]] = []
            if pieces and len(pieces) > 1:
                for p in pieces:
                    candidate.append((p, True, False))
            else:
                for h in _hard_split_json(text, chunk_size, max_len):
                    candidate.append((h, True, True))

            # 兜底：结构切分只切顶层，超大顶层对象可能仍超长，必须再硬切
            final: list[tuple[str, bool, bool]] = []
            for p_text, p_json, p_hard in candidate:
                if len(p_text) > max_len:
                    for h in _hard_split(p_text, max_len):
                        final.append((h, True, True))
                else:
                    final.append((p_text, p_json, p_hard))
            splitted.extend(final)
        else:
            for h in _hard_split_at_boundary(text, max_len):
                splitted.append((h, False, False))
    merged = splitted

    # ---- 第三遍：反复消除所有 < min_len 的碎片 ----
    # 碎片可能是文本，也可能是切分后的 JSON 小片。两者都要合并。
    # 反复扫描直到没有可处理的碎片为止。
    while True:
        frag_idx = -1
        for i, (text, _, _) in enumerate(merged):
            if len(text) < min_len:
                frag_idx = i
                break

        if frag_idx == -1:
            break  # 没有可处理的碎片了

        text, is_json, is_hard_frag = merged[frag_idx]

        # 找左右最近的【同类型块】：优先保持类型隔离。
        def _is_same_type(j: int) -> bool:
            return merged[j][1] == is_json

        left_same = -1
        for j in range(frag_idx - 1, -1, -1):
            if _is_same_type(j):
                left_same = j
                break
        right_same = -1
        for j in range(frag_idx + 1, len(merged)):
            if _is_same_type(j):
                right_same = j
                break

        target = -1
        if left_same >= 0 and right_same >= 0:
            # 两边都同类型：选距离更近的；距离相同时优先左合并。
            if (frag_idx - left_same) <= (right_same - frag_idx):
                target = left_same
            else:
                target = right_same
        elif left_same >= 0:
            target = left_same
        elif right_same >= 0:
            target = right_same

        if target >= 0:
            # 合并到 target，保留被合并块的类型标记。
            combined = merged[target][0] + text
            merged[target] = (combined, merged[target][1], False)
            merged.pop(frag_idx)
            continue

        # 没有同类型邻居：尝试并入最近的文本块（允许略微超长）。
        # 碎片绝不并入 JSON 块——文本黏在 JSON/围栏上会破坏其结构
        # （如 '```' 闭行后直接续上正文，下游 structured_text 表格识别失效）。
        def _nearest_text(idx: int, step: int) -> int:
            j = idx + step
            while 0 <= j < len(merged):
                if not merged[j][1]:
                    return j
                j += step
            return -1

        left_any = _nearest_text(frag_idx, -1)
        right_any = _nearest_text(frag_idx, 1)

        if left_any >= 0 and right_any >= 0:
            if (frag_idx - left_any) <= (right_any - frag_idx):
                target = left_any
            else:
                target = right_any
        elif left_any >= 0:
            target = left_any
        elif right_any >= 0:
            target = right_any

        if target >= 0:
            merged[target] = (merged[target][0] + text, merged[target][1], False)
            merged.pop(frag_idx)
            continue

        # 实在没有可并入的对象（列表只剩这一块碎片，或邻居全是 JSON 块），保留。
        break

    return merged


def _tail_with_boundary(text: str, max_len: int) -> str:
    """从文本末尾截取最多 max_len 字符，对齐到句子/段落边界。"""
    if len(text) <= max_len:
        return text
    tail = text[-max_len:]
    if not tail:
        return ""

    for sep in ["。", "！", "？", "\n\n", "\n", ". ", "! ", "? ", "; ", " "]:
        if not sep:
            continue
        idx = tail.find(sep, 1)
        if idx != -1:
            start = idx + len(sep)
            return tail[start:] if start <= len(tail) else ""

    return tail


def _apply_overlap(
    blocks: list[tuple[str, bool]],
    chunk_size: int,
    chunk_overlap: int,
) -> list[tuple[str, bool]]:
    """在相邻同类型块之间施加 overlap（前块尾巴作为当前块前缀）。

    - 类型不同或前一块是 JSON 块：不加 overlap
    - overlap 对齐到句子边界，避免半句
    """
    if chunk_overlap <= 0 or len(blocks) < 2:
        return blocks

    result: list[tuple[str, bool]] = [blocks[0]]
    for text, is_json in blocks[1:]:
        prev_text, prev_is_json = result[-1]
        if is_json != prev_is_json or prev_is_json:
            result.append((text, is_json))
            continue

        tail = _tail_with_boundary(prev_text, chunk_overlap)
        if len(tail) > len(prev_text):
            tail = prev_text[-chunk_overlap:]
        new_text = tail + text
        max_allowed = chunk_size + chunk_overlap
        if len(new_text) > max_allowed:
            excess = len(new_text) - max_allowed
            tail = tail[excess:] if len(tail) > excess else ""
            new_text = tail + text
        result.append((new_text, is_json))
    return result


# ======================== 自定义递归切分（核心） ========================

def _recursive_split_pass(
    text: str,
    chunk_size: int,
    separators: list[str],
    sep_index: int = 0,
) -> list[str]:
    """按分隔符优先级递归切分，不打包、不加 overlap。

    separators 已从高到低排序。每级切分时只把该级切出来的、仍超长的段
    交给下一级分隔符处理——保证"段落优先于句子、句子优先于字符"的语义。
    """
    if not text or len(text) <= chunk_size:
        return [text] if text else []

    for idx in range(sep_index, len(separators)):
        sep = separators[idx]
        if not sep or sep not in text:
            continue

        parts = text.split(sep)
        segments = []
        for p in parts[:-1]:
            if p:
                segments.append(p + sep)
        if parts[-1]:
            segments.append(parts[-1])
        if len(segments) <= 1:
            continue

        result: list[str] = []
        for seg in segments:
            if len(seg) <= chunk_size:
                result.append(seg)
            else:
                result.extend(_recursive_split_pass(seg, chunk_size, separators, idx + 1))
        if result:
            return result

    # 所有分隔符都切不动（例如一个超长单词）：兜底硬切
    return _hard_split(text, chunk_size)


def _recursive_split_text(
    text: str,
    chunk_size: int,
    chunk_overlap: int = 0,
    separators: list[str] | None = None,
) -> list[tuple[str, bool]]:
    """对一段普通（非 JSON）文本做递归切分，返回 [(片段, is_json=False), ...]。

    流程：递归切分 → 打包合并（去碎片）→ 加 overlap。
    """
    if not text or not text.strip():
        return []

    seps = separators or DEFAULT_SEPARATORS
    seps = [s for s in seps if s] or DEFAULT_SEPARATORS

    sub_chunks = _recursive_split_pass(text, chunk_size, seps)
    packed = _merge_no_leftover([(c, False) for c in sub_chunks if c], chunk_size)
    if chunk_overlap > 0:
        packed = _apply_overlap(packed, chunk_size, chunk_overlap)
    return packed


# ======================== 语义切片相关工具 ========================

def _cosine_similarity(a, b) -> float:
    """两个向量间的余弦相似度。"""
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


def _split_into_segments(text: str, separators: list[str] | None = None) -> list[str]:
    """在句子/段落边界把文本切成原子片段。"""
    seps = separators if separators is not None else list(_SENTENCE_SEPARATORS)
    segments: list[str] = []
    buf = ""
    i = 0
    while i < len(text):
        matched = False
        for sep in seps:
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
        if seg in seps:
            current += seg
        else:
            if current:
                merged.append(current)
            current = seg
    if current:
        merged.append(current)

    return [m for m in merged if m and m.strip()] or ([text.strip()] if text.strip() else [])


def _split_by_markdown_headers(text: str) -> list[str]:
    """在 Markdown 标题边界切分。"""
    if not text.strip():
        return [text]

    matches = list(_HEADER_PATTERN.finditer(text))
    if not matches:
        return [text]

    sections: list[str] = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section = text[start:end].strip()
        if section:
            sections.append(section)

    if matches and matches[0].start() > 0:
        prefix = text[:matches[0].start()].strip()
        if prefix:
            sections.insert(0, prefix)

    if len(sections) >= 2:
        def _is_title_only(s: str) -> bool:
            lines = s.splitlines()
            if not lines:
                return False
            return bool(re.match(r"^#{1,6}\s+\S", lines[0])) and not "\n".join(lines[1:]).strip()

        if _is_title_only(sections[0]):
            sections[1] = sections[0] + "\n" + sections[1]
            sections = sections[1:]

    return sections if sections else [text]


def _merge_tiny_segments(
    segments: list[str],
    embeddings: "numpy.ndarray",
    min_segment_len: int = 20,
) -> tuple[list[str], "numpy.ndarray"]:
    """把过短片段并入相邻片段。"""
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
    """检测相邻片段间的语义断点。"""
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
    """Max-Min 语义分组算法。"""
    import numpy as np

    breakpoints = breakpoints or set()
    if not segments:
        return []

    chunks: list[str] = []
    chunk_segs: list[str] = [segments[0]]
    chunk_embs: list[np.ndarray] = [embeddings[0]]
    chunk_minsim = 1.0

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
        elif max_sim >= chunk_minsim and max_sim >= min_similarity:
            chunk_minsim = min(min(chunk_minsim, s) for s in pairwise_sims)
            chunk_segs.append(seg)
            chunk_embs.append(emb)
        elif len(chunk_segs) < min_segments and current_len + len(seg) <= chunk_size:
            chunk_minsim = min(min(chunk_minsim, s) for s in pairwise_sims)
            chunk_segs.append(seg)
            chunk_embs.append(emb)
        else:
            chunks.append("".join(chunk_segs))
            chunk_segs = [seg]
            chunk_embs = [emb]
            chunk_minsim = 1.0

    if chunk_segs:
        chunks.append("".join(chunk_segs))

    # 组内可能仍超长，最终兜底硬切
    result: list[str] = []
    for chunk_text in chunks:
        if len(chunk_text) <= chunk_size:
            result.append(chunk_text)
        else:
            result.extend(_hard_split(chunk_text, chunk_size))
    return result


def _build_heading_tree(text: str) -> dict:
    """解析 Markdown 标题层级，构建文档树。"""
    lines = text.splitlines(keepends=True)
    root = {"level": 0, "title": "", "content": "", "children": []}
    stack = [root]
    current_lines: list[str] = []

    for line in lines:
        heading_match = re.match(r"^(#{1,6})\s+(.*)", line)
        if heading_match:
            if current_lines:
                stack[-1]["content"] += "".join(current_lines)
                current_lines = []
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            new_node = {"level": level, "title": title, "content": line, "children": []}
            while len(stack) > 1 and stack[-1]["level"] >= level:
                stack.pop()
            stack[-1]["children"].append(new_node)
            stack.append(new_node)
        else:
            current_lines.append(line)

    if current_lines:
        stack[-1]["content"] += "".join(current_lines)
    return root


# ======================== 切片方法 ========================

def _finalize(
    fragments: list[tuple[str, bool]],
    base_meta: dict,
) -> list[Document]:
    """把 (text, is_json) 片段列表转成 Document 列表，并清除内部标记。"""
    result: list[Document] = []
    for text, is_json in fragments:
        if not text:
            continue
        meta = dict(base_meta)
        if is_json:
            meta["_is_json"] = True
        result.append(Document(page_content=text, metadata=meta))
    return result


def split_by_characters(
    documents: list[Document],
    chunk_size: int = 500,
    separators: list[str] | None = None,
    chunk_overlap: int = 0,
    *,
    protect_json: bool = True,
) -> list[Document]:
    """按配置的所有分隔符逐级切分后打包。JSON 块（连同围栏）独立成片。

    - plain_limit = chunk_size - chunk_overlap：overlap 预算内嵌在片宽里，
      施加 overlap 后成品片长仍 ≤ chunk_size
    - 分隔符拆分逐字守恒（分隔符跟随前一段、空段保留为分隔符片），
      拼接所有片后与原文一致；切不动且超宽的大块按宽度兜底硬切
    - 超大 JSON 块独占一片不参与打包，其余片段按类型隔离贪心装入 chunk_size
    - 每篇文档独立打包：不跨文档合并，metadata 逐篇保留，并记录 __source_range
    """
    if chunk_size <= 0 or not 0 <= chunk_overlap < chunk_size:
        raise ValueError("chunk_size 必须大于 0，且 0 <= chunk_overlap < chunk_size")
    plain_limit = chunk_size - chunk_overlap
    active_separators = [s for s in (separators or ["\n"]) if s] or ["\n"]

    result: list[Document] = []
    for document in documents:
        text = document.page_content
        if not text.strip():
            continue
        base_meta = dict(document.metadata)

        fragments: list[tuple[str, bool]] = []
        ranges: list[list[int]] = []
        source_cursor = 0
        parts = _split_text_with_json_protection(text) if protect_json else [(text, False)]
        for part_text, is_json in parts:
            part_start = text.find(part_text, source_cursor)
            if part_start < 0:
                part_start = source_cursor
            source_cursor = part_start + len(part_text)

            if is_json:
                fragments.append((part_text, True))
                ranges.append([part_start, source_cursor])
                continue

            segs = [part_text]
            for separator in active_separators:
                next_segs: list[str] = []
                for t in segs:
                    if len(t) <= plain_limit:
                        next_segs.append(t)
                        continue
                    parts = t.split(separator)
                    # 分隔符逐字守恒：非空段带上后缀分隔符；空段仅在非末尾时
                    # 保留为独立的分隔符片，保证所有片拼接后与原段一致
                    next_segs.extend(
                        p + (separator if idx < len(parts) - 1 else "")
                        for idx, p in enumerate(parts)
                        if p or idx < len(parts) - 1
                    )
                segs = next_segs

            part_cursor = 0
            for seg in segs:
                if not seg:
                    continue
                seg_start = part_start + part_cursor
                part_cursor += len(seg)
                if len(seg) <= plain_limit:
                    fragments.append((seg, False))
                    ranges.append([seg_start, seg_start + len(seg)])
                else:
                    for head_start in range(0, len(seg), plain_limit):
                        head = seg[head_start:head_start + plain_limit]
                        fragments.append((head, False))
                        ranges.append([seg_start + head_start, seg_start + head_start + len(head)])

        # 打包：同类型片段贪心装入 chunk_size，类型切换落盘；超大 JSON 独占一片。
        # 不强制消除极小片——它们无法在不超宽的前提下并入邻块时允许独立成块，
        # 这是 chunk_document_items 逐字定位与拼接还原的硬前提。
        blocks: list[tuple[str, bool]] = []
        block_ranges: list[list[int]] = []
        buf_text = ""
        buf_type = False
        buf_range: list[int] | None = None
        buf_len = 0

        def _flush_buf() -> None:
            nonlocal buf_text, buf_range, buf_len
            if not buf_text:
                return
            blocks.append((buf_text, buf_type))
            block_ranges.append(list(buf_range) if buf_range is not None else [0, buf_len])
            buf_text, buf_range, buf_len = "", None, 0

        for (frag_text, frag_type), frag_range in zip(fragments, ranges):
            if frag_type and len(frag_text) > chunk_size:
                _flush_buf()
                blocks.append((frag_text, True))
                block_ranges.append(list(frag_range))
                continue
            if buf_text and (frag_type != buf_type or buf_len + len(frag_text) > chunk_size):
                _flush_buf()
            if buf_text:
                buf_text += frag_text
                buf_len += len(frag_text)
                buf_range[1] = frag_range[1]
            else:
                buf_text, buf_type, buf_range, buf_len = frag_text, frag_type, list(frag_range), len(frag_text)
        _flush_buf()

        # 施加 overlap：前块尾巴（对齐句子边界）作为当前块前缀，成品 ≤ chunk_size。
        # chunk_document_items 依赖片文本是原文连续子串来定位偏移，因此边界对齐
        # 的尾巴若跳过了前块尾部的空白字符（不再是连续后缀），宁可放弃 overlap。
        if chunk_overlap > 0:
            overlapped: list[tuple[str, bool]] = []
            overlapped_ranges: list[list[int]] = []
            for idx, (block_text, block_type) in enumerate(blocks):
                block_range = block_ranges[idx]
                if idx == 0:
                    overlapped.append((block_text, block_type))
                    overlapped_ranges.append(list(block_range))
                    continue
                prev_text, prev_type = overlapped[-1]
                prev_range = overlapped_ranges[-1]
                if block_type != prev_type or prev_type:
                    overlapped.append((block_text, block_type))
                    overlapped_ranges.append(list(block_range))
                    continue
                tail = _tail_with_boundary(prev_text, chunk_overlap)
                if (
                    tail
                    and block_range[0] != prev_range[1]
                    and len(prev_text) == prev_range[1] - prev_range[0]
                ):
                    tail = ""
                if tail and not prev_text.endswith(tail):
                    tail = ""
                if tail and tail != prev_text:
                    new_text = tail + block_text
                    if len(new_text) > chunk_size:
                        allowed = max(0, chunk_size - len(block_text))
                        tail = tail[-allowed:] if allowed > 0 else ""
                        new_text = tail + block_text
                    overlapped.append((new_text, block_type))
                    overlapped_ranges.append(
                        [max(prev_range[1] - len(tail), prev_range[0]), block_range[1]]
                    )
                else:
                    overlapped.append((block_text, block_type))
                    overlapped_ranges.append(list(block_range))
            blocks, block_ranges = overlapped, overlapped_ranges

        for (block_text, block_type), block_range in zip(blocks, block_ranges):
            if not block_text:
                continue
            meta = dict(base_meta)
            if block_type:
                meta["_is_json"] = True
            meta["__source_range"] = list(block_range)
            result.append(Document(page_content=block_text, metadata=meta))

    return result


def split_recursively(
    documents: list[Document],
    chunk_size: int = 500,
    separators: list[str] | None = None,
    chunk_overlap: int = 0,
    max_overflow: int | None = None,
    min_block_ratio: float = 0.3,
) -> list[Document]:
    """自定义递归切分，完全不依赖 langchain。

    - 目标块长 chunk_size（建议 5000-10000 字符）
    - 浮动范围：min_len = chunk_size*min_block_ratio, max_len = chunk_size+max_overflow
    - 优先在段落/句子/标点边界切分
    - 小于 min_len 的片一律视为碎片，JSON 与纯文本都必合并
    - 大于 max_len 的片一律切分，JSON 结构切分失败则硬切
    - JSON 与文本之间：优先不拼接，但若 JSON 是碎片且无同类型邻居，可并入文本
    - overlap 只在相邻文本块之间施加，JSON 块不参与拼接
    - 原文档 metadata 完整保留到每个输出块
    """
    if not documents:
        return []

    seps = separators if separators is not None else DEFAULT_SEPARATORS
    seps = [s for s in seps if s] or DEFAULT_SEPARATORS

    min_len = max(1, int(chunk_size * min_block_ratio))
    max_len = chunk_size + (max_overflow if max_overflow is not None else 2000)

    result: list[Document] = []

    for document in documents:
        text = document.page_content
        if not text.strip():
            continue
        base_meta = dict(document.metadata)

        # 先对整段文本做 JSON 识别，得到有序的 (片段, is_json) 列表
        protected = _split_text_with_json_protection(text)

        # 处理每个片段，直接构建有序的块列表
        # 关键：保持原文顺序，不跨类型合并
        ordered_blocks: list[tuple[str, bool]] = []

        for part_text, is_json in protected:
            if is_json:
                # JSON 块：超长则结构切分，切不动则硬切。
                # 小 JSON 片后续由 _cleanup_blocks 统一决定是否合并。
                if len(part_text) <= chunk_size:
                    ordered_blocks.append((part_text, True, False))
                else:
                    pieces = _split_json_by_structure(part_text, chunk_size)
                    if pieces and len(pieces) > 1:
                        for p in pieces:
                            ordered_blocks.append((p, True, False))
                    else:
                        for h in _hard_split_json(part_text, chunk_size, max_len):
                            ordered_blocks.append((h, True, True))
            else:
                # 非 JSON：递归切分
                rec = _recursive_split_text(part_text, chunk_size, 0, seps)
                for t, _ in rec:
                    if t:
                        ordered_blocks.append((t, False, False))

        # 统一清扫：碎片必合并、超长必切分，对 JSON 与纯文本一视同仁。
        ordered_blocks = _cleanup_blocks(ordered_blocks, chunk_size, min_len, max_len)

        # 对相邻文本块施加 overlap（JSON 块与硬切 JSON 块不参与 overlap）
        if chunk_overlap > 0 and ordered_blocks:
            plain_idx: list[int] = []
            plain_blocks: list[tuple[str, bool]] = []
            for idx, item in enumerate(ordered_blocks):
                _, is_json, is_hard = item
                if is_json or is_hard:
                    continue
                plain_idx.append(idx)
                plain_blocks.append((item[0], is_json))
            if len(plain_blocks) >= 2:
                plain_blocks = _apply_overlap(plain_blocks, chunk_size, chunk_overlap)
            rebuilt: list = []
            pi = 0
            for idx, item in enumerate(ordered_blocks):
                text, is_json, is_hard = item
                if is_json or is_hard:
                    rebuilt.append((text, is_json, is_hard))
                else:
                    if pi < len(plain_blocks):
                        pt, pj = plain_blocks[pi]
                        rebuilt.append((pt, pj, False))
                        pi += 1
            ordered_blocks = rebuilt

        for item in ordered_blocks:
            if len(item) == 3:
                piece_text, is_json, _ = item
            else:
                piece_text, is_json = item
            if not piece_text:
                continue
            meta = dict(base_meta)
            if is_json:
                meta["_is_json"] = True
            result.append(Document(page_content=piece_text, metadata=meta))

    # ============ 切片结果日志 ============
    sizes = [len(doc.page_content) for doc in result]
    json_flags = [doc.metadata.get("_is_json", False) for doc in result]
    type_labels = ["JSON" if f else "TXT" for f in json_flags]
    detail = ", ".join(
        f"[{i}]{label}={sz}" for i, (label, sz) in enumerate(zip(type_labels, sizes))
    )
    logger.info(
        "[切片] 递归切分完成: 输入=%d字符 输出=%d块 总字符=%d 每片: %s",
        sum(sizes), len(result), sum(sizes), detail,
    )
    # 异常片告警：只在"输入足够长、但切出了碎片或超长片"时才告警。
    # 输入总字符 < min_len*2 时直接整块返回，不算碎片。
    total_input = sum(sizes)
    if total_input >= min_len * 2:
        if any(s < min_len for s in sizes):
            logger.warning("[切片] 存在碎片片(<min_len): %s", [s for s in sizes if s < min_len])
        if any(s > max_len for s in sizes):
            logger.warning(
                "[切片] 存在超长片(>max_len): %s", [s for s in sizes if s > max_len]
            )

    return result


def split_by_markdown(
    documents: list[Document],
    chunk_size: int = 500,
    chunk_overlap: int = 0,
) -> list[Document]:
    """Markdown 结构切片 + JSON 保护。

    按标题层级切分后，每段再用递归切分收敛到目标块长（无碎片、无过长段）。
    """
    if not documents:
        return []

    fragments: list[tuple[str, bool]] = []
    for document in documents:
        text = document.page_content
        if not text.strip():
            continue

        for part_text, is_json in _split_text_with_json_protection(text):
            if is_json:
                fragments.append((part_text, True))
            else:
                for section in _split_by_markdown_headers(part_text):
                    if len(section) <= chunk_size:
                        fragments.append((section, False))
                    else:
                        fragments.extend(
                            _recursive_split_text(section, chunk_size, 0, DEFAULT_SEPARATORS)
                        )

    packed = _merge_no_leftover(fragments, chunk_size)
    if chunk_overlap > 0:
        packed = _apply_overlap(packed, chunk_size, chunk_overlap)
    return _finalize(packed, documents[0].metadata if documents else {})


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

    has_model = False
    model = None
    try:
        from algorithm.NL_to_cypher.vector import _load_model, _model_path
        model = _load_model(_model_path(model_path))
        has_model = True
    except Exception:
        pass

    min_similarity = max(0.3, similarity - 0.15) if has_model else 0.0

    result: list[Document] = []
    for document in documents:
        text = document.page_content
        if not text.strip():
            continue
        base_meta = dict(document.metadata)

        for part_text, is_json in _split_text_with_json_protection(text):
            if is_json:
                result.append(Document(page_content=part_text, metadata={**base_meta, "_is_json": True}))
                continue

            if not has_model or not part_text.strip():
                result.extend(
                    _finalize(
                        _recursive_split_text(part_text, chunk_size, 0, DEFAULT_SEPARATORS),
                        base_meta,
                    )
                )
                continue

            section_spans: list[tuple[int, int]] = []
            all_segments: list[str] = []
            for section in _split_by_markdown_headers(part_text):
                segs = _split_into_segments(section, list(_SENTENCE_SEPARATORS))
                if segs:
                    section_spans.append((len(all_segments), len(all_segments) + len(segs)))
                    all_segments.extend(segs)

            if not all_segments:
                continue

            all_embeddings = model.encode(all_segments, convert_to_numpy=True)
            doc_chunks: list[str] = []
            for start, end in section_spans:
                merged_segs, merged_embs = _merge_tiny_segments(
                    all_segments[start:end], all_embeddings[start:end]
                )
                if not merged_segs:
                    continue
                bps = _detect_breakpoints(merged_segs, merged_embs, method=breakpoint_method)
                doc_chunks.extend(_group_segments_maxmin(
                    merged_segs, merged_embs, chunk_size,
                    tau=similarity, min_similarity=min_similarity,
                    breakpoints=bps, min_segments=min_segments_per_chunk,
                ))

            doc_chunks = _merge_no_leftover(
                [(c, False) for c in doc_chunks], chunk_size
            )
            for chunk_text, _ in doc_chunks:
                result.append(Document(page_content=chunk_text, metadata=dict(base_meta)))

    if chunk_overlap > 0 and len(result) > 1:
        blocks = [(d.page_content, d.metadata.get("_is_json", False)) for d in result]
        blocks = _apply_overlap(blocks, chunk_size, chunk_overlap)
        result = _finalize(blocks, result[0].metadata if result else {})

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
        base_meta = dict(doc.metadata)

        doc_title = base_meta.get("title") or base_meta.get("source") or ""
        if not doc_title:
            first_h1 = re.search(r"^#\s+(.*)", text, re.MULTILINE)
            doc_title = first_h1.group(1).strip() if first_h1 else "未命名文档"

        chunks_to_flush: list[tuple[list[str], str]] = []
        current_path: list[str] = [doc_title]
        current_parts: list[str] = []
        current_len = 0

        def _flush() -> None:
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
            if current_len + len(node_text) > chunk_size and current_parts:
                _flush()
            current_parts.append(node_text)
            current_len += len(node_text)
            for child in node["children"]:
                if current_len + len(child["content"]) > chunk_size and current_parts:
                    _flush()
                _traverse(child, path + [child["title"]])

        for part_text, is_json in _split_text_with_json_protection(text):
            if is_json:
                if current_parts:
                    _flush()
                marker = f"[文档位置: {' > '.join(current_path)}]"
                results.append(Document(
                    page_content=f"{marker}\n\n{part_text}",
                    metadata={**base_meta, "_is_json": True},
                ))
                continue

            tree = _build_heading_tree(part_text)
            if tree["content"].strip():
                if current_len + len(tree["content"]) > chunk_size and current_parts:
                    _flush()
                current_parts.append(tree["content"])
                current_len += len(tree["content"])

            for child in tree["children"]:
                if current_len + len(child["content"]) > chunk_size and current_parts:
                    _flush()
                _traverse(child, [doc_title, child["title"]])

        _flush()

        for path, content in chunks_to_flush:
            prefix = f"[文档位置: {' > '.join(path)}]" if len(path) > 1 else f"[文档位置: {path[0]}]"
            results.append(Document(
                page_content=f"{prefix}\n\n{content}",
                metadata={**base_meta, "_is_json": False},
            ))

    return results
