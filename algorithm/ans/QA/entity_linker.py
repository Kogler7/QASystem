import re
import json
from typing import Dict, List, Tuple, Any
from algorithm.ans.QA.utils import run_llm
from algorithm.ans.QA.prompt_list import entity_recognition_prompt, entity_ranking_prompt


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
            # 增加 max_tokens 以确保 JSON 响应不被截断
            response = run_llm(prompt, 0.4, 800, self.args.LLM_API_KEY, self.args.LLM_TYPE)

            entities = self._parse_llm_response(question, response)

            self.llm_cache[cache_key] = entities
            return entities

        except Exception as e:
            return []


    def _build_entity_recognition_prompt(self, question: str) -> str:
        """构建实体识别提示词"""
        return """你是一个专业的实体提取专家。请分析以下问题，提取出所有对回答问题有帮助的实体，并根据实体的类型进行标注。

问题："{question}"

分析要求：
1. 理解问题的意图和上下文
2. 识别问题中提到的所有具体事物、概念、人物、地点等
3. 对于模糊的实体，根据上下文确定最可能的含义
4. 特别注意与问题核心相关的关键概念
5. **重要**：对于需要计算或对比的问题，请根据业务逻辑推断可能需要的实体

实体类型：
- 物品/产品：具体的物品、产品、设备等
- 人物：具体的个人、角色等
- 组织/机构：公司、政府部门、组织等
- 地点/位置：具体的地点、位置等
- 时间/日期：具体的时间、日期、时期等
- 事件：具体的事件、活动等
- 概念/术语：抽象的概念、专业术语等
- 学科/领域：具体的学科、领域等
- 统计指标：金额、数量、比例等
- 其他：其他类型的实体

实体提取标准：
- 名词性短语（如"瑜伽垫"、"跑步机"）
- 专有名词（如"姚明"、"北京"）
- 概念术语（如"人工智能"、"量子力学"）
- 事件活动（如"奥运会"、"春节"）
- **业务逻辑推断**：如果问题需要计算或对比，请推断需要的实体
  - 例如：如果问题包含"非公开采购金额"，请推断需要"电子采购中标金额"和"公开采购中标金额"
  - 例如：如果问题包含"A和B的对比"，请提取A和B相关的所有实体

**重要提示**：
- 直接输出JSON，不要输出任何思考过程或解释
- 不要使用<think>标签
- 确保JSON格式完整且正确

输出格式（严格的JSON）：
{{
  "analysis": "简短分析问题的意图",
  "entities": [
    {{
      "text": "实体文本",
      "type": "物品/产品/人物/组织/机构/地点/位置/时间/日期/事件/概念/术语/学科/领域/统计指标/其他",
      "importance": "高/中/低",
      "reason": "为什么提取这个实体"
    }}
  ]
}}

注意：
- 只提取问题中实际提到的实体或根据业务逻辑推断的实体
- 确保JSON格式正确，不要有多余的逗号或括号
- 直接输出JSON，不要添加任何其他内容""".format(question=question)


    def _parse_llm_response(self, question: str, response: str) -> List[str]:
        """解析LLM响应，提取实体"""
        entities = []
        
        # 清理响应
        response = response.strip()
        
        # 移除 <think> 标签及其内容（即使没有结束标签）
        import re
        # 移除完整的 <think>...</think> 标签
        response = re.sub(r'<think>.*?</think>', '', response, flags=re.DOTALL)
        # 移除只有开始标签的情况
        response = re.sub(r'<think>.*', '', response, flags=re.DOTALL)
        response = response.strip()
        
        # 尝试解析JSON
        try:
            # 尝试直接解析整个响应
            import json
            # 清理可能的格式问题
            clean_response = response
            # 移除可能的markdown代码块标记
            clean_response = re.sub(r'^```json\n|^```\n|\n```$', '', clean_response)
            clean_response = clean_response.strip()
            
            json_data = json.loads(clean_response)
            if json_data and "entities" in json_data:
                entities = self._parse_json_entities(json_data, question)
                if entities:
                    return entities
        except json.JSONDecodeError:
            # 如果直接解析失败，尝试提取JSON片段
            # 查找包含 entities 的JSON对象
            entity_pattern = r'\{[\s\S]*?"entities"[\s\S]*?\}'
            match = re.search(entity_pattern, response)
            if match:
                try:
                    json_str = match.group(0)
                    # 修复可能的JSON格式问题
                    json_str = re.sub(r',\s*\}', '}', json_str)
                    json_str = re.sub(r',\s*\]', ']', json_str)
                    json_data = json.loads(json_str)
                    if json_data and "entities" in json_data:
                        entities = self._parse_json_entities(json_data, question)
                        if entities:
                            return entities
                except json.JSONDecodeError:
                    pass

        # 如果没有JSON，尝试自由文本解析
        if not entities:
            # 直接从问题中提取可能的实体
            # 基于问题的结构和内容提取实体
            import re
            
            # 分割问题
            separators = ["与", "和", "的", "是", "如何", "什么", "多少", "？", "，", "。"]
            temp_question = question
            for sep in separators:
                temp_question = temp_question.replace(sep, " ")
            
            # 提取单词
            words = temp_question.split()
            
            # 过滤和处理
            for word in words:
                # 清理单词
                cleaned_word = word.strip()
                # 只保留长度大于等于2的单词
                if len(cleaned_word) >= 2:
                    # 排除常见疑问词
                    if cleaned_word not in {"什么", "谁", "哪里", "如何", "为什么", "怎样", "多少"}:
                        entities.append(cleaned_word)
            
            # 去重
            entities = list(set(entities))
        
        # 后处理
        entities = self._post_process_entities(entities, question)
        
        return entities

    def _extract_json_from_response(self, response: str) -> Dict:
        """从响应中提取JSON数据"""
        try:
            # 清理 <think> 标签
            import re
            response = re.sub(r'<think>.*?</think>', '', response, flags=re.DOTALL)
            response = re.sub(r'<think>.*', '', response, flags=re.DOTALL)
            response = response.strip()
            
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
                    
                    # 尝试修复JSON格式
                    # 移除可能的多余逗号
                    json_str = re.sub(r',\s*}', '}', json_str)
                    json_str = re.sub(r',\s*]', ']', json_str)
                    
                    # 尝试解析
                    try:
                        data = json.loads(json_str)
                        if isinstance(data, dict):
                            # 支持多种字段名
                            if "entities" in data or "rankings" in data:
                                return data
                    except json.JSONDecodeError:
                        # 如果解析失败，继续尝试其他模式
                        continue
        except Exception as e:
            return {"rankings": []}

        return {"rankings": []}

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
            pass

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
        # 清理实体和问题
        entity = entity.strip()
        question = question.strip()
        
        # 检查实体是否就是整个问题
        if entity == question:
            return False
        
        # 检查实体是否包含整个问题
        if question in entity:
            return False
        
        # 直接包含
        if entity in question:
            return True

        # 部分包含（对于长实体）
        if len(entity) > 2:
            # 检查实体的关键部分是否在问题中
            for part in entity.split():
                if part in question and len(part) > 1:
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
        """后处理实体列表"""
        if not entities:
            return []

        # 去重
        unique_entities = []
        seen = set()

        # 清理问题文本，用于比较
        cleaned_question = question.strip()
        # 移除末尾的标点符号
        cleaned_question = re.sub(r'[？！。,，]$', '', cleaned_question)

        for entity in entities:
            entity = entity.strip()
            # 清理实体，移除末尾的标点符号
            cleaned_entity = re.sub(r'[？！。,，]$', '', entity)
            
            # 过滤条件
            if (entity and
                    len(entity) >= 2 and
                    entity not in seen and
                    not self._is_common_word(entity) and
                    cleaned_entity != cleaned_question):  # 排除整个问题作为实体（忽略末尾标点）
                seen.add(entity)
                unique_entities.append(entity)

        # 排序：按在问题中出现的顺序
        sorted_entities = sorted(
            unique_entities,
            key=lambda x: question.find(x) if x in question else len(question)
        )

        return sorted_entities



    def link_entities_in_question(self, question: str) -> Dict[str, str]:
        entity_mentions = self.recognize_entities(question)

        if not entity_mentions:
            return {}

        linked_entities = {}
        for mention in entity_mentions:
            try:
                qid = self._find_entity_in_knowledge_base(mention)
                if qid:
                    label = self._get_entity_label(qid, mention)
                    linked_entities[qid] = label
            except Exception as e:
                pass

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
        术语对齐搜索：优先精确匹配 name，其次同义词展开，再模糊匹配。
        匹配策略：name精确匹配 > 同义词匹配 > name包含匹配 > 其他属性匹配
        """
        try:
            driver = None
            if hasattr(self.wiki_client, 'client'):
                driver = self.wiki_client.client.driver
            else:
                driver = self.wiki_client.driver

            with driver.session() as session:
                matches = []
                seen_ids = set()

                def add_match(node_id, node_data, matched_props):
                    qid = f"NEO_{node_id}"
                    if qid not in seen_ids:
                        seen_ids.add(qid)
                        matches.append({
                            "qid": qid,
                            "properties": node_data,
                            "matched_properties": matched_props,
                            "matched_keyword": keyword
                        })

                # ── 策略1: Term 标签 name 精确匹配（文档: Term 节点就是标准名称和同义词的集合）──
                query_term = """
                MATCH (n:Term)
                WHERE n.name = $keyword
                RETURN id(n) as node_id, n as node
                LIMIT $limit
                """
                for record in session.run(query_term, keyword=keyword, limit=limit):
                    node_data = dict(record["node"])
                    add_match(record["node_id"], node_data,
                              {"name": node_data.get("name", "")})

                # ── 策略2: name 精确匹配（全类型，兜底）──
                query_exact = """
                MATCH (n)
                WHERE n.name = $keyword
                RETURN id(n) as node_id, n as node
                LIMIT $limit
                """
                for record in session.run(query_exact, keyword=keyword, limit=limit):
                    node_data = dict(record["node"])
                    add_match(record["node_id"], node_data,
                              {"name": node_data.get("name", "")})

                # ── 策略3: name 包含匹配（原有逻辑，兜底）──
                if len(matches) < limit:
                    query_name = """
                    MATCH (n)
                    WHERE n.name IS NOT NULL AND (
                        toLower(n.name) CONTAINS toLower($keyword) OR
                        toLower($keyword) CONTAINS toLower(replace(n.name, 'CG_00', '')) OR
                        toLower($keyword) CONTAINS toLower(split(n.name, ' ')[-1]) OR
                        toLower(replace(n.name, 'CG_00', '')) CONTAINS toLower($keyword) OR
                        toLower(n.name) CONTAINS toLower(split($keyword, '与')[0]) OR
                        toLower(n.name) CONTAINS toLower(split($keyword, '与')[1]) OR
                        toLower(n.name) =~ ('(?i).*' + replace(replace($keyword, '与', '|'), '金额', '') + '.*') OR
                        toLower(n.name) =~ ('(?i).*' + $keyword + '.*')
                    )
                    RETURN id(n) as node_id, n as node
                    ORDER BY
                        CASE WHEN n.name CONTAINS $keyword THEN 0 ELSE 1 END,
                        CASE WHEN replace(n.name, 'CG_00', '') CONTAINS $keyword THEN 0 ELSE 1 END
                    LIMIT $limit
                    """
                    for record in session.run(query_name, keyword=keyword, limit=limit):
                        node_data = dict(record["node"])
                        mprops = {k: v for k, v in node_data.items()
                                  if isinstance(v, str) and keyword.lower() in v.lower()}
                        add_match(record["node_id"], node_data, mprops)

                # ── 策略4: 其他属性搜索（原有逻辑，兜底）──
                if len(matches) < 3:
                    query_other = """
                    MATCH (n)
                    WHERE n.name IS NULL OR NOT toLower(n.name) CONTAINS toLower($keyword)
                    RETURN id(n) as node_id, n as node
                    LIMIT 50
                    """
                    for record in session.run(query_other, keyword=keyword):
                        node_data = dict(record["node"])
                        mprops = {k: v for k, v in node_data.items()
                                  if isinstance(v, str) and keyword.lower() in v.lower()}
                        if mprops:
                            add_match(record["node_id"], node_data, mprops)
                        if len(matches) >= limit:
                            break

                return matches[:limit]
        except Exception as e:
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
            return None
        
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
        
        return entity_ranking_prompt.format(question=question, candidates_text=candidates_text)
    
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
                            "is_relevant": ranking.get("is_relevant", ranking.get("can_answer", False)),
                            "reason": ranking.get("reason", "")
                        })
                
                # 按相关度排序
                ranked.sort(key=lambda x: x["relevance_score"], reverse=True)
                return ranked
                
        except Exception as e:
            pass
        
        # 解析失败，返回默认分数
        return [{**cand, "relevance_score": 0.5, "is_relevant": False, "reason": "解析失败，默认分数"} for cand in candidates]

    def link_entities_with_llm_judge(self, question: str, keywords: list = None) -> Dict[str, Any]:
        if keywords is None:
            keywords = self.recognize_entities(question)
        print(f"  搜索关键词: {keywords}")

        all_candidates = []
        seen_qids = set()

        for keyword in keywords:
            candidates = self.search_entities_with_full_info(keyword, limit=5)
            for cand in candidates:
                if cand['qid'] not in seen_qids:
                    all_candidates.append(cand)
                    seen_qids.add(cand['qid'])

        if not all_candidates:
            return {
                "linked_entities": {},
                "best_match": None,
                "all_candidates": []
            }

        ranked_candidates = self.rank_entities_by_llm(question, all_candidates)

        linked_entities = {}
        for cand in ranked_candidates:
            if cand.get('relevance_score', 0) >= 0.7:
                label = cand['properties'].get('name', cand['qid'])
                linked_entities[cand['qid']] = label

        return {
            "linked_entities": linked_entities,
            "all_candidates": ranked_candidates
        }