"""本体驱动的 LLM 知识抽取（V2），不带模板匹配。

参考：kg-extract-v7 的抽取方法 — LLM 直接读取 PUML 本体作为抽取 schema。
直接使用 OpenAI SDK 调用 LLM。
"""
import json
import logging
import os
import re
import time
from typing import Any, Optional

from openai import OpenAI

from .config import get_llm_config
from .graph_utils import build_node_properties, filter_relationships_with_known_nodes, merge_node_properties
from .ontology_schema import OntologySchema
from .scheduler import get_llm_scheduler

logger = logging.getLogger(__name__)


# ── 通用抽取指令（参考 kg-extract-v7）──

GENERIC_INSTRUCTION = """## 抽取规则

你是知识图谱抽取专家。上方"本体定义"中的 PlantUML 类图就是你唯一的抽取 schema，请严格按本体定义抽取：

### 实体抽取
- 本体中每个 class 定义一个实体类型，实体 `type` 必须使用下方“本体标识目录”给出的稳定类型标识
- 类体中每个 `+属性名: 类型 // 注释` 即为该实体的属性字段，从文档中提取对应值填入 `properties`
- 属性类型标注为 `String[]` 的用 JSON 数组表示，标注为 `Boolean` 的用 true/false
- 文档中未提及的属性设为 null 或不包含在 properties 中
- 可以根据文档结构（## 标题、|| 表格、// 段落、&& 小节等）识别内容边界，将对应内容映射到本体属性

### 关系抽取
- 本体中每条 `A "1" --> "B" : REL_NAME` 定义一个关系类型
- relations 中每条记录的 `from_type`/`to_type` 使用上述元模型类标识，`rel_type` 使用本体关系名
- `from_name`/`to_name` 使用实体的 name 属性值（或 @identity 标注的标识字段）
- 每输出一条 relation，必须保证 `entities` 中同时存在对应的源节点和目标节点
- 如果文本里明显存在关系，但某一端实体尚未列入 `entities`，先补建该实体，再输出 relation
- 补建实体时，至少补齐该实体的 `type`、`name`，并尽量补充文本中可确定的 `properties`
- 禁止输出引用不存在实体的 relation；如果无法确定任一端实体，就不要输出这条 relation
- 从文档的层级结构推断父子归属关系
- 从文档中的流程描述/关系表推断流程流转关系
- 标注为"后处理建立"/"postprocess"的关系**跳过**，不在此阶段抽取

### 输出要求
- 只输出 JSON，不要任何解释、markdown 代码块或额外文字
- 不要自创本体中不存在的类型、属性或关系名
- 即使文档中某些字段缺失，也不要编造值

### 关系与实体一致性示例
- 正确示例：若输出 `A -[REL]-> B`，则 `entities` 中必须同时有 A 和 B；如果 B 尚未抽出，先把 B 补进 `entities`
- 错误示例：`entities` 里只有 A，却在 `relations` 里输出 `A -[REL]-> B`
- 记忆规则：宁可少一条无法落到实体集合中的 relation，也不要输出悬空 relation"""


def build_generic_extraction_prompt(text: str) -> str:
    """构建通用抽取 prompt（无本体定义时使用），让 LLM 自由识别实体和关系。"""

    return f"""你是一个知识图谱抽取专家。请从以下文本中自由识别实体和关系，构建知识图谱。

## 抽取规则

### 实体抽取
- 从文本中识别所有有意义的实体（如人员、组织、设备、概念、术语、指标等）
- 为每个实体选择合适的 type 类型名称（如 Person、Organization、Equipment、Concept、Term、Metric、Document 等）
- 从文本中提取该实体的 name（名称）和其他可用的属性
- 属性以键值对形式填入 properties，未提到的属性可省略
- 只抽取文本中明确提到的实体，不要编造

### 关系抽取
- 从文本中识别实体之间的关系
- 为每条关系指定合适的 rel_type（如 "工作于"、"管理"、"包含"、"定义"、"关联"、"属于" 等）
- from_name/to_name 使用实体的 name 属性值
- 每输出一条 relation，必须保证 entities 中同时存在对应的源节点和目标节点
- 不要输出悬空的 relation（引用不存在的实体）

### 输出要求
- 只输出 JSON，不要任何解释、markdown 代码块或额外文字
- 不要自创文本中不存在的内容

## 待抽取文本
{text}

## 输出格式
{{{{
  "entities": [
    {{{{
      "type": "实体类型",
      "name": "实体名称",
      "properties": {{{{ "属性名": "属性值", ... }}}}
    }}}}
  ],
  "relations": [
    {{{{
      "from_type": "源实体类型",
      "from_name": "源实体名称",
      "rel_type": "关系类型",
      "to_type": "目标实体类型",
      "to_name": "目标实体名称"
    }}}}
  ]
}}}}"""


