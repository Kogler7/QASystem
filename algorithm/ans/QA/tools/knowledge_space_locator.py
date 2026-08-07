"""
知识空间定位工具 V4（适配知识地图标准模型）
功能：根据用户问题，定位到知识子空间

标准模型结构：
- KnowledgeApp -[HAS_ROOT_SPACE]-> KnowledgeSpace（空间层级用 HAS_CHILD_SPACE）
- Dimension -[HAS_SCALE_VALUE]-> ScaleValue（全局维度及其刻度值）
- KnowledgeSpace -[BOUND_TO_SCALE]-> ScaleValue（空间绑定到刻度）
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Any, Optional

from algorithm.ans.QA.utils import run_llm
from external_services.application import ApplicationServiceError, ExternalCapabilityUnavailable


class KnowledgeSpaceLocator:
    """知识空间定位器 V4 — 逐层下钻定位（适配标准模型）"""

    def __init__(self, args):
        self.args = args
        from algorithm.ans.QA.knowledge_map_client import KnowledgeMapClient

        self.client = KnowledgeMapClient()
        self._md_descriptions = self._load_md_descriptions()

    # ==================== 对外接口 ====================

    def locate(self, user_question: str, app_requirement: Optional[Dict] = None) -> Dict:
        """
        定位知识子空间（逐层下钻）

        Args:
            user_question: 用户问题
            app_requirement: 可选的应用需求信息

        Returns:
            定位结果 {目标子空间, 选中的刻度, 定位路径, ...}
        """
        try:
            # Step 1: 匹配根节点
            root = self._match_root(user_question)
            if not root:
                return self._fallback("未找到根节点")

            # Step 2: 查询维度及刻度
            dim_tree = self._load_dimensions_info(root)
            if not dim_tree:
                return self._fallback("维度刻度加载不完整")

            # Step 3: LLM逐维度选刻度（用md描述）
            selected = self._select_scales_by_llm(user_question, app_requirement, dim_tree)

            # Step 4: 刻度反向图谱查子空间
            subspace = self._match_subspace(selected)
            if not subspace:
                return {
                    "可回答": False,
                    "信息": "当前知识空间无法回答该问题，未找到匹配的子空间",
                    "目标子空间": None,
                    "选中的刻度": selected,
                    "置信度": 0.0,
                    "定位方法": "逐层下钻（无匹配子空间）",
                    "根节点": root
                }

            return {
                "可回答": True,
                "目标子空间": subspace,
                "选中的刻度": selected,
                "置信度": 0.9,
                "选择原因": self._reason or "LLM 根据问题语义逐维度匹配合成",
                "定位方法": "逐层下钻（图谱反向匹配）",
                "根节点": root
            }

        except (ApplicationServiceError, ExternalCapabilityUnavailable):
            raise
        except Exception as e:
            return self._fallback(str(e))

    # ==================== Step 1: 匹配根节点 ====================

    def _match_root(self, question: str) -> Optional[str]:
        """从图谱查根节点（无父空间），用LLM匹配用户问题"""
        result = self.client.execute_cypher(
            "MATCH (sp:KnowledgeSpace) "
            "OPTIONAL MATCH (parent:KnowledgeSpace)-[:HAS_CHILD_SPACE]->(sp) "
            "WITH sp, parent "
            "WHERE parent IS NULL "
            "RETURN DISTINCT sp.name AS name, sp.description AS desc "
            "LIMIT 10",
            {},
        )
        if not result:
            return None
        if len(result) == 1:
            return result[0]["name"]

        roots_text = "\n".join(
            f"- {r['name']}" + (f"（{r.get('desc')}）" if r.get('desc') else "")
            for r in result
        )
        prompt = f"""请根据用户问题，选择最匹配的知识根节点。

## 用户问题
{question}

## 可用根节点

{roots_text}

## 输出要求

只输出最匹配的根节点名称，不要其他内容。"""
        try:
            response = run_llm(prompt, 0.3, 200, self.args.GRAPH_LLM_API_KEY, self.args.GRAPH_LLM_MODEL)
            if response:
                for r in result:
                    if r["name"] in response:
                        return r["name"]
        except Exception:
            pass
        return result[0]["name"]

    # ==================== Step 2: 加载维度及刻度 ====================

    def _load_dimensions_info(self, root_name: str) -> Dict:
        """
        从图谱查询全局维度及其刻度（标准模型：Dimension -[HAS_SCALE_VALUE]-> ScaleValue）
        """
        dim_tree: Dict[str, Dict] = {}
        result = self.client.execute_cypher(
            "MATCH (d:Dimension)-[r:HAS_SCALE_VALUE]->(s:ScaleValue) "
            "RETURN d.id AS dim_id, d.name AS dim_name, d.description AS dim_desc, "
            "       s.id AS scale_id, s.name AS scale_name "
            "ORDER BY d.name, s.name",
            {},
        )
        for row in result:
            dim_name = row.get("dim_name") or ""
            if not dim_name:
                continue
            entry = dim_tree.setdefault(dim_name, {"刻度": [], "描述": row.get("dim_desc") or ""})
            scale_name = row.get("scale_name") or ""
            if scale_name and scale_name not in entry["刻度"]:
                entry["刻度"].append(scale_name)
        return dim_tree

    # ==================== Step 3: LLM 选刻度 ====================

    def _select_scales_by_llm(self, question: str, app_req: Optional[Dict],
                              dim_tree: Dict) -> Dict[str, str]:
        """LLM根据md描述为每个维度选一个刻度"""
        self._reason = ""

        # 构建刻度清单（用md描述补语义）
        dim_choices = []
        for dim_name in dim_tree:
            info = dim_tree[dim_name]
            scales = info.get("刻度", [])
            if not scales:
                continue
            lines = [f"### {dim_name}"]
            for sc in scales:
                desc = self._md_descriptions.get(sc, "")
                lines.append(f"  - {sc}: {desc}")
            dim_choices.append("\n".join(lines))

        if not dim_choices:
            return {}

        # 构建请求文本
        req_text = self._build_requirement_text(app_req)

        prompt = f"""请根据用户问题和需求信息，为每个维度选择最合适的刻度。

