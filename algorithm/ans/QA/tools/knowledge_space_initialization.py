import sys
import os
import json
import re
from typing import Dict, List, Any

current_file = os.path.abspath(__file__)
tools_dir = os.path.dirname(current_file)
qa_dir = os.path.dirname(tools_dir)
ans_dir = os.path.dirname(qa_dir)
algorithm_dir = os.path.dirname(ans_dir)
project_root = os.path.dirname(algorithm_dir)
sys.path.insert(0, project_root)

from algorithm.ans.QA.utils import run_llm
from algorithm.ans.QA.neo4j_client import Neo4jClient


class KnowledgeSpaceInitializationTool:
    """
    知识空间初始化工具（三步骤）
    1. 维度推荐 → recommend_dimensions
    2. 空间草稿 → generate_space_draft
    3. 空间初始化 → initialize_space
    维度定义来源：knowledge_space_dimensions.md
    """

    _MD_DIMENSIONS = None

    _DIMENSION_NAMES = ["方面", "视角", "时间", "空间", "抽象层级"]
    _DIMENSION_DEFAULTS = {"时间": "年月", "空间": "集团", "抽象层级": "逻辑"}
    _OUTPUT_ORDER = ["时间", "抽象层级", "方面", "视角", "空间"]

    _ID_MAP = {
        "时间": "dim-time", "空间": "dim-space", "抽象层级": "dim-abstract",
        "方面": "dim-aspect", "视角": "dim-perspective",
        "年月": "sc-month", "集团": "sc-org", "逻辑": "sc-logic",
        "架构管理": "sc-arch", "战略": "sc-strategy", "运营": "sc-oper",
        "服务": "sc-service", "人员": "sc-people", "资源": "sc-resource",
        "安全": "sc-security", "项目": "sc-project", "标准": "sc-standard",
        "实际资源": "sc-physical", "动机": "sc-motive", "分类法": "sc-taxonomy",
        "结构": "sc-structure", "连通性": "sc-connect", "流程": "sc-process",
        "状态": "sc-status", "序列": "sc-sequence", "信息": "sc-info",
        "参数": "sc-param", "约束": "sc-constraint",
        "路线图": "sc-roadmap", "可追溯性": "sc-trace",
    }

    def __init__(self, args):
        self.args = args
        self.client = Neo4jClient(
            os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            os.getenv("NEO4J_USERNAME", "neo4j"),
            os.getenv("NEO4J_PASSWORD", "neo4j2025"),
        )

    # ==================== MD 文档加载 ====================

    def _load_dimensions_md(self) -> str:
        md_path = os.path.join(os.path.dirname(__file__), "knowledge_space_dimensions.md")
        if not os.path.exists(md_path):
            raise FileNotFoundError(f"维度库文档不存在: {md_path}")
        with open(md_path, "r", encoding="utf-8") as f:
            return f.read()

    def _parse_dimensions(self) -> Dict:
        if self._MD_DIMENSIONS is not None:
            return self._MD_DIMENSIONS

        md_content = self._load_dimensions_md()
        dimensions = {}
        current_dim = None
        in_table = False

        for line in md_content.split("\n"):
            line = line.strip()

            dim_match = re.match(r'^##\s+\d+\.\s*(.+?)(?:维度)?$', line)
            if dim_match:
                dim_name = re.sub(r'维度$', '', dim_match.group(1).strip())
                dim_name = re.sub(r'^\d+\.\s*', '', dim_name)
                if dim_name and dim_name not in dimensions:
                    current_dim = dim_name
                    dimensions[current_dim] = {"维度类型": dim_name, "维度描述": "", "刻度": {}}
                    in_table = False
                continue

            if current_dim and not dimensions[current_dim]["维度描述"]:
                desc_match = re.match(r'^\*\*(.+?)\*\*', line)
                if desc_match:
                    rest = line[desc_match.end():].strip()
                    dimensions[current_dim]["维度描述"] = desc_match.group(1) + rest
                    continue

            if current_dim and line.startswith("| 刻度 | 含义 |"):
                in_table = True
                continue

            if in_table and re.match(r'^\|[-|\s]+\|$', line):
                continue

            if in_table and current_dim and line.startswith("|") and line.endswith("|"):
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 3 and parts[1] and parts[2] and not parts[1].startswith("-"):
                    dimensions[current_dim]["刻度"][parts[1]] = {"刻度值": parts[2]}

            if in_table and (line.startswith("##") or line.startswith("---")):
                in_table = False

        self._MD_DIMENSIONS = dimensions
        return dimensions

    # ==================== 格式转换辅助 ====================

    def _dim_id(self, name: str) -> str:
        return self._ID_MAP.get(name, f"dim-{name}")

    def _scale_id(self, name: str) -> str:
        return self._ID_MAP.get(name, f"sc-{name}")

    def _md_to_protocol(self) -> List[Dict]:
        """将 md 解析结果转为协议格式的 total_dimensions"""
        dims = self._parse_dimensions()
        result = []
        for dim_name in self._OUTPUT_ORDER:
            config = dims.get(dim_name)
            if not config:
                continue
            scales = [
                {"scale_id": self._scale_id(sn), "scale_name": sn, "scale_desc": si["刻度值"]}
                for sn, si in config["刻度"].items()
            ]
            result.append({
                "dimension_id": self._dim_id(dim_name),
                "dimension_name": dim_name + "维度",
                "scales": scales,
            })
        return result

    def _protocol_dim_to_internal(self, dimensions: List[Dict]) -> Dict:
        """协议格式 dimensions → 内部 {维度名: [刻度名]}"""
        name_map = {d.get("dimension_name", "").replace("维度", ""): d for d in dimensions}
        result = {}
        for dim_name in self._DIMENSION_NAMES:
            dim = name_map.get(dim_name)
            if dim and dim.get("scales"):
                result[dim_name] = [s["scale_name"] for s in dim["scales"]]
        return result

    def _internal_to_protocol_output(self, combined: Dict) -> List[Dict]:
        """内部 {维度名: [刻度名]} → 协议输出 [{dimension_id, scales: [刻度名]}]"""
        result = []
        for dim_name in self._OUTPUT_ORDER:
            scales = combined.get(dim_name, [])
            if not scales:
                continue
            result.append({
                "dimension_id": self._dim_id(dim_name),
                "scales": scales,
            })
        return result

    def _internal_to_protocol_full(self, combined: Dict) -> List[Dict]:
        """内部 {维度名: [刻度名]} → 协议完整格式（带 scale_name/scale_desc）"""
        dims = self._parse_dimensions()
        result = []
        for dim_name in self._OUTPUT_ORDER:
            scales_selected = combined.get(dim_name, [])
            if not scales_selected:
                continue
            config = dims.get(dim_name, {})
            all_scales = config.get("刻度", {})
            scales = []
            for sn in scales_selected:
                si = all_scales.get(sn)
                if si:
                    scales.append({
                        "scale_id": self._scale_id(sn),
                        "scale_name": sn,
                        "scale_desc": si["刻度值"],
                    })
            if scales:
                result.append({
                    "dimension_id": self._dim_id(dim_name),
                    "dimension_name": dim_name + "维度",
                    "scales": scales,
                })
        return result

    def _compact_to_full(self, recommended: List[Dict]) -> List[Dict]:
        """Step 1 紧凑输出 → Step 2 完整 dimensions 格式"""
        dims = self._parse_dimensions()
        result = []
        for item in recommended:
            dim_id = item.get("dimension_id", "")
            dim_name = self._dim_name_by_id(dim_id) if dim_id else ""
            if dim_name not in dims:
                continue
            config = dims[dim_name]
            scales = []
            for scale_name in item.get("scales", []):
                scale_info = config["刻度"].get(scale_name, {})
                if not scale_info:
                    continue
                scales.append({
                    "scale_id": self._scale_id(scale_name),
                    "scale_name": scale_name,
                    "scale_desc": scale_info.get("刻度值", ""),
                })
            if scales:
                result.append({
                    "dimension_id": dim_id,
                    "dimension_name": dim_name + "维度",
                    "scales": scales,
                })
        return result

    # ==================== Step 1: 维度推荐 ====================

    def recommend_dimensions(self, requirement, selected_dimensions=None, total_dimensions=None) -> Dict:
        """
        维度推荐
        输入：用户需求、预选维度刻度、全量维度刻度
        输出：推荐维度刻度
        """
        try:
            user_internal = self._normalize_selected_dimensions(selected_dimensions or [])
            total_dims = total_dimensions if total_dimensions else self._md_to_protocol()

            llm_internal = self._llm_analyze(requirement, total_dims)
            combined = self._combine_results(user_internal, llm_internal)
            recommended = self._internal_to_protocol_output(combined)

            return {"recommended_dimensions": recommended}
        except Exception as e:
            return {"recommended_dimensions": []}

    def _normalize_selected_dimensions(self, selected: List[Dict]) -> Dict:
        """兼容新旧两种 selected_dimensions 格式 → 内部 {维度名: [刻度名]}"""
        # 新格式: [{dimension_id, dimension_name, scales: [{scale_name,...}]}]
        if selected and "scales" in selected[0]:
            return self._protocol_dim_to_internal(selected)
        # 旧格式: [{维度名称, 刻度类型}]
        result = {name: [] for name in self._DIMENSION_NAMES}
        for item in selected:
            dim_name = item.get("维度名称", "")
            scale = item.get("刻度类型", "")
            if dim_name in self._DIMENSION_DEFAULTS:
                result[dim_name] = [self._DIMENSION_DEFAULTS[dim_name]]
            elif dim_name in result and scale:
                if scale not in result[dim_name]:
                    result[dim_name].append(scale)
        for name in result:
            if not result[name]:
                def_val = self._DIMENSION_DEFAULTS.get(name, "")
                result[name] = [def_val] if def_val else []
        return result

    def _normalize_requirement(self, requirement) -> str:
        if isinstance(requirement, dict):
            return self._extract_req_fields(requirement)
        if isinstance(requirement, str):
            # 尝试解析 JSON 字符串
            try:
                parsed = json.loads(requirement)
                if isinstance(parsed, dict):
                    return self._extract_req_fields(parsed)
            except (json.JSONDecodeError, TypeError):
                pass
            return requirement
        return str(requirement)

    def _extract_req_fields(self, req: dict) -> str:
        parts = []
        for k in ["需求名称", "问题描迷", "目标"]:
            v = req.get(k, "")
            if v:
                parts.append(v)
        return "；".join(parts) if parts else json.dumps(req, ensure_ascii=False)

    def _llm_analyze(self, requirement, total_dimensions: List[Dict]) -> Dict:
        """LLM 分析选择维度（接收结构化需求，构建完整 prompt）"""
        # 解析需求
        req_dict = requirement if isinstance(requirement, dict) else {}
        if isinstance(requirement, str):
            try:
                req_dict = json.loads(requirement)
            except (json.JSONDecodeError, TypeError):
                req_dict = {}

        req_name = req_dict.get("需求名称", "")
        req_desc = req_dict.get("问题描迷", "")
        req_goal = req_dict.get("目标", "")
        req_constraints = req_dict.get("约束条件", [])
        req_stakeholders = req_dict.get("关联利益相关者", [])

        stakeholder_text = ""
        for sh in req_stakeholders:
            name = sh.get("名称", "")
            concerns = sh.get("关注点", [])
            if name and concerns:
                stakeholder_text += f"- {name}：{'、'.join(concerns)}\n"

        # 构建维度描述
        dim_descriptions = []
        for dim in total_dimensions:
            dim_name = dim.get("dimension_name", "")
            scales = dim.get("scales", [])
            scale_lines = [f"  - {s['scale_name']}: {s.get('scale_desc', '')}" for s in scales]
            dim_descriptions.append(f"### {dim_name}\n" + "\n".join(scale_lines))

        prompt = f"""
请根据以下应用需求，分析并选择最合适的知识图谱维度。

## 应用需求

### 需求名称
{req_name}

### 问题描述
{req_desc}

### 目标
{req_goal}

### 约束条件
{', '.join(req_constraints) if req_constraints else '无'}

### 关联利益相关者及其关注点
{stakeholder_text if stakeholder_text else '无'}

## 可选维度

{chr(10).join(dim_descriptions)}

## 输出格式

请以JSON格式输出，包含"方面"、"视角"、"时间"、"空间"、"抽象层级"五个一级节点及其对应的二级节点列表：

{{{{
  "方面": ["刻度名1", "刻度名2", ...],
  "视角": ["刻度名1", "刻度名2", ...],
  "时间": ["年月"],
  "空间": ["集团"],
  "抽象层级": ["逻辑"]
}}}}

注意：
1. 根据需求选择所有相关的方面和视角，不要遗漏
2. 时间、空间、抽象层级使用默认值即可
3. 请严格按照JSON格式输出，不要添加额外内容
"""
        try:
            response = run_llm(prompt, 0.2, 1000, self.args.LLM_API_KEY, self.args.LLM_TYPE)
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                return self._validate_dimensions(json.loads(json_match.group(0)))
        except Exception:
            pass
        return self._get_default_dimensions()

    def _validate_dimensions(self, dimensions: Dict) -> Dict:
        dims = self._parse_dimensions()
        result = {name: self._DIMENSION_DEFAULTS.get(name, [])[:] for name in self._DIMENSION_NAMES}
        for dim_name in self._DIMENSION_NAMES:
            selected = dimensions.get(dim_name)
            if not isinstance(selected, list):
                continue
            valid = list(dims.get(dim_name, {}).get("刻度", {}).keys())
            filtered = [s for s in selected if s in valid]
            if filtered:
                result[dim_name] = filtered
            elif dim_name in self._DIMENSION_DEFAULTS:
                result[dim_name] = [self._DIMENSION_DEFAULTS[dim_name]]
        return result

    def _get_default_dimensions(self) -> Dict:
        return {
            "方面": ["运营", "战略"],
            "视角": ["状态", "结构", "约束"],
            "时间": ["年月"], "空间": ["集团"], "抽象层级": ["逻辑"],
        }

    def _combine_results(self, user: Dict, llm: Dict) -> Dict:
        combined = {}
        for dim_name in self._DIMENSION_NAMES:
            merged = []
            for item in user.get(dim_name, []) + llm.get(dim_name, []):
                if item not in merged:
                    merged.append(item)
            combined[dim_name] = merged
        return combined

    # ==================== Step 2: 空间草稿 ====================

    def _generate_root_name(self, requirement) -> str:
        """使用 LLM 根据需求生成简洁的根空间名称，如 产业链-智能问数"""
        req_dict = requirement if isinstance(requirement, dict) else {}
        if isinstance(requirement, str):
            try:
                req_dict = json.loads(requirement)
            except (json.JSONDecodeError, TypeError):
                req_dict = {}

        req_name = req_dict.get("需求名称", "")
        req_desc = req_dict.get("问题描迷", "")
        req_goal = req_dict.get("目标", "")

        if not req_name and not req_desc:
            return "根空间"

        prompt = f"""
请根据以下需求信息，生成一个简洁的知识空间名称。

要求：
- 8个字以内，格式如 "产业链-智能问数"、"运维-故障诊断"、"采购-数据看板"
- 使用"领域-方向"的格式
- 只输出名称，不要额外内容

需求名称：{req_name}
问题描述：{req_desc}
目标：{req_goal}
"""

        try:
            response = run_llm(prompt, 0.2, 30, self.args.LLM_API_KEY, self.args.LLM_TYPE)
            name = response.strip().strip('"').strip("'").strip("。").strip()
            if name and len(name) <= 20:
                return name
        except Exception:
            pass

        # 兜底：用需求名前20字
        fallback = req_name or req_desc or "根空间"
        return fallback[:20]

    def generate_space_draft(self, requirement, dimensions: List[Dict]) -> Dict:
        """
        空间草稿
        输入：用户需求、维度刻度
        输出：知识空间草稿
        """
        try:
            internal = self._protocol_dim_to_internal(dimensions)
            root_name = self._generate_root_name(requirement)

            # 取固定的时间/空间/抽象层级刻度
            time_scales = internal.get("时间", [self._DIMENSION_DEFAULTS["时间"]])
            space_scales = internal.get("空间", [self._DIMENSION_DEFAULTS["空间"]])
            abstract_scales = internal.get("抽象层级", [self._DIMENSION_DEFAULTS["抽象层级"]])
            aspects = internal.get("方面", []) or ["默认"]
            perspectives = internal.get("视角", []) or ["默认"]

            # 构建前缀：固定的三维度
            prefix_parts = []
            for dim_name, scales in [("时间", time_scales), ("空间", space_scales), ("抽象层级", abstract_scales)]:
                for s in scales:
                    prefix_parts.append(f"{dim_name}.{s}")
            prefix = "+".join(prefix_parts) + "+" if prefix_parts else ""

            children = []
            for aspect in aspects:
                for perspective in perspectives:
                    name = f"{prefix}视角.{perspective}+方面.{aspect}"
                    children.append({"space_name": name})

            workspace_tree = [{
                "space_name": root_name,
                "children": children,
            }]

            return {"workspace_tree": workspace_tree}
        except Exception as e:
            return {"workspace_tree": []}


    # ==================== 兼容旧接口 ====================

    def initialize_knowledge_space(self, app_requirement: Dict, user_selected_dimensions: List[Dict]) -> Dict:
        """旧接口兼容"""
        try:
            # 旧格式 selected_dimensions 直接透传，由 _normalize_selected_dimensions 处理
            result = self.recommend_dimensions(app_requirement, user_selected_dimensions)
            recommended = result.get("recommended_dimensions", [])
            dims_full = self._compact_to_full(recommended)
            combined = self._protocol_dim_to_internal(dims_full)
            dims = self._parse_dimensions()
            dimension_set = []
            for dim_name in self._OUTPUT_ORDER:
                config = dims.get(dim_name)
                if not config:
                    continue
                scales = [{"刻度名称": sn, "刻度值": config["刻度"][sn]["刻度值"]}
                          for sn in combined.get(dim_name, []) if sn in config.get("刻度", {})]
                if scales:
                    dimension_set.append({"维度名称": dim_name, "维度类型": config["维度类型"], "维度描述": config["维度描述"], "包含刻度": scales})
            return {"初始维度集合": dimension_set}
        except Exception:
            return {"初始维度集合": []}

    def _dim_name_by_id(self, dim_id: str) -> str:
        for name, did in self._ID_MAP.items():
            if did == dim_id and name in self._DIMENSION_NAMES:
                return name
        return dim_id

    def _scale_name_by_id(self, scale_id: str) -> str:
        for name, sid in self._ID_MAP.items():
            if sid == scale_id and name not in self._DIMENSION_NAMES:
                return name
        return scale_id