def build_extraction_prompt(ontology: str, text: str) -> str:
    """构建本体驱动的抽取 prompt（参考 kg-extract-v7 extract.py::build_prompt）。"""

    # Extract @identity lines for entity matching
    identity_hints = []
    for line in ontology.split("\n"):
        if "@identity" in line:
            identity_hints.append(line.strip())
    identity_text = "\n".join(identity_hints) if identity_hints else "所有类型默认用 name 属性作为 identity 标识"
    schema_catalog = OntologySchema.parse(ontology).prompt_catalog()

    return f"""你是一个知识图谱抽取专家。请根据以下本体定义，从文本中抽取实体和关系。

## 本体定义
{ontology}

## 本体标识目录
{schema_catalog}

## 实体标识规则
{identity_text}
- relations 中的 from_name/to_name 必须填写实体的 identity 标识值
- 若 identity 指定的属性值为空，回退使用实体的 name 属性值
- 输出 relations 之前，先检查 `entities` 是否已经包含两端实体；没有就先补到 `entities`

{GENERIC_INSTRUCTION}

## 待抽取文本
{text}

## 输出格式
{{
  "entities": [
    {{
      "type": "本体标识目录中定义的稳定类型标识",
      "name": "实体标识值（按 @identity 规则）",
      "properties": {{ "属性名": "属性值", ... }}
    }}
  ],
  "relations": [
    {{
      "from_type": "源元模型类标识",
      "from_name": "源实体标识值",
      "rel_type": "本体中定义的关系名",
      "to_type": "目标元模型类标识",
      "to_name": "目标实体标识值"
    }}
  ]
}}"""


def extract_json_from_response(response: str) -> dict:
    """从 LLM 响应中提取 JSON 对象（参考 kg-extract-v7 的 extract_json_from_response）。"""
    response = (response or "").strip()
    if not response:
        return {}

    # Remove markdown code fences
    if response.startswith("```"):
        lines = response.split("\n")
        response = "\n".join(lines[1:]) if len(lines) > 1 else response
        if response.endswith("```"):
            response = response[:-3].rstrip()

    # Find first { and last }
    start = response.find("{")
    end = response.rfind("}")
    if start >= 0 and end > start:
        response = response[start:end + 1]

    try:
        return json.loads(response)
    except json.JSONDecodeError as e:
        logger.warning("JSON 解析失败: %s, 原始响应前 500 字符: %s", e, response[:500])
        return {}


def _entities_to_graph(extracted: dict) -> dict:
    """
    将抽取结果 { entities, relations } 转换为统一的 graph 结构：
    { nodes: [...], relationships: [...] }
    （不再需要 ontology_relations）
    """
    nodes = []
    relationships = []

    # Build uid → node lookup for dedup
    node_index = {}

    for entity in extracted.get("entities", []):
        etype = entity.get("type", "Element")
        ename = entity.get("name", "")
        props = entity.get("properties", {}) or {}
        if not isinstance(props, dict):
            props = {}

        uid = f"{etype}::{ename.lower()}" if ename else f"{etype}::{hash(json.dumps(props, sort_keys=True, default=str))}"

        if uid not in node_index:
            node = {
                "uid": uid,
                "label": etype,
                "properties": build_node_properties(ename, props),
            }
            nodes.append(node)
            node_index[uid] = node
        else:
            # Merge properties
            node_index[uid]["properties"] = merge_node_properties(node_index[uid]["properties"], props)

    for rel in extracted.get("relations", []):
        from_type = rel.get("from_type", "")
        from_name = rel.get("from_name", "")
        to_type = rel.get("to_type", "")
        to_name = rel.get("to_name", "")
        rel_type = rel.get("rel_type", "RELATED")

        from_uid = f"{from_type}::{from_name.lower()}" if from_name else ""
        to_uid = f"{to_type}::{to_name.lower()}" if to_name else ""

        if from_uid and to_uid:
            relationships.append({
                "from_uid": from_uid,
                "to_uid": to_uid,
                "type": rel_type,
            })

    relationships = filter_relationships_with_known_nodes(
        relationships,
        {str(node.get("uid") or "") for node in nodes if str(node.get("uid") or "")},
    )

    return {
        "nodes": nodes,
        "relationships": relationships,
    }


def _build_openai_client(
    base_url: str = "",
    api_key: str = "",
) -> OpenAI:
    """创建 OpenAI 客户端。"""
    kwargs: dict = {}
    if base_url:
        kwargs["base_url"] = base_url
    if api_key:
        kwargs["api_key"] = api_key
    else:
        # openai 需要 api_key，传占位符避免报错（本地 vLLM 不校验）
        kwargs["api_key"] = api_key or "not-needed"
    return OpenAI(**kwargs)


