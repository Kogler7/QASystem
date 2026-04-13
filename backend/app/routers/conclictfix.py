import sys
import os
import re
import json
import logging
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from rdflib import Graph, URIRef
from neo4j import GraphDatabase

# =========================================
#  0. 工业级日志配置
# =========================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("API_ConflictFix")

# ==========================================
#路径挂载
# ==========================================
CURRENT_FILE_DIR = Path(__file__).resolve().parent 
PROJECT_ROOT = CURRENT_FILE_DIR.parent.parent.parent 
ALGO_ROOT = PROJECT_ROOT / "algorithm" / "conflict_detection"


if str(ALGO_ROOT) not in sys.path:
    sys.path.insert(0, str(ALGO_ROOT))


try:
    from src.config import NEO4J_URI, NEO4J_USER, NEO4J_PWD, COMMON_PREFIXES
    from src.core import UpSHACLRepairManager
except ImportError as e:
    logger.critical(f"❌ 关键配置导入失败，请检查 sys.path 或路径: {ALGO_ROOT}")
    raise e

from ..deps.auth import require_admin

router = APIRouter(
    prefix="/api/conflict",
    tags=["冲突检测与修复"],
    dependencies=[Depends(require_admin)],
)
manager = UpSHACLRepairManager()

class DetectRequest(BaseModel):
    insert_ttl: str = Field(default="", description="拟插入的 RDF Turtle 数据")
    delete_ttl: str = Field(default="", description="拟删除的 RDF Turtle 数据")

class RepairAction(BaseModel):
    subject: str
    predicate: str
    action_type: str = Field(..., description="枚举: overwrite, keep, manual")
    value: str
    customValue: Optional[str] = ""

class RepairRequest(BaseModel):
    actions: List[RepairAction]

# ==========================================
#  3. 核心工具函数
# ==========================================
def clean_id(text: str) -> str:
    """万能 ID 清洗器：剥离所有命名空间和多余符号"""
    if not text: return ""
    return text.split("/")[-1].split("#")[-1].replace("ex:", "").replace('"', '').strip()

def extract_node_data(ttl_content: str) -> Dict[str, Dict[str, Any]]:
    """将 TTL 转化为 { subject: { predicate: { value: obj, is_uri: bool } } } 的通用字典"""
    g = Graph()
    g.parse(data=ttl_content, format="turtle")
    data_map = {}
    for s, p, o in g:
        subj = clean_id(str(s))
        pred = clean_id(str(p))
        if not subj or subj.startswith("http"): continue
        
        if subj not in data_map: data_map[subj] = {}
        data_map[subj][pred] = {
            "value": clean_id(str(o)),
            "is_uri": isinstance(o, URIRef)
        }
    return data_map

