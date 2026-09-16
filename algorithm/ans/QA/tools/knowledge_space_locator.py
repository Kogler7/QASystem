"""
知识空间定位工具 V5（适配知识地图标准模型）
功能：根据用户问题，定位到根知识空间，并为该空间涉及的维度选出刻度坐标。

标准模型结构：
- KnowledgeApp -[HAS_ROOT_SPACE]-> KnowledgeSpace（空间层级用 HAS_CHILD_SPACE）
- Dimension -[HAS_SCALE_VALUE]-> ScaleValue（全局维度及其刻度值）
- KnowledgeSpace -[BOUND_TO_SCALE]-> ScaleValue（空间绑定到刻度）
- KnowledgeSpace -[CONTAINS_UNIT]-> KnowledgeUnit（空间包含知识单元）

定位粒度说明：
- 子空间之间通过共享刻度相互连通，只有根空间（知识域）之间才真正隔离；
- 因此本工具只定位到【根空间 + 维度刻度坐标】，不做子空间下钻，也不取知识单元；
- 知识单元在后续实体链接环节获取（见 link_entities 等工具）。

匹配策略：
- 根空间通常几乎只有名称，问句也很少出现空间名，因此由 LLM 按名称含义做意图选择；
- 不做问句与空间名的字面打分/子串抢答；
- 旧缺陷是模型已经比较过候选，解析却按「响应里先出现的名字」取值——这里只认结构化选择结果。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

from algorithm.ans.QA.utils import run_llm
from external_services.application import ApplicationServiceError, ExternalCapabilityUnavailable

_JSON_OBJECT = re.compile(r"\{[^{}]*\}")


class KnowledgeSpaceLocator:
    """知识空间定位器 V5 — 根空间定位 + 维度刻度选择（适配标准模型）"""

    def __init__(self, args):
        self.args = args
        from algorithm.ans.QA.knowledge_map_client import KnowledgeMapClient

        self.client = KnowledgeMapClient()
        self._root_candidates: List[str] = []
        self._children_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._match_method = ""
        self._match_confidence = 0.0

    # ==================== 对外接口 ====================

    def locate(self, user_question: str, app_requirement: Optional[Dict] = None) -> Dict:
        """
        定位根知识空间并为涉及的维度选出刻度坐标（不下钻子空间、不取知识单元）。

        Returns:
            定位结果 {可回答, 知识空间(根), 选中的刻度, 置信度, 定位方法, ...}
        """
        try:
            root = self._match_root(user_question, app_requirement)
            if not root:
                return self._fallback("未匹配到唯一根知识空间")

            children = self._child_spaces(root)
            dim_tree = self._load_dimensions_info(root)
            dim_scales = {dim: list(info.get("刻度", [])) for dim, info in dim_tree.items()}

            return {
                "可回答": True,
                "知识空间": root,
                "根节点": root,
                "子空间": children,
                "维度刻度": dim_scales,
                "置信度": self._match_confidence,
                "定位方法": self._match_method or "LLM按根空间名称选择",
                "候选根": list(self._root_candidates),
            }

        except (ApplicationServiceError, ExternalCapabilityUnavailable):
            raise
        except Exception as e:
            return self._fallback(str(e))

    # ==================== Step 1: 匹配根节点 ====================

    def _match_root(self, question: str, app_requirement: Optional[Dict] = None) -> Optional[str]:
        """候选集由图谱给出，多个根空间时一律交给 LLM 选择。"""
        self._match_method = ""
        self._match_confidence = 0.0
        self._children_cache = {}

        names = self._load_root_names()
        self._root_candidates = list(names)
        if not names:
            return None
        if len(names) == 1:
            return self._accept(names[0], "唯一根空间", 0.8)

        children_names = self._load_children_name_map(names)
        picked = self._pick_root_by_llm(question, names, children_names, app_requirement)
        if picked:
            return self._accept(picked, "LLM按根空间名称选择", 0.7)
        return None

    def _accept(self, name: str, method: str, confidence: float) -> str:
        self._match_method = method
        self._match_confidence = confidence
        return name

    def _load_root_names(self) -> List[str]:
        """优先走标准 HAS_ROOT_SPACE；没有这条边时再回退「无父 KnowledgeSpace」。"""
        queries = [
            "MATCH (:KnowledgeApp)-[:HAS_ROOT_SPACE]->(sp:KnowledgeSpace) "
            "RETURN DISTINCT sp.name AS name ORDER BY name",
            "MATCH ()-[:HAS_ROOT_SPACE]->(sp:KnowledgeSpace) "
            "RETURN DISTINCT sp.name AS name ORDER BY name",
            "MATCH (sp:KnowledgeSpace) "
            "OPTIONAL MATCH (parent:KnowledgeSpace)-[:HAS_CHILD_SPACE]->(sp) "
            "WITH sp, parent WHERE parent IS NULL "
            "RETURN DISTINCT sp.name AS name ORDER BY name",
        ]
        for statement in queries:
            try:
                rows = self.client.execute_cypher(statement, {})
            except Exception:
                continue
            names = [str(r.get("name") or "").strip() for r in (rows or [])]
            names = [n for n in names if n]
            if names:
                return names
        return []

    def _load_children_name_map(self, roots: Sequence[str]) -> Dict[str, List[str]]:
        """一跳子空间名称作为给 LLM 的附加名称上下文，不做字面打分。"""
        mapping: Dict[str, List[str]] = {name: [] for name in roots}
        cache: Dict[str, List[Dict[str, Any]]] = {name: [] for name in roots}
        if not roots:
            self._children_cache = cache
            return mapping
        try:
            rows = self.client.execute_cypher(
                "MATCH (p:KnowledgeSpace)-[:HAS_CHILD_SPACE]->(c:KnowledgeSpace) "
                "WHERE p.name IN $roots "
                "RETURN p.name AS root, c.id AS id, c.name AS name "
                "ORDER BY p.name, c.name",
                {"roots": list(roots)},
            )
        except Exception:
            self._children_cache = cache
            return mapping

        seen: Dict[str, set] = {name: set() for name in roots}
        for row in rows or []:
            root = str(row.get("root") or "").strip()
            child_name = str(row.get("name") or "").strip()
            if root not in mapping or not child_name or child_name in seen[root]:
                continue
            seen[root].add(child_name)
            mapping[root].append(child_name)
            cache[root].append({"id": row.get("id"), "name": child_name})
        self._children_cache = cache
        return mapping

    def _pick_root_by_llm(
        self,
        question: str,
        names: Sequence[str],
        children_names: Dict[str, List[str]],
        app_requirement: Optional[Dict],
    ) -> Optional[str]:
        """让模型按问题意图选择候选；解析只认 JSON 中的编号或完整名称。"""
        lines = []
        for index, name in enumerate(names, start=1):
            children = children_names.get(name) or []
            extra = f"\n  子空间名称: {'、'.join(children[:8])}" if children else ""
            lines.append(f"{index}. {name}{extra}")
        names_text = "\n".join(lines)
        requirement_text = self._requirement_context(app_requirement)
        requirement_block = (
            f"\n## 应用侧提示（供理解意图，不是要字面命中的关键词）\n{requirement_text}\n"
            if requirement_text
            else ""
        )
        prompt = f"""请根据用户问题的意图，从下列根知识空间中选择最匹配的一个。

