from pydantic import BaseModel, Field
from typing import Union


class Intent(BaseModel):
    intent_id: str = Field(..., description="意图唯一标识，如业务自定义ID，用于数据库主键/索引")
    version: str = Field(..., description="意图版本号，用于迭代管理")
    domain: str = Field(..., description="意图所属业务域，用于分类筛选")
    create_time: str = Field(..., description="意图创建时间（ISO8601格式），溯源用")
    update_time: str = Field(..., description="意图更新时间，版本管理用")
    status: str = Field(..., description="意图状态（active/archive/draft）")
    tags: list = Field(..., description="业务标签，用于分类检索（非语义检索）")
    intent_name: str = Field(..., description="意图名称（简洁描述），核心语义字段")
    intent_description: str = Field(..., description="意图详细描述，补充语义")
    sample_utterances: list = Field(..., description="意图的典型用户表述样本，多维度语义载体")
    core_keywords: list = Field(..., description="核心关键词")
    intent_summary: str = Field(..., description="意图精简摘要")
    user_goal: str = Field(..., description="用户核心目标")


class Intent_Base_Recognition(BaseModel):
    intent_id: str = Field(..., description="意图唯一标识，如业务自定义ID，用于数据库主键/索引")
    domain: str = Field(..., description="意图所属业务域，用于分类筛选")
    scenario: str = Field(..., description="意图所属场景")
    object: str = Field(..., description="意图对象")
    intent_name: str = Field(..., description="意图名称（简洁描述），核心语义字段")
    user_goal: str = Field(..., description="用户核心目标")