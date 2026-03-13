import re
import json
from typing import Dict, List, Tuple, Any
from utils import run_llm


class EntityLinker:
    def __init__(self, wiki_client, args):
        self.wiki_client = wiki_client
        self.args = args
        self.llm_cache = {}

        self.entity_types = [
            "物品/产品", "人物", "组织/机构", "地点/位置",
            "时间/日期", "事件", "概念/术语", "学科/领域"
        ]

    def recognize_entities(self, question: str) -> List[str]:
        """使用LLM理解问题并提取相关实体"""
        cache_key = f"ner_zh_{question}"
        if cache_key in self.llm_cache:
            return self.llm_cache[cache_key]

        prompt = self._build_entity_recognition_prompt(question)

        try:
            response = run_llm(prompt, 0.1, 300, self.args.LLM_API_KEY, self.args.LLM_TYPE)

            entities = self._parse_llm_response(question, response)

            # 如果还没有实体，使用fallback方法
            if not entities:
                entities = self._fallback_extraction(question)

            print(f"问题: '{question}' -> 提取的实体: {entities}")
            self.llm_cache[cache_key] = entities
            return entities

        except Exception as e:
            print(f"实体识别异常: {e}")
            return self._fallback_extraction(question)

    # def recognize_entities(self, question: str) -> List[str]:
    #     """使用LLM理解问题并提取相关实体"""
    #     cache_key = f"ner_zh_{question}"
    #     if cache_key in self.llm_cache:
    #         return self.llm_cache[cache_key]
    #
    #     prompt = self._build_entity_recognition_prompt(question)
    #
    #     try:
    #         response = run_llm(prompt, 0.1, 100, self.args.LLM_API_KEY, self.args.LLM_TYPE)
    #
    #         # 清理响应
    #         response = self._clean_think_tags(response)
    #         print(f"LLM响应: {response}")
    #
    #         # 简单的解析：取第一行或最后一行
    #         lines = [line.strip() for line in response.split('\n') if line.strip()]
    #
    #         entities = []
    #         if lines:
    #             # 尝试找到包含逗号的行
    #             for line in lines:
    #                 if ',' in line:
    #                     entities = [e.strip() for e in line.split(',') if e.strip()]
    #                     break
    #
    #             # 如果没有逗号，使用第一行
    #             if not entities:
    #                 entities = [lines[0]]
    #
    #         # 过滤无效实体
    #         filtered_entities = []
    #         for entity in entities:
    #             if (len(entity) >= 2 and len(entity) <= 10 and
    #                     entity not in ['什么', '谁', '哪里', '如何', '为什么']):
    #                 filtered_entities.append(entity)
    #
    #         # 如果还是没有，使用备用方法
    #         if not filtered_entities:
    #             filtered_entities = self._fallback_extraction(question)
    #
    #         print(f"问题: '{question}' -> 提取的实体: {filtered_entities}")
    #         self.llm_cache[cache_key] = filtered_entities
    #         return filtered_entities
    #
    #     except Exception as e:
    #         print(f"实体识别异常: {e}")
    #         return self._fallback_extraction(question)

    # def _clean_think_tags(self, response: str) -> str:
    #     """清理 <think> 标签"""
    #     import re
    #     # 移除 <think> 和 </think> 标签及其内容
    #     cleaned = re.sub(r'<think>.*?</think>', '', response, flags=re.DOTALL)
    #     # 移除可能漏掉的 <think> 标签
    #     cleaned = re.sub(r'<think>', '', cleaned)
    #     cleaned = re.sub(r'</think>', '', cleaned)
    #     return cleaned.strip()

    def _build_entity_recognition_prompt(self, question: str) -> str:
        """构建实体识别提示词"""
        return f"""
        你是一个专业的实体提取专家。请分析以下问题，提取出所有对回答问题有帮助的实体。

        问题："{question}"

        分析要求：
        1. 理解问题的意图和上下文
        2. 识别问题中提到的所有具体事物、概念、人物、地点等
        3. 对于模糊的实体，根据上下文确定最可能的含义
        4. 特别注意物品、工具、设备等具体事物

        实体提取标准：
        - 名词性短语（如"瑜伽垫"、"跑步机"）
        - 专有名词（如"姚明"、"北京"）
        - 概念术语（如"人工智能"、"量子力学"）
        - 事件活动（如"奥运会"、"春节"）

        输出格式：请用严格的JSON格式输出：
        {{
          "analysis": "简短分析问题的意图",
          "entities": [
            {{
              "text": "实体文本",
              "type": "实体类型",
              "importance": "高/中/低",
              "reason": "为什么提取这个实体"
            }}
          ]
        }}

        示例：
        问题："谁使用瑜伽垫？"
        输出：
        {{
          "analysis": "这个问题询问瑜伽垫的使用者，需要找到使用瑜伽垫的人或群体",
          "entities": [
            {{
              "text": "瑜伽垫",
              "type": "物品/产品",
              "importance": "高",
              "reason": "这是问题的核心对象，所有回答都围绕瑜伽垫展开"
            }}
          ]
        }}
        """

    def _parse_llm_response(self, question: str, response: str) -> List[str]:
        """解析LLM响应，提取实体"""
        entities = []
        # response = self._clean_think_tags(response)
        #尝试解析JSON
        json_data = self._extract_json_from_response(response)
        if json_data:
            entities = self._parse_json_entities(json_data, question)

        #如果没有JSON，尝试自由文本解析
        if not entities:
            entities = self._parse_free_text_entities(response, question)

        #后处理
        entities = self._post_process_entities(entities, question)

        return entities

    def _extract_json_from_response(self, response: str) -> Dict:
        """从响应中提取JSON数据"""
        try:
            # response = self._clean_think_tags(response)
            # 尝试多种JSON匹配模式
            patterns = [
                r'```json\n(.*?)\n```',  # 代码块
                r'```\n(.*?)\n```',  # 通用代码块
                r'\{.*"entities".*\}',  # 包含entities的JSON
                r'\{[\s\S]*\}',  # 任何JSON对象
            ]

            for pattern in patterns:
                match = re.search(pattern, response, re.DOTALL)
                if match:
                    json_str = match.group(1) if pattern.startswith('```') else match.group(0)
                    # 清理可能的markdown标记
                    json_str = json_str.strip()
                    # 尝试解析
                    data = json.loads(json_str)
                    if isinstance(data, dict) and "entities" in data:
                        return data
        except Exception as e:
            print(f"JSON提取失败: {e}")

        return None

    def _parse_json_entities(self, json_data: Dict, question: str) -> List[str]:
        """从JSON数据中解析实体"""
        entities = []

        try:
            if "entities" in json_data:
                for entity_info in json_data["entities"]:
                    if isinstance(entity_info, dict):
                        # 尝试多个可能的字段名
                        text_fields = ["text", "entity", "name", "mention", "value"]
                        for field in text_fields:
                            if field in entity_info:
                                entity_text = str(entity_info[field]).strip()
                                if entity_text and len(entity_text) >= 2:
                                    # 验证实体是否确实在问题中
                                    if self._validate_entity_in_question(entity_text, question):
                                        entities.append(entity_text)
                                    break
        except Exception as e:
            print(f"解析JSON实体失败: {e}")

        return entities

    def _parse_free_text_entities(self, response: str, question: str) -> List[str]:
        """从自由文本中解析实体"""
        entities = []

        #查找列表格式
        list_patterns = [
            r'实体[：:]\s*(.*?)(?:\n|$)',
            r'[0-9]+[\.、]?\s*(.*?)(?:\n|$)',
            r'- (.*?)(?:\n|$)',
        ]

        for pattern in list_patterns:
            matches = re.findall(pattern, response, re.MULTILINE)
            for match in matches:
                if match and len(match.strip()) >= 2:
                    entity = match.strip()
                    if self._validate_entity_in_question(entity, question):
                        entities.append(entity)

        #查找中文短语
        chinese_phrases = re.findall(r'[\u4e00-\u9fa5]{2,12}', response)
        for phrase in chinese_phrases:
            # 过滤常见词，但不过滤物品名词
            if (self._validate_entity_in_question(phrase, question) and
                    not self._is_common_word(phrase)):
                entities.append(phrase)

        return list(set(entities))

    def _validate_entity_in_question(self, entity: str, question: str) -> bool:
        """验证实体是否在问题中出现（或语义相关）"""
        # 直接包含
        if entity in question:
            return True

        # 部分包含（对于长实体）
        if len(entity) > 3 and entity in question:
            return True

        # 对于短实体，确保不是常见虚词
        if len(entity) < 4:
            common_words = {"什么", "谁", "哪里", "如何", "为什么", "怎样", "多少"}
            if entity in common_words:
                return False

        # 对于模糊匹配，可以添加更多逻辑
        return True

    def _is_common_word(self, word: str) -> bool:
        """判断是否为常见词"""
        common_words = {
            "问题", "实体", "识别", "提取", "分析", "需要", "根据",
            "以下", "输出", "格式", "类型", "包括", "不要", "如果",
            "任何", "没有", "这个", "那个", "这些", "那些", "一种",
            "一个", "一些", "什么", "如何", "为什么", "怎样"
        }
        return word in common_words

    def _post_process_entities(self, entities: List[str], question: str) -> List[str]:
        """实体后处理"""
        if not entities:
            return []

        # 去重
        unique_entities = []
        seen = set()

        for entity in entities:
            entity = entity.strip()
            # 过滤条件
            if (entity and
                    len(entity) >= 2 and
                    entity not in seen and
                    not self._is_common_word(entity)):
                seen.add(entity)
                unique_entities.append(entity)

        # 排序：按在问题中出现的顺序
        sorted_entities = sorted(
            unique_entities,
            key=lambda x: question.find(x) if x in question else len(question)
        )

        return sorted_entities

    def _fallback_extraction(self, question: str) -> List[str]:
        """备用提取方法"""
        entities = []

        # 提取所有名词短语（中文）
        # 使用更智能的提取：提取连续的中文字符，排除常见虚词
        words = re.findall(r'[\u4e00-\u9fa5]+', question)

        # 动态判断是否为实体
        for word in words:
            if len(word) >= 2:
                # 排除常见疑问词和虚词
                if word not in {"什么", "谁", "哪里", "如何", "为什么", "怎样", "多少",
                                "的", "了", "在", "和", "与", "或"}:
                    entities.append(word)

        return entities

    def link_entities_in_question(self, question: str) -> Dict[str, str]:
        """实体链接函数"""
        print(f"原始问题: {question}")
        # 提取实体
        entity_mentions = self.recognize_entities(question)
        print(f"提取的实体: {entity_mentions}")

        if not entity_mentions:
            print("警告：未识别到任何实体")
            # 尝试直接使用整个问题作为查询
            entity_mentions = [question]

        # 实体链接
        linked_entities = {}
        for mention in entity_mentions:
            print(f"\n处理实体: '{mention}'")

            try:
                qid = self._find_entity_in_knowledge_base(mention)

                if qid:
                    # 获取标签
                    label = self._get_entity_label(qid, mention)
                    linked_entities[qid] = label
                    print(f"链接成功: '{mention}' -> {qid} ({label})")
                else:
                    print(f"未找到实体: '{mention}'")

            except Exception as e:
                print(f"实体链接异常: {e}")
                import traceback
                traceback.print_exc()

        print(f"最终链接结果: {linked_entities}")

        return linked_entities

    def _find_entity_in_knowledge_base(self, entity: str) -> str:
        """在知识库中查找实体"""
        qids = set()

        #精确匹配
        query_exact = """
        MATCH (n {name: $name})
        RETURN n.name as name, id(n) as id
        LIMIT 1
        """

        with self.wiki_client.client.driver.session() as session:
            # 精确匹配
            result = session.run(query_exact, name=entity)
            for record in result:
                qids.add(f"NEO_{record['id']}")

            # 如果没找到，尝试模糊匹配
            if not qids:
                query_fuzzy = """
                MATCH (n)
                WHERE n.name CONTAINS $keyword OR 
                      toLower(n.name) CONTAINS toLower($keyword)
                RETURN n.name as name, id(n) as id
                ORDER BY 
                  CASE WHEN n.name = $keyword THEN 0
                       WHEN n.name STARTS WITH $keyword THEN 1
                       ELSE 2 END
                LIMIT 5
                """

                result = session.run(query_fuzzy, keyword=entity)
                for record in result:
                    qids.add(f"NEO_{record['id']}")

        # 使用标准接口
        if not qids:
            try:
                standard_qids = self.wiki_client.query_all("label2qid", entity)
                if standard_qids and "Not Found!" not in standard_qids:
                    for qid in standard_qids:
                        if qid.startswith("NEO_"):
                            qids.add(qid)
            except:
                pass

        return next(iter(qids)) if qids else None

    def _get_entity_label(self, qid: str, default: str) -> str:
        """获取实体标签"""
        try:
            labels = self.wiki_client.query_all("qid2label", qid)
            if labels and "Not Found!" not in labels:
                return next(iter(labels))
        except:
            pass

        return default