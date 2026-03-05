import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class TemplateDef:
    """
    从 PUML 中解析出来的模板定义。
    - key: 模板键名（如 ConstraintRuleTypeTemplate / ImplicationRuleTypeTemplate 等）
    - template: 模板字符串（包含 [槽位] 与 / 备选）
    """

    key: str
    template: str


@dataclass(frozen=True)
class TemplateMatch:
    """
    一次正则匹配结果。
    - key/template: 来源模板
    - matched_text: 原文中被匹配到的片段
    - slots: 抽取到的槽位值（key 为模板里的槽位名，如 "对象"、"约束条件"）
    - span: matched_text 在原 sentence 中的 span
    - sentence: 发生匹配的原句
    """

    key: str
    template: str
    matched_text: str
    slots: Dict[str, str]
    span: Tuple[int, int]
    sentence: str


_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")


def split_sentences(text: str) -> List[str]:
    """
    轻量句子切分：按中文/英文常见句末符号及换行切。
    """
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(text) if p and p.strip()]
    return parts


def parse_examples_templates_from_puml(puml_text: str) -> List[TemplateDef]:
    """
    从 PUML 中解析 ExamplesTemplate <<static>> 里的模板行，例如：

        class ExamplesTemplate <<static>> {
            + ConstraintRuleTypeTemplate: "[对象]必须/应当满足[约束条件]"
            + ImplicationRuleTypeTemplate: "如果[antecedent]，那么[consequent]"
        }
    """
    in_block = False
    out: List[TemplateDef] = []

    for raw_line in puml_text.splitlines():
        line = raw_line.strip()
        if not in_block:
            if line.startswith("class ExamplesTemplate") and "{" in line:
                in_block = True
            continue

        if line.startswith("}"):
            break

        # 解析：+ Key: "Template"
        m = re.match(r'^\+\s*([A-Za-z_]\w*)\s*:\s*"(.*)"\s*$', line)
        if not m:
            continue
        key, template = m.group(1), m.group(2)
        out.append(TemplateDef(key=key, template=template))

    return out


def _escape_outside_brackets_to_regex(text: str) -> str:
    """
    将非槽位的模板文本转为 regex 字面量，并把斜杠表示的备选转换为 alternation。
    例如：'必须/应当满足' -> '(?:必须|应当满足)'（保守实现：按整段 split）

    注意：这里只处理 bracket 外部文本，槽位由调用方替换为捕获组。
    """
    if "/" not in text:
        return re.escape(text)

    # 仅在模板语义中：'A/B' 通常表示二选一。这里按最简单规则处理成 alternation。
    alts = [re.escape(p) for p in text.split("/") if p != ""]
    if len(alts) <= 1:
        return re.escape(text)
    return "(?:" + "|".join(alts) + ")"


def compile_template_regex(template: str) -> Tuple[re.Pattern, List[str]]:
    """
    将模板字符串编译为 regex pattern。

    规则（保守、可解释）：
    - [xxx] 视为一个槽位，输出为捕获组，槽位名保留为 xxx
    - bracket 外出现的 '/' 视为备选（alternation）
    - 默认用非贪婪匹配，适合在 sentence 级别进行匹配
    """
    # 拆分 bracket 段落
    tokens: List[Tuple[str, str]] = []  # (kind, value) kind in {"text","slot"}
    i = 0
    while i < len(template):
        if template[i] == "[":
            j = template.find("]", i + 1)
            if j == -1:
                tokens.append(("text", template[i:]))
                break
            slot_name = template[i + 1 : j].strip()
            tokens.append(("slot", slot_name))
            i = j + 1
        else:
            j = template.find("[", i)
            if j == -1:
                tokens.append(("text", template[i:]))
                break
            tokens.append(("text", template[i:j]))
            i = j

    slot_names: List[str] = []
    pattern_parts: List[str] = []
    slot_idx = 0

    for kind, val in tokens:
        if kind == "text":
            # 宽松处理空白：模板外部文本允许出现任意空白
            escaped = _escape_outside_brackets_to_regex(val)
            escaped = escaped.replace(r"\ ", r"\s*")
            pattern_parts.append(escaped)
        else:
            slot_idx += 1
            # Python 的命名捕获组名必须是 ASCII；这里用 slotN，同时保存原槽位名映射
            group_name = f"slot{slot_idx}"
            slot_names.append(val)
            # 在句子范围内匹配：尽量不跨行
            pattern_parts.append(f"(?P<{group_name}>[^\\n]+?)")

    # 允许两侧有少量空白
    pattern = r"\s*" + "".join(pattern_parts) + r"\s*"
    return re.compile(pattern), slot_names


