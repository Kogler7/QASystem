
from typing import List, Dict, Optional, Any, Union
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
    问题ID: str
    问题描述: str
    问题类型: ProblemType
    约束: List[str]
    目标对象: str
    干系人: List[str]
    目标: List[str]
    当前状态: str
    创建时间: Optional[str] = None
    相关实体: Optional[List[str]] = None  # 从问题中提取的实体

# ========== 方案本体 ==========
class SolutionCategory(str, Enum):
    EVALUATION = "评估类问题解决方案"
    DIAGNOSTIC = "诊断类问题解决方案"
    PLANNING = "规划类问题解决方案"
    EXECUTION = "执行类问题解决方案"
    ANALYSIS = "分析类问题解决方案"

class Action(BaseModel):
    """动作定义 - 对应ICOM中的Action"""
    动作名称: str
    动作描述: Optional[str] = None
    输入: List[str]  # 需要的输入参数
    输出: List[str]  # 产生的输出
    前置条件: Optional[List[str]] = None
    后置条件: Optional[List[str]] = None

class SolutionModel(BaseModel):
    """方案本体模型 - 对应文档中的方案表格"""
    方案ID: str
    方案类别: SolutionCategory
    方案目标: str
    输入: List[str]  # 方案整体的输入
    输出: List[str]  # 方案整体的输出
    约束: List[str]
    控制逻辑: Optional[str] = None  # IF-THEN-ELSE逻辑
    置信度: Optional[float] = None  # 方案匹配的置信度

# ========== 问题-方案匹配模型 ==========
class ProblemSolutionMatch(BaseModel):
    """问题与方案的匹配关系"""
    问题ID: str
    方案ID: str
    匹配度: float  # 0-1
    匹配理由: str
    子问题匹配: Optional[List[Dict[str, str]]] = None  # 子问题与子方案的对应

# ========== 执行计划 ==========
class ComponentCall(BaseModel):
    """物理组件调用"""
    逻辑动作: str
    物理组件: str  # 组件名称/ID
    组件类型: str  # API、模型、工具等
    调用参数: Dict[str, Any]
    预期输出: List[str]
    备选组件: Optional[List[str]] = None  # 失败时的备选

class ExecutionPlan(BaseModel):
    """执行计划"""
    方案ID: str
    组件调用序列: List[ComponentCall]
    整体输入: Dict[str, Any]
    预期输出: Dict[str, Any]
    回滚策略: Optional[str] = None