def extract_v2(
    ontology: str,
    text: str,
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    json_retries: Optional[int] = None,
) -> dict[str, Any]:
    """
    本体驱动的知识抽取 V2（无模板匹配）。

    参数：
        ontology: PUML 本体文本
        text: 待抽取文本
        base_url: LLM API 地址，不传则使用环境变量
        api_key: API 密钥，不传则使用环境变量
        model: 模型名，不传则使用环境变量
        max_tokens: 最大输出 token 数

    返回：
        {"raw": {...}, "graph": {"nodes": [...], "relationships": [...]}}
    """
    ontology = (ontology or "").strip()
    text = (text or "").strip()

    if not text:
        return {
            "raw": {},
            "graph": {"nodes": [], "relationships": []},
        }

    cfg = get_llm_config()
    base_url = base_url or cfg["base_url"]
    api_key = api_key or cfg["api_key"]
    model = model or cfg["model"]
    max_tokens = max_tokens or cfg["max_tokens"]
    json_retries = max(
        0,
        json_retries
        if json_retries is not None
        else int(os.getenv("KG_EXTRACT_LLM_JSON_RETRIES", os.getenv("KG_EXTRACT_JSON_RETRIES", "2"))),
    )

    logger.info(
        "extract_v2: ontology_len=%d text_len=%d model=%s",
        len(ontology), len(text), model,
    )

    # 构建 prompt（有本体按本体抽取，无本体通用抽取）
    if ontology:
        prompt = build_extraction_prompt(ontology, text)
        system_msg = "你是一个知识图谱抽取专家。严格按照本体定义从文本中抽取实体和关系。只输出JSON，不要任何解释。"
    else:
        prompt = build_generic_extraction_prompt(text)
        system_msg = "你是一个知识图谱抽取专家。从文本中自由识别实体和关系。只输出JSON，不要任何解释。"

    t0 = time.perf_counter()

    # 调用 LLM（流式读取，避免等待超大响应一次性返回）
    client = _build_openai_client(base_url=base_url, api_key=api_key)
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": prompt},
    ]
    attempts: list[dict[str, Any]] = []
    extracted: dict[str, Any] = {}
    raw_response = ""
    for attempt in range(json_retries + 1):
        try:
            with get_llm_scheduler().request_slot():
                stream = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=max_tokens,
                    timeout=180,
                    stream=True,
                )
                parts: list[str] = []
                for chunk in stream:
                    if not chunk.choices:
                        continue
                    content = chunk.choices[0].delta.content
                    if content:
                        parts.append(content)
            raw_response = "".join(parts)
        except Exception as exc:
            attempts.append({"attempt": attempt + 1, "type": "request", "message": str(exc)})
            logger.warning("LLM 调用第 %d 次失败: %s", attempt + 1, exc)
            if attempt >= json_retries:
                client.close()
                return {
                    "raw": {},
                    "graph": {"nodes": [], "relationships": []},
                    "_error": "LLM 调用失败",
                    "error_code": "LLM_REQUEST_FAILED",
                    "attempts": attempts,
                }
            time.sleep(min(2 ** attempt, 4))
            continue

        extracted = extract_json_from_response(raw_response)
        entities = extracted.get("entities") if isinstance(extracted, dict) else None
        relations = extracted.get("relations") if isinstance(extracted, dict) else None
        if isinstance(entities, list) and isinstance(relations, list):
            break

        attempts.append({
            "attempt": attempt + 1,
            "type": "json",
            "message": "响应不是包含 entities 和 relations 数组的 JSON 对象",
        })
        logger.warning("LLM 响应第 %d 次 JSON 校验失败", attempt + 1)
        if attempt < json_retries:
            messages = [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": raw_response[:12000]},
                {
                    "role": "user",
                    "content": "上一次输出不是合法的目标 JSON。请重新输出且只输出包含 entities 和 relations 两个数组的 JSON 对象。",
                },
            ]

    elapsed = time.perf_counter() - t0
    logger.info("extract_v2: LLM call done in %.2fs, response_len=%d", elapsed, len(raw_response or ""))

    if not extracted or not isinstance(extracted.get("entities"), list) or not isinstance(extracted.get("relations"), list):
        logger.warning("extract_v2: 重试后仍未获得有效 JSON")
        client.close()
        return {
            "raw": {},
            "graph": {"nodes": [], "relationships": []},
            "_error": "LLM 返回内容无法解析为知识抽取 JSON",
            "error_code": "LLM_JSON_PARSE_FAILED",
            "attempts": attempts,
            "_raw_response": raw_response[:1000] if raw_response else "",
        }

    # 有本体时通过本体模型解析层规范化类型、属性和关系标识；无本体时跳过
    if ontology:
        extracted = OntologySchema.parse(ontology).normalize_extraction(extracted)

    # 转换为图结构
    graph = _entities_to_graph(extracted)
    client.close()

    return {
        "raw": extracted,
        "graph": graph,
        "attempts": attempts,
    }
