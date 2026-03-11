import json
import logging
from openai import OpenAI
from src.config import config

logger = logging.getLogger(__name__)

class LocalLLMTranslator:
    def __init__(self):
        # 🎯 使用你定义的通用本地变量名
        self.client = OpenAI(
            api_key=config.LOCAL_LLM_KEY,
            base_url=config.LOCAL_LLM_URL
        )
        self.model = config.LOCAL_MODEL_NAME

    def translate_to_logic(self, raw_json):
        print(f"🤖 正在调用本地模型 [{self.model}] 提取逻辑...")
        
        # 提示词建议引导模型输出干净的 JSON
        prompt = f"请将以下规程 JSON 提取为逻辑三元组格式：{json.dumps(raw_json, ensure_ascii=False)}"
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是一个知识图谱专家。请直接返回 JSON 结果，不要包含任何解释文字。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=config.TEMPERATURE
            )

            content = response.choices[0].message.content
            return json.loads(content)

        except Exception as e:
            logger.error(f"❌ 本地模型调用失败: {e}")
            return None