## 用户问题
{question}

{req_text}
## 可选刻度（含描述）

{chr(10).join(dim_choices)}

## 输出要求

请为每个维度选择一个最匹配的刻度。输出JSON格式：
{{
    "{list(dim_tree.keys())[0]}": "刻度名",
    ...
    "reason": "选择原因"
}}

只输出JSON，不要有其他内容。"""

        try:
            response = run_llm(prompt, 0.3, 800, self.args.GRAPH_LLM_API_KEY, self.args.GRAPH_LLM_MODEL)
            parsed = self._extract_json(response)
            if parsed:
                self._reason = parsed.pop("reason", "")
                result = {}
                for dim_name in parsed:
                    val = str(parsed[dim_name])
                    if val and val != "null":
                        result[dim_name] = val
                return result
        except Exception:
            pass

        return {}

    def _build_requirement_text(self, app_req: Optional[Dict]) -> str:
        """构建需求信息的文本描述"""
        if not app_req or not isinstance(app_req, dict):
            return ""
        parts = []
        name = app_req.get("需求名称") or app_req.get("name", "")
        if name:
            parts.append(f"### 需求名称\n{name}")
        desc = app_req.get("问题描迷") or app_req.get("description", "")
        if desc:
            parts.append(f"### 问题描述\n{desc}")
        goal = app_req.get("目标") or app_req.get("goal", "")
        if goal:
            parts.append(f"### 目标\n{goal}")
        constraints = app_req.get("约束条件") or app_req.get("constraints", [])
        if constraints:
            parts.append(f"### 约束条件\n{', '.join(constraints)}")
        stakeholders = app_req.get("关联利益相关者") or app_req.get("stakeholders", [])
        if stakeholders:
            lines = []
            for sh in stakeholders:
                name = sh.get("名称") or sh.get("name", "")
                concerns = sh.get("关注点") or sh.get("concerns", [])
                if name and concerns:
                    lines.append(f"- {name}：{'、'.join(concerns)}")
            if lines:
                parts.append(f"### 关联利益相关者及其关注点\n" + "\n".join(lines))
        return "\n\n".join(parts) + "\n\n" if parts else ""

    # ==================== Step 4: 刻度 → 子空间 ====================

    def _match_subspace(self, selected: Dict[str, str]) -> Optional[str]:
        """
        用选中的刻度反向匹配知识空间（空间需绑定到全部选中刻度）
        """
        scale_names = [val for val in selected.values() if val]
        if not scale_names:
            return None
        query = (
            "MATCH (sp:KnowledgeSpace)-[:BOUND_TO_SCALE]->(sv:ScaleValue) "
            "WHERE sv.name IN $scale_names "
            "WITH sp, count(DISTINCT sv.name) AS matched "
            "WHERE matched = $expected "
            "RETURN sp.name AS name "
            "LIMIT 1"
        )
        result = self.client.execute_cypher(
            query,
            {"scale_names": scale_names, "expected": len(scale_names)},
        )
        if result:
            return result[0]["name"]
        return None

    # ==================== 工具方法 ====================

    def _load_md_descriptions(self) -> Dict[str, str]:
        """从 knowledge_space_dimensions.md 加载刻度描述"""
        md_path = Path(__file__).with_name("knowledge_space_dimensions.md")
        desc = {}
        try:
            with md_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("|") and line.endswith("|"):
                        parts = [p.strip() for p in line.split("|")]
                        if len(parts) >= 3 and parts[1] and parts[1] not in ("刻度", "") and not parts[1].startswith("-"):
                            desc[parts[1]] = parts[2]
        except Exception:
            pass
        return desc

    def _extract_json(self, text: str) -> Optional[Dict]:
        """从文本中提取JSON"""
        try:
            json_match = re.search(r'\{[\s\S]*\}', text)
            if json_match:
                return json.loads(json_match.group(0))
        except Exception:
            pass
        return None

    def _fallback(self, error_info: str = "") -> Dict:
        """LLM/图谱失败时的兜底"""
        return {
            "可回答": False,
            "信息": f"知识空间定位异常: {error_info}",
            "目标子空间": None,
            "选中的刻度": {},
            "置信度": 0.0,
            "定位方法": "兜底",
            "根节点": None
        }

    def get_all_subspaces(self) -> List[Dict]:
        """获取所有知识空间及其绑定刻度"""
        result = self.client.execute_cypher(
            "MATCH (sp:KnowledgeSpace)-[:BOUND_TO_SCALE]->(sv:ScaleValue) "
            "RETURN sp.name AS name, collect(DISTINCT sv.name) AS scales",
            {},
        )
        return [{"子空间名称": r["name"], "关联刻度": r["scales"]} for r in result]
