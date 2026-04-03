import re
import json
from typing import Dict, List, Tuple, Any
from algorithm.ans.QA.utils import run_llm


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
        5. **重要**：对于需要计算或对比的问题，请根据业务逻辑推断可能需要的实体

        实体提取标准：
        - 名词性短语（如"瑜伽垫"、"跑步机"）
        - 专有名词（如"姚明"、"北京"）
        - 概念术语（如"人工智能"、"量子力学"）
        - 事件活动（如"奥运会"、"春节"）
        - **业务逻辑推断**：如果问题需要计算或对比，请推断可能需要的实体
          - 例如：如果问题包含"非公开采购金额"，请推断需要"电子采购中标金额"和"公开采购中标金额"
          - 例如：如果问题包含"A和B的对比"，请提取A和B相关的所有实体

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

        注意：
        - 只提取问题中实际提到的实体或根据业务逻辑推断的实体
        - 确保JSON格式正确，不要有多余的逗号或括号
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
                r'\{.*"rankings".*\}',  # 包含rankings的JSON
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
                    if isinstance(data, dict):
                        # 支持多种字段名
                        if "entities" in data or "rankings" in data:
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

    def search_entities_with_full_info(self, keyword: str, limit: int = 10) -> List[Dict]:
        """
        灵活搜索：遍历所有属性，返回匹配的节点及其完整信息
        让大模型根据完整信息判断相关性
        """
        try:
            # 检查wiki_client的类型
            driver = None
            if hasattr(self.wiki_client, 'client'):
                # 如果是MultiServerWikidataQueryClient包装类
                driver = self.wiki_client.client.driver
            else:
                # 如果是直接的Neo4jClient
                driver = self.wiki_client.driver
            
            with driver.session() as session:
                # 策略1：首先搜索name属性
                query_name = """
                MATCH (n)
                WHERE n.name IS NOT NULL AND toLower(n.name) CONTAINS toLower($keyword)
                RETURN id(n) as node_id, n as node
                LIMIT $limit
                """
                
                result = session.run(query_name, keyword=keyword, limit=limit)
                
                matches = []
                for record in result:
                    node_data = dict(record["node"])
                    node_id = record["node_id"]
                    
                    # 找出哪些属性匹配了关键词
                    matched_properties = {}
                    for key, value in node_data.items():
                        if isinstance(value, str) and keyword.lower() in value.lower():
                            matched_properties[key] = value
                    
                    matches.append({
                        "qid": f"NEO_{node_id}",
                        "properties": node_data,
                        "matched_properties": matched_properties,
                        "matched_keyword": keyword
                    })
                
                # 如果name属性搜索结果不足，搜索其他属性
                if len(matches) < 3:
                    query_other = """
                    MATCH (n)
                    WHERE n.name IS NULL OR NOT toLower(n.name) CONTAINS toLower($keyword)
                    RETURN id(n) as node_id, n as node
                    LIMIT 50
                    """
                    
                    result2 = session.run(query_other, keyword=keyword)
                    seen_ids = {m['qid'] for m in matches}
                    
                    for record in result2:
                        node_data = dict(record["node"])
                        node_id = record["node_id"]
                        qid = f"NEO_{node_id}"
                        
                        if qid in seen_ids:
                            continue
                        
                        # 检查其他属性
                        matched_properties = {}
                        for key, value in node_data.items():
                            if isinstance(value, str) and keyword.lower() in value.lower():
                                matched_properties[key] = value
                        
                        if matched_properties:
                            matches.append({
                                "qid": qid,
                                "properties": node_data,
                                "matched_properties": matched_properties,
                                "matched_keyword": keyword
                            })
                            seen_ids.add(qid)
                        
                        if len(matches) >= limit:
                            break
                
                return matches
        except Exception as e:
            print(f"灵活搜索异常: {e}")
            return []

    def get_node_full_info(self, qid: str) -> Dict:
        """根据QID获取节点的完整信息"""
        try:
            if not qid.startswith("NEO_"):
                return None
            
            node_id = int(qid.replace("NEO_", ""))
            
            # 检查wiki_client的类型
            driver = None
            if hasattr(self.wiki_client, 'client'):
                # 如果是MultiServerWikidataQueryClient包装类
                driver = self.wiki_client.client.driver
            else:
                # 如果是直接的Neo4jClient
                driver = self.wiki_client.driver
            
            with driver.session() as session:
                query = """
                MATCH (n)
                WHERE id(n) = $node_id
                RETURN id(n) as node_id, n as node
                """
                
                result = session.run(query, node_id=node_id)
                for record in result:
                    return {
                        "qid": qid,
                        "properties": dict(record["node"])
                    }
        except Exception as e:
            print(f"获取节点信息异常: {e}")
        
        return None

    def _get_entity_label(self, qid: str, default: str) -> str:
        """获取实体标签"""
        try:
            labels = self.wiki_client.query_all("qid2label", qid)
            if labels and "Not Found!" not in labels:
                return next(iter(labels))
        except:
            pass

        return default

    def rank_entities_by_llm(self, question: str, candidates: List[Dict]) -> List[Dict]:
        """
        使用大模型根据问题对候选实体进行相关性排序
        返回排序后的实体列表，包含相关度分数和理由
        """
        if not candidates:
            return []
        
        # 构建提示词
        prompt = self._build_entity_ranking_prompt(question, candidates)
        
        try:
            response = run_llm(prompt, 0.1, 1000, self.args.LLM_API_KEY, self.args.LLM_TYPE)
            
            # 解析大模型的响应
            ranked_results = self._parse_ranking_response(response, candidates)
            
            return ranked_results
            
        except Exception as e:
            print(f"大模型排序异常: {e}")
            # 如果大模型失败，返回原始列表，默认分数为0.5
            return [{**cand, "relevance_score": 0.5, "reason": "默认分数"} for cand in candidates]
    
    def _build_entity_ranking_prompt(self, question: str, candidates: List[Dict]) -> str:
        """构建实体排序提示词"""
        
        # 格式化候选实体信息
        candidates_text = ""
        for i, cand in enumerate(candidates, 1):
            candidates_text += f"\n【候选{i}】\n"
            candidates_text += f"QID: {cand['qid']}\n"
            for key, value in cand['properties'].items():
                # 截断过长的值
                value_str = str(value)
                if len(value_str) > 500:
                    value_str = value_str[:500] + "..."
                candidates_text += f"  {key}: {value_str}\n"
        
        prompt = f"""你是一个专业的信息检索专家。请根据用户的问题，评估以下候选实体的相关程度。

用户问题："{question}"

候选实体信息：
{candidates_text}

请对每个候选实体进行评估：
1. 相关度分数（0-1之间，1表示最相关）
2. 判断理由（简要说明为什么相关或不相关）
3. 是否能直接回答用户问题（是/否）

输出格式（严格的JSON）：
{{
  "rankings": [
    {{
      "qid": "实体的QID",
      "relevance_score": 0.95,
      "can_answer": true,
      "reason": "判断理由"
    }}
  ]
}}

重要判断原则：
1. 识别用户问题的核心关键词（如"金额"、"构成"、"标段数"、"比例"等）
2. 优先选择与核心关键词完全匹配的实体
3. 对于不匹配的实体类型，应该给出低分数（如0.1-0.2）
   - 例如：如果问题问"金额构成"，则"标段数"、"比例"等实体相关性很低（0.1-0.2）
   - 例如：如果问题问"金额构成"，则"金额"类实体相关性很高（0.8-1.0）
4. 请仔细分析实体的所有属性，特别关注能够帮助回答用户问题的属性
5. 相关度分数要严格区分，不要给所有实体相似的分数
6. **业务逻辑推断**：如果问题需要计算或对比，请根据业务逻辑判断真正需要的实体
   - 例如：如果问题问"非公开采购金额"，虽然问题中没有直接提到"电子采购"，但根据业务逻辑"非公开采购金额 = 电子采购中标金额 - 公开采购中标金额"，需要"电子采购中标金额"和"公开采购中标金额"两个实体
   - 例如：如果问题问"A和B的金额构成"，只需要A和B相关的金额类实体，不需要比例、标段数等其他类型实体
   - 例如：如果问题需要"总金额"，应该选择聚合查询（如SUM），而不是分组查询（如GROUP BY）
7. **严格过滤**：对于问题不需要的实体类型，请给出很低的相关度分数（0.1-0.2）
8. **精确匹配**：仔细分析实体的计算SQL，判断SQL的查询类型是否符合问题需求
   - 如果问题需要总金额，应该选择聚合查询（如SUM），而不是分组查询（如GROUP BY）
   - 如果问题需要构成分析，应该选择能够提供构成数据的实体，而不是细分数据的实体"""
        
        return prompt
    
    def _parse_ranking_response(self, response: str, candidates: List[Dict]) -> List[Dict]:
        """解析大模型的排序响应"""
        try:
            # 尝试提取JSON
            json_data = self._extract_json_from_response(response)
            
            if json_data and "rankings" in json_data:
                # 构建QID到候选实体的映射
                cand_map = {c['qid']: c for c in candidates}
                
                ranked = []
                for ranking in json_data["rankings"]:
                    qid = ranking.get("qid")
                    if qid in cand_map:
                        ranked.append({
                            **cand_map[qid],
                            "relevance_score": ranking.get("relevance_score", 0.5),
                            "can_answer": ranking.get("can_answer", False),
                            "reason": ranking.get("reason", "")
                        })
                
                # 按相关度排序
                ranked.sort(key=lambda x: x["relevance_score"], reverse=True)
                return ranked
                
        except Exception as e:
            print(f"解析排序响应异常: {e}")
        
        # 解析失败，返回默认分数
        return [{**cand, "relevance_score": 0.5, "can_answer": False, "reason": "解析失败，默认分数"} for cand in candidates]

    def link_entities_with_llm_judge(self, question: str) -> Dict[str, Any]:
        """
        完整的实体链接流程：
        1. 从问题中提取关键词
        2. 搜索匹配的节点（所有属性）
        3. 使用大模型判断相关性
        4. 返回最相关的实体及其完整信息
        """
        print(f"\n=== 开始实体链接（带大模型判断）===")
        print(f"问题: {question}")
        
        # 1. 提取实体/关键词
        keywords = self.recognize_entities(question)
        if not keywords:
            # 如果没有提取到实体，使用整个问题作为关键词
            keywords = [question]
        
        print(f"提取的关键词: {keywords}")
        
        # 2. 搜索所有关键词，收集候选
        all_candidates = []
        seen_qids = set()
        
        for keyword in keywords:
            candidates = self.search_entities_with_full_info(keyword, limit=5)
            for cand in candidates:
                if cand['qid'] not in seen_qids:
                    all_candidates.append(cand)
                    seen_qids.add(cand['qid'])
        
        print(f"找到 {len(all_candidates)} 个候选实体")
        
        if not all_candidates:
            print("未找到任何候选实体")
            return {
                "linked_entities": {},
                "best_match": None,
                "all_candidates": []
            }
        
        # 3. 使用大模型进行相关性排序
        ranked_candidates = self.rank_entities_by_llm(question, all_candidates)
        
        print(f"\n大模型排序结果（前3）：")
        for i, cand in enumerate(ranked_candidates[:3], 1):
            print(f"  {i}. QID: {cand['qid']}, 分数: {cand['relevance_score']:.2f}, 可回答: {cand['can_answer']}")
            print(f"     理由: {cand['reason']}")
        
        # 4. 构建返回结果
        linked_entities = {}
        for cand in ranked_candidates:
            # 只包含相关度分数高于0.5的实体
            if cand.get('relevance_score', 0) >= 0.5:
                # 直接使用实体的name属性作为标签，如果没有则使用QID
                # 大模型已经在排序过程中看到了所有属性，不需要在这里做额外处理
                label = cand['properties'].get('name', cand['qid'])
                linked_entities[cand['qid']] = label
        
        best_match = ranked_candidates[0] if ranked_candidates else None
        
        return {
            "linked_entities": linked_entities,
            "best_match": best_match,
            "all_candidates": ranked_candidates
        }