def match_templates(
    text: str,
    templates: Iterable[TemplateDef],
    *,
    sentence_level: bool = True,
    max_matches_per_template: int = 50,
) -> List[TemplateMatch]:
    """
    在文本中匹配模板，并抽取槽位值。

    推荐用法：
    - 先 sentence_level=True 粗匹配出候选片段与槽位
    - 再把候选 sentence / matched_text 交给大模型做：
      - 槽位归一化（同义、标准化术语）
      - 对复杂跨句逻辑补全 antecedent/consequent 等
    """
    sentences = split_sentences(text) if sentence_level else [text]
    out: List[TemplateMatch] = []

    compiled: List[Tuple[TemplateDef, re.Pattern, List[str]]] = []
    for t in templates:
        pat, slot_names = compile_template_regex(t.template)
        compiled.append((t, pat, slot_names))

    for sentence in sentences:
        if not sentence:
            continue
        for t, pat, slot_names in compiled:
            count = 0
            for m in pat.finditer(sentence):
                count += 1
                if count > max_matches_per_template:
                    break
                slot_map: Dict[str, str] = {}
                for idx, slot_label in enumerate(slot_names, start=1):
                    slot_map[slot_label] = (m.group(f"slot{idx}") or "").strip()
                out.append(
                    TemplateMatch(
                        key=t.key,
                        template=t.template,
                        matched_text=m.group(0).strip(),
                        slots=slot_map,
                        span=m.span(),
                        sentence=sentence,
                    )
                )

    return out


def load_templates_from_puml_file(puml_path: str) -> List[TemplateDef]:
    with open(puml_path, "r", encoding="utf-8") as f:
        return parse_examples_templates_from_puml(f.read())


def build_llm_hint_from_matches(matches: List[TemplateMatch]) -> str:
    """
    将正则匹配得到的槽位候选，组织成一段可选的“提示信息”，用于给大模型补全/归一化。
    """
    if not matches:
        return ""
    lines = ["已通过模板正则在原文中匹配到以下候选（供你参考，不要捏造）："]
    for i, m in enumerate(matches, start=1):
        lines.append(f"{i}. {m.key} :: {m.matched_text}")
        if m.slots:
            for k, v in m.slots.items():
                lines.append(f"   - {k} = {v}")
    return "\n".join(lines)


__all__ = [
    "TemplateDef",
    "TemplateMatch",
    "split_sentences",
    "parse_examples_templates_from_puml",
    "compile_template_regex",
    "match_templates",
    "load_templates_from_puml_file",
    "build_llm_hint_from_matches",
]


if __name__ == "__main__":
    example_puml = """
@startuml
class ExamplesTemplate <<static>> {
    + ImplicationRuleTypeTemplate: "如果[antecedent]，那么[consequent]"
    + ConstraintRuleTypeTemplate: "[对象]必须/应当满足[约束条件]"
}
@endum
""".strip()
    templates = parse_examples_templates_from_puml(example_puml)
    sample_text = "如果轮对轮径小于772mm，那么更新车轮、更换轴箱轴承。车轮必须满足轮缘厚度要求。"
    ms = match_templates(sample_text, templates)
    print(ms)
