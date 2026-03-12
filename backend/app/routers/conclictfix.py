import sys
import os
import re
import logging
from pathlib import Path
from fastapi import APIRouter, HTTPException
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
# 🎯 动态路径挂载 (核心导航)
# ==========================================
CURRENT_FILE_DIR = Path(__file__).resolve().parent # 此时在 backend/app/routers
PROJECT_ROOT = CURRENT_FILE_DIR.parent.parent.parent # 追溯到 /mnt/data3/conclictfix
ALGO_ROOT = PROJECT_ROOT / "algorithm" / "conflict_detection"

# 只要把这个路径塞进 sys.path，src 文件夹就变成了可导入的包
if str(ALGO_ROOT) not in sys.path:
    sys.path.insert(0, str(ALGO_ROOT))

# ==========================================
#  统一引用 (SSOT: 唯一事实来源)
# ==========================================
try:
    # 🎯 所有的“弹药”都从指挥中心 (config) 领
    from src.config import NEO4J_URI, NEO4J_USER, NEO4J_PWD, COMMON_PREFIXES
    from src.core import UpSHACLRepairManager
except ImportError as e:
    logger.critical(f"❌ 关键配置导入失败，请检查 sys.path 或路径: {ALGO_ROOT}")
    raise e

# 🎯 初始化逻辑
router = APIRouter(prefix="/api/conflict", tags=["冲突检测与修复"])
manager = UpSHACLRepairManager()

# ❌ 原本那几行 COMMON_PREFIXES 的定义直接人间蒸发即可

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
#  3. 核心工具函数 (灵活与通用)
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
#  4. 冲突检测接口 (零写死，动态比对)
# ==========================================
@router.post("/detect")
async def detect_conflict(req: DetectRequest):
    try:
        input_ttl = req.insert_ttl
        shape_path = ALGO_ROOT / "data" / "shape.ttl"
        shape_ttl = shape_path.read_text(encoding='utf-8') if shape_path.exists() else ""

        # 1. 执行精准推演
        result = manager.run_logic_stream(
            insert_ttl=input_ttl, 
            delete_ttl="", 
            shape_ttl=shape_ttl, 
            shape_lp="", 
            strategy_logic="STRICT"
        )
        
        violated_details = result.get("violated_details", [])
        safe_actions = result.get("safe_actions", [])

        # 2.  静默入库：无辜属性立刻进库
        if safe_actions:
            manager.execute_sync_to_db(safe_actions)

        # 3.  精准组装：只展示真正报错的属性
        # 3.  精准组装：展示真正报错的属性及其真实新值
        conflict_groups = []
        if violated_details:
            #  第一步：先把输入的 TTL 转化成方便查询的字典
            # 确保这个方法在你文件里（之前重构版里有）
            new_data_map = extract_node_data(input_ttl) 
            
            driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PWD))
            with driver.session() as session:
                for item in violated_details:
                    s_id, p_id = item["subject"], item["predicate"]
                    
                    #  第二步：从输入数据里抓取那个“犯错”的真实新值
                    # 这样就不会显示“待修正”，而是显示具体的 600
                    new_val_data = new_data_map.get(s_id, {}).get(p_id, {})
                    actual_new_val = new_val_data.get("value", "未知")
                    is_rel = new_val_data.get("is_rel", False)

                    #  第三步：查旧值 (带上反引号保护，解决你刚才的 SyntaxError)
                    # 注意这里的 `{p_id}` 两边加了反引号 `` ` ``
                    db_res = session.run(f"MATCH (n {{id: '{s_id}'}}) RETURN n.`{p_id}` AS old_v").single()
                    old_val = str(db_res["old_v"]) if db_res and db_res["old_v"] is not None else "无记录"

                    # 判定冲突类型文案
                    if p_id == "type":
                        c_type = "实体标签冲突"
                    elif is_rel:
                        c_type = "关系连线冲突"
                    else:
                        c_type = "属性数值冲突"

                    conflict_groups.append({
                        "subject": s_id,
                        "predicate": f"{p_id}",
                        "conflictType": c_type,
                        "oldValue": old_val,
                        "newValue": actual_new_val, #  现在的 600 回来了！
                        "selectedResolution": "overwrite",
                        "customValue": "",
                        "operation": "add"
                    })
            driver.close()

        return {"success": True, "conflicts": conflict_groups}

    except Exception as e:
        logger.error(f" 检测失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
#  5. 手动修复接口
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