# ==========================================
#  4. 冲突检测接口 
# ==========================================
@router.post("/detect")
async def detect_conflict(req: DetectRequest):
    try:
        json_data = json.loads(req.insert_ttl)
        # 1. 获取安全的 TTL 和映射表
        input_ttl, new_data_map = manager.process_and_map_data(json_data)

        # 2. 读取规则
        shape_path = ALGO_ROOT / "data" / "shape.ttl"
        lp_path = ALGO_ROOT / "data" / "shape.lp"
        shape_ttl_content = shape_path.read_text(encoding='utf-8') if shape_path.exists() else ""
        shape_lp_content = lp_path.read_text(encoding='utf-8') if lp_path.exists() else ""
        
        # 3. 喂给推演引擎（现在的 input_ttl 全是 ex:n_G005，引擎吃得极香）
        result = manager.run_logic_stream(
            insert_ttl=input_ttl, 
            delete_ttl="", 
            shape_ttl=shape_ttl_content, 
            shape_lp=shape_lp_content,   
            strategy_logic="STRICT"      
        )
        
        violated_details = result.get("violated_details", [])
        safe_actions = result.get("safe_actions", [])

        # 4. 静默入库 (引擎认为绝对安全的直接入库)
        if safe_actions:
            manager.execute_sync_to_db(safe_actions)

        # 5. 精准组装和动态脱壳
        conflict_groups = []
        
        # --- [新增：业务翻译字典] 把生硬的英文转成能看懂的中文 ---
        BUSINESS_MAP = {
            "VersionInformation": "规程版本效力",
            "HAS_CONSTRAINTRULETYPE": "关联约束规则内容",
            "HAS_EXAMPLES": "关联示例数据",
            "Terminology": "所属规程/术语",
            "constraint": "具体约束条款"
        }

        if violated_details:
            driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PWD))
            seen_conflicts = set() 
            # 提前把 nodes 数组拿出来，方便后面去挖文字内容
            graph_nodes = json_data.get("graph", {}).get("nodes", []) 
            
            with driver.session() as session:
                for item in violated_details:
                    raw_s = clean_id(str(item["subject"]))
                    p_id = clean_id(str(item["predicate"]))
                    
                    # --- [核心修复 1：撕下面具，还原真实 ID] ---
                    if raw_s.startswith("n_"):
                        real_s_id = raw_s[2:]
                    else:
                        real_s_id = raw_s

                    conflict_key = f"{real_s_id}_{p_id}"
                    if conflict_key in seen_conflicts:
                        continue
                    seen_conflicts.add(conflict_key)
                    
                    # --- [翻译：拿中文名] ---
                    p_display_name = BUSINESS_MAP.get(p_id, p_id)
                    
                    # --- [预判冲突类型] ---
                    c_type = "属性数值冲突"
                    is_rel = False
                    if p_id == "type":
                        c_type = "实体标签冲突"
                    elif "HAS_" in p_id or "INSTANCE_OF" in p_id:
                        c_type = "关系连线冲突"
                        is_rel = True

                    # --- [获取新值逻辑] ---
                    safe_s_key = f"n_{real_s_id}"
                    node_props = new_data_map.get(safe_s_key, {})
                    actual_new_val = node_props.get(p_id, "未知")
                    
                    if actual_new_val == "未知" and not is_rel:
                        for k, v in node_props.items():
                            if clean_id(str(k)) == p_id:
                                actual_new_val = v
                                break

                    # 🔥 [核心修复 2：深度内容穿透！只抓文字，不要数字 ID！] 🔥
                    if is_rel or actual_new_val == "未知":
                        graph_rels = json_data.get("graph", {}).get("relationships", [])
                        for rel in graph_rels:
                            if str(rel.get("from_uid")) == real_s_id and str(rel.get("type")) == p_id:
                                target_id = str(rel.get("to_uid"))
                                
                                # 拿着这个 ID，去 nodes 里面找它真正的内容！
                                target_node = next((n for n in graph_nodes if str(n["uid"]) == str(target_id)), None)
                                if target_node:
                                    t_props = target_node.get("properties", {})
                                    # 优先抓长文本 constraint，没有就抓 RuleName，再没有才用 ID 兜底
                                    actual_new_val = t_props.get("constraint") or t_props.get("RuleName") or t_props.get("name") or target_id
                                else:
                                    actual_new_val = target_id
                                break

                    # 脱壳，去除 str_ 前缀
                    actual_new_val_str = str(actual_new_val)
                    if actual_new_val_str.startswith("str_"):
                        actual_new_val = actual_new_val_str[4:] 

                    # --- [用真实 ID 查数据库获取旧值] ---
                    db_res = session.run(
                        f"MATCH (n {{id: '{real_s_id}'}}) RETURN n.`{p_id}` AS old_v"
                    ).single()
                    old_val = str(db_res["old_v"]) if db_res and db_res["old_v"] is not None else "无记录"

                    # 提取友好的中文名称 (拿不到 RuleName 就用真实 ID)
                    display_name = node_props.get("RuleName", node_props.get("name", real_s_id))

                    conflict_groups.append({
                        "subject": real_s_id,          
                        "subjectName": display_name,   
                        "predicate": p_id,
                        "predicateName": p_display_name, # 【新增】带过去的中文名
                        "conflictType": c_type,
                        "isRelation": is_rel,            # 【新增】传给前端做判断
                        "oldValue": old_val,
                        "newValue": actual_new_val,      # 【修复】这里彻底变成了文字内容！
                        "selectedResolution": "overwrite",
                        "customValue": "",
                        "operation": "add"
                    })
            driver.close()

        return {"success": True, "conflicts": conflict_groups}

    except Exception as e:
        import traceback
        logger.error(f"🚨 检测失败堆栈: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"检测引擎异常: {str(e)}")

# ==========================================
#  5. 修复接口
# ==========================================
@router.post("/repair")
async def repair_conflict(req: RepairRequest):
    try:
        repair_actions = []
        for item in req.actions:
            if item.action_type == "keep": 
                continue
            
            s = clean_id(item.subject)
            p = clean_id(item.predicate)
            raw_val = item.value if item.action_type != "manual" else item.customValue
            if not raw_val: 
                continue
            
            # 动态格式化：纯数字不加引号，字符串必须加引号，供 Core 层解析
            val_str = raw_val if str(raw_val).replace('.', '', 1).isdigit() else f'"{raw_val}"'
            repair_actions.append(f'add("{s}","{p}",{val_str})')

        if repair_actions:
            logger.info(f" 提交手动修复动作: {repair_actions}")
            manager.execute_sync_to_db(repair_actions)

        return {"success": True, "message": f"成功应用 {len(repair_actions)} 个修复动作"}
    except Exception as e:
        logger.error(f" 修复执行崩溃: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))