这些空间几乎只有名称。请理解【当前列出的名称及其子空间名称】本身能读出的领域含义，再与问题意图对齐。
不要使用列表之外的业务知识，也不要按「哪个名字先在思考里出现」来选择。

## 用户问题
{question}
{requirement_block}
## 候选根知识空间（只能选其中一个）
{names_text}

## 输出要求
只输出 JSON，不要输出解释、思考过程或其它候选名称。
选中时：{{"index": <编号>, "name": "<上面的完整名称>"}}
无法判断时：{{"index": null, "name": null}}
index 必须是上面的编号，name 必须与该编号对应的完整名称完全一致。"""
        try:
            response = run_llm(
                prompt,
                0.1,
                512,
                self.args.GRAPH_LLM_API_KEY,
                self.args.GRAPH_LLM_MODEL,
            )
        except Exception:
            return None
        return self._parse_chosen_root_name(response, names)

    @staticmethod
    def _requirement_context(app_requirement: Optional[Dict]) -> str:
        if not isinstance(app_requirement, dict) or not app_requirement:
            return ""
        try:
            return json.dumps(app_requirement, ensure_ascii=False)
        except TypeError:
            return str(app_requirement)

    @staticmethod
    def _strip_think(text: str) -> str:
        text = text or ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        text = re.sub(r"<think>.*", "", text, flags=re.DOTALL)
        return text.strip()

    def _parse_chosen_root_name(self, response: str, names: Sequence[str]) -> Optional[str]:
        """只解析模型给出的结构化选择，禁止按候选列表顺序做子串命中。"""
        raw = (response or "").strip()
        if not raw or raw.startswith("LLM_ERROR:"):
            return None
        text = self._strip_think(raw)
        payload = self._extract_choice_json(text)
        if not payload:
            return None
        return self._name_from_choice(payload, names)

    def _name_from_choice(self, payload: Dict[str, Any], names: Sequence[str]) -> Optional[str]:
        name_value = payload.get("name")
        if name_value is None:
            name_value = payload.get("根空间") or payload.get("知识空间")
        chosen = ""
        if name_value not in (None, False):
            chosen = str(name_value).strip()
            if chosen.lower() in {"null", "none"}:
                chosen = ""
        if chosen in names:
            return chosen

        index = self._as_choice_index(payload.get("index"), len(names))
        if index is not None:
            return names[index]
        return None

    @staticmethod
    def _as_choice_index(value: Any, count: int) -> Optional[int]:
        if value is None or value is False:
            return None
        if isinstance(value, str):
            value = value.strip()
            if not value.isdigit():
                return None
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if 1 <= value <= count:
            return value - 1
        return None

    def _extract_choice_json(self, text: str) -> Dict[str, Any]:
        """取最后一个含 name/index 的 JSON 对象，避免思考过程里的示例抢先。"""
        if not text:
            return {}
        candidates: List[Dict[str, Any]] = []
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                candidates.append(data)
        except json.JSONDecodeError:
            pass
        for match in _JSON_OBJECT.finditer(text):
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                candidates.append(data)
        for data in reversed(candidates):
            if any(key in data for key in ("name", "index", "根空间", "知识空间")):
                return data
        return candidates[-1] if candidates else {}

    def _child_spaces(self, root_name: str) -> List[Dict]:
        """取该根空间的一跳子空间（HAS_CHILD_SPACE），定位时一并带回。

        子空间查询失败不阻塞定位，回退为空列表。
        """
        cached = self._children_cache.get(root_name)
        if cached is not None:
            return list(cached)
        try:
            rows = self.client.execute_cypher(
                "MATCH (p:KnowledgeSpace {name:$root})-[:HAS_CHILD_SPACE]->(c:KnowledgeSpace) "
                "RETURN c.id AS id, c.name AS name ORDER BY c.name",
                {"root": root_name},
            )
        except Exception:
            rows = []
        return [{"id": r.get("id"), "name": r.get("name") or ""} for r in (rows or [])]

    # ==================== Step 2: 加载维度及刻度 ====================

    def _load_dimensions_info(self, root_name: str) -> Dict:
        """取根空间绑定的维度/刻度及节点描述（仅根空间直接绑定，不下钻子空间）。

        若根空间自身未绑定任何刻度，回退到全局维度-刻度清单兜底。
        """
        dim_tree: Dict[str, Dict] = {}
        rows = self.client.execute_cypher(
            "MATCH (sp:KnowledgeSpace {name:$root})-[:BOUND_TO_SCALE]->(sv:ScaleValue) "
            "OPTIONAL MATCH (d:Dimension)-[:HAS_SCALE_VALUE]->(sv) "
            "RETURN d.name AS dim_name, d.description AS dim_desc, "
            "       sv.name AS scale_name, sv.description AS scale_desc "
            "ORDER BY d.name, sv.name",
            {"root": root_name},
        )
        for row in rows or []:
            self._add_dim_row(dim_tree, row)

        if not dim_tree:
            rows = self.client.execute_cypher(
                "MATCH (d:Dimension)-[:HAS_SCALE_VALUE]->(s:ScaleValue) "
                "RETURN d.name AS dim_name, d.description AS dim_desc, "
                "       s.name AS scale_name, s.description AS scale_desc "
                "ORDER BY d.name, s.name",
                {},
            )
            for row in rows or []:
                self._add_dim_row(dim_tree, row)
        return dim_tree

    def _add_dim_row(self, dim_tree: Dict, row: Dict) -> None:
        """把一行 {维度, 刻度, 描述} 并入 dim_tree。"""
        dim_name = (row.get("dim_name") or "").strip() or "(未归维度)"
        scale_name = (row.get("scale_name") or "").strip()
        if not scale_name:
            return
        entry = dim_tree.setdefault(
            dim_name,
            {
                "描述": (row.get("dim_desc") or "").strip(),
                "刻度": [],
                "刻度描述": {},
            },
        )
        if scale_name not in entry["刻度"]:
            entry["刻度"].append(scale_name)
        if scale_name not in entry["刻度描述"]:
            node_desc = (row.get("scale_desc") or "").strip()
            entry["刻度描述"][scale_name] = node_desc

    # ==================== 工具方法 ====================

    def _fallback(self, error_info: str = "") -> Dict:
        """LLM/图谱失败时的兜底。保留候选根，供后续工具消歧，避免静默编造。"""
        return {
            "可回答": False,
            "信息": f"知识空间定位异常: {error_info}",
            "知识空间": None,
            "根节点": None,
            "子空间": [],
            "维度刻度": {},
            "置信度": 0.0,
            "定位方法": "兜底",
            "候选根": list(self._root_candidates),
        }

    def get_all_subspaces(self) -> List[Dict]:
        """获取所有知识空间及其绑定刻度（供定位失败时查看候选）"""
        result = self.client.execute_cypher(
            "MATCH (sp:KnowledgeSpace)-[:BOUND_TO_SCALE]->(sv:ScaleValue) "
            "RETURN sp.name AS name, collect(DISTINCT sv.name) AS scales",
            {},
        )
        return [{"子空间名称": r["name"], "关联刻度": r["scales"]} for r in result]
