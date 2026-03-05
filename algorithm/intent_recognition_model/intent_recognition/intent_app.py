import datetime
import json
import logging
import os
import sys

from langchain_core.prompts import ChatPromptTemplate
from langchain_deepseek import ChatDeepSeek
from pydantic import BaseModel, Field
from config.config import Config
from utils.search_intent import HybridSearch
from intent_recognition.create_agent import create_agent


# 配置日志
config = Config()
log_path = config.log_path
if log_path:
    os.makedirs(log_path, exist_ok=True)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler(filename=os.path.join(log_path,"search.log"),encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


# 加载api_key
api_key = Config().api_key

model = ChatDeepSeek(
    model="deepseek-chat",
    api_key=api_key,
)

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


agent = create_agent(
    model=model,
    response_format=Intent,
    system_prompt="你是一个意图识别的专家，需要你根据用户输入的询问以及相关的意图列表选取最合适的意图"
)


class IntentRecognition:
    def __init__(self):
        self.model = model
        self.prompt_template = self.init_prompt_template()
        self.search_tool = HybridSearch()
        self.agent = agent

    def init_prompt_template(self):
        """初始化提示模板"""
        prompt_template = ChatPromptTemplate.from_messages(
            [
                ("system", "你是一个意图识别的专家，需要你根据用户输入的询问以及相关的意图列表选取最合适的意图,以下是意图列表：\n{intent}"),
                ("human", "我的问题是{query},请帮我筛选出最恰当的意图，并将它原封不动的输出"),
            ]
        )
        return prompt_template

    def retrival(self, query: str):
        """检索函数"""
        result = self.search_tool.search(query=query)
        final_result = []
        for r in result:
            # 直接提取entity字典，避免protobuf对象
            entity = r.get("entity")
            # 确保所有字段都是JSON可序列化的
            serializable_entity = self._make_serializable(entity)
            final_result.append(serializable_entity)
        return final_result

    def _make_serializable(self, obj):
        """递归转换非JSON可序列化对象"""
        if isinstance(obj, dict):
            return {k: self._make_serializable(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [self._make_serializable(i) for i in obj]
        elif isinstance(obj, (datetime.datetime, datetime.date)):
            return obj.isoformat()
        elif hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
            # 处理protobuf的RepeatedScalarContainer等可迭代对象
            try:
                return list(obj)
            except TypeError:
                return str(obj)
        else:
            return obj

    def generate_result(self, query: str):
        """答案生成函数（改造后：content转字典）"""
        logger.info("正在进行答案生成...")
        retrival_result = self.retrival(query=query)

        prompt = self.prompt_template.format_messages(
            query=query,
            intent=retrival_result,
        )

        # 调用大模型
        result = self.agent.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": f"我的问题是{query},请帮我筛选出最恰当的意图，并将它原封不动的输出,以下是意图列表：\n{retrival_result}"
                    }
                ]
            }
        )
        print(type(result))
        return result["structured_response"]

if __name__ == "__main__":
    intent_recognition = IntentRecognition()
    query = "发现裂纹后应如何处理？"
    logger.info(f"query: {query}")
    result = intent_recognition.generate_result(query)
    logger.info(f"result: {result}")



