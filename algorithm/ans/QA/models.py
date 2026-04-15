
from typing import List, Dict, Optional
from enum import Enum
from pydantic import BaseModel
from datetime import datetime

# ========== 问题本体 ==========
class ProblemType(str, Enum):
    DESCRIPTIVE = "描述型问题"
    DIAGNOSTIC = "诊断型问题"
    PREDICTIVE = "预测型问题"
    PRESCRIPTIVE = "指导型问题"
    EVALUATION = "评估型问题"
    PLANNING = "规划型问题"

class ProblemModel(BaseModel):
    """问题本体模型 - 对应文档中的表格"""
    problem_id: str
    description: str
    problem_type: ProblemType
    constraints: List[str]
    target_object: str
    stakeholders: List[str]
    goals: List[str]
    status: str
    created_at: Optional[str] = None
    related_entities: Optional[List[str]] = None
    children: Optional[List[Dict]] = None