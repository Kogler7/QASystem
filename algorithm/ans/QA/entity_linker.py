import os
import re
import json
import logging
from typing import Dict, List, Tuple, Any
from algorithm.ans.QA.utils import run_llm
from algorithm.ans.QA.prompt_list import entity_ranking_prompt


LOGGER = logging.getLogger(__name__)

# ── Term 语义向量索引（懒加载，可整体关闭） ──
_term_index = None
_term_index_tried = False


def load_term_vector_index():
    """加载 Term 向量索引；未配置 TERM_VECTOR_INDEX 或加载失败返回 None。"""
    global _term_index, _term_index_tried
    if _term_index_tried:
        return _term_index
    _term_index_tried = True
    path = os.getenv("TERM_VECTOR_INDEX", "").strip()
    if not path:
        return None
    try:
        from algorithm.term_vector.index import TermVectorIndex

        _term_index = TermVectorIndex.from_json(path)
        LOGGER.info("已加载 Term 向量索引：%s（%d 条，dim=%d）",
                    path, len(_term_index.records), _term_index.vectors.shape[1])
    except Exception as exc:
        LOGGER.warning("Term 向量索引加载失败，跳过向量召回：%s", exc)
        _term_index = None
    return _term_index


_DECOMPOSE_PROMPT = """请把用户问题改写/拆分成一个或多个“自洽、可直接用于语义检索”的简单子问题。

要求：
1. 每个子问题尽量只针对用户问题中明确出现的一个对象/实体/指标，或一个分析意图；
2. 只按问题里已有的内容拆分或改写，禁止补充问题外的实体、规则或背景；
3. 保持完整名词短语，不要裁短、去掉修饰词或把短语拆碎；
4. 问题只有一个目标时，就返回一个改写后的问题（可基本保留原句）。

用户问题：{question}

只输出 JSON：{{"questions": ["子问题1", "子问题2", ...]}}
不要输出解释和 Markdown。"""


class EntityLinker:
    def __init__(self, km_client, args):
        self.km_client = km_client
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
            response = run_llm(prompt, 0.4, 800, self.args.GRAPH_LLM_API_KEY, self.args.GRAPH_LLM_MODEL)

            entities = self._parse_llm_response(question, response)

            self.llm_cache[cache_key] = entities
            return entities

        except Exception as e:
            return []


    def _build_entity_recognition_prompt(self, question: str) -> str:
        """构建实体识别提示词"""
        return """你是知识图谱检索的查询词提取器。请只提取问题文本中明确出现、适合用于图谱实体链接的名词短语。保持短语完整（含其限定修饰），不要拆成更短的子串、不要去掉修饰词；宁可提取较完整的名词短语。

问题："{question}"

禁止根据模型记忆补充问题中没有出现的实体、业务规则、公式或上下游概念。后续关联实体必须通过图谱关系探索获得。

输出格式（严格的JSON）：
{{
  "entities": [
    {{
      "text": "实体文本",
      "type": "问题文本可判断的粗粒度类型",
      "importance": "高/中/低"
    }}
  ]
}}

只输出 JSON，不要输出解释或 Markdown。""".format(question=question)


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
        """在知识库中查找实体（KnowledgeUnit，精确匹配优先）"""
        try:
            rows = self.km_client.execute_cypher(
                "MATCH (n:KnowledgeUnit) "
                "WHERE n.resource_type CONTAINS $term_type "
                "AND (n.name = $keyword OR n.name CONTAINS $keyword) "
                "RETURN n.id AS id "
                "LIMIT 5",
                {"term_type": "Term", "keyword": entity},
            )
            return rows[0]["id"] if rows else None
        except Exception:
            return None

    def search_entities_with_full_info(
        self, keyword: str, limit: int = 10, space_id: str = None
    ) -> List[Dict]:
        """实体对齐搜索：在 KnowledgeUnit 中按 name 匹配（CONTAINS），精确命中优先。

        命中 name 与关键词完全相同的候选标记 exact_match=True 并置顶，
        避免后续 LLM 统一打分时被向量候选挤低。

        ``space_id`` 给出时，术语范围由该知识空间圈定（CONTAINS_UNIT 一跳），
        不再依赖 ``resource_type`` 里写死的 'Term' 标识——术语属性改名或换场景
        时，只要调用方指明空间就仍能取到术语节点。未给出时保持原有 Term 过滤。
        """
        if space_id:
            statement = (
                "MATCH (sp:KnowledgeSpace {id:$space_id})-[:CONTAINS_UNIT]->(n:KnowledgeUnit) "
                "WHERE n.name CONTAINS $keyword "
                "RETURN n.id AS id, n.name AS name, n.content AS content "
                "LIMIT $limit"
            )
            parameters = {"space_id": space_id, "keyword": keyword, "limit": limit}
        else:
            statement = (
                "MATCH (n:KnowledgeUnit) "
                "WHERE n.resource_type CONTAINS $term_type AND n.name CONTAINS $keyword "
                "RETURN n.id AS id, n.name AS name, n.content AS content "
                "LIMIT $limit"
            )
            parameters = {"term_type": "Term", "keyword": keyword, "limit": limit}
        rows = self.km_client.execute_cypher(statement, parameters)
        keyword_norm = (keyword or "").strip()
        matches = []
        seen_ids = set()
        for row in rows:
            qid = row.get("id", "")
            if not qid or qid in seen_ids:
                continue
            seen_ids.add(qid)
            name = row.get("name", "") or ""
            node_data = {
                "id": qid,
                "name": name,
                "content": row.get("content", "") or "",
            }
            mprops = {k: v for k, v in node_data.items()
                      if isinstance(v, str) and keyword.lower() in v.lower()}
            matches.append({
                "qid": qid,
                "properties": node_data,
                "labels": ["KnowledgeUnit"],
                "matched_properties": mprops,
                "matched_keyword": keyword,
                "exact_match": bool(keyword_norm) and name.strip() == keyword_norm,
            })
        # 精确命中置顶（名称相同优先）
        matches.sort(key=lambda m: (not m.get("exact_match"), m.get("properties", {}).get("name") or ""))
        return matches[:limit]

    def get_node_full_info(self, qid: str) -> Dict:
        """根据 QID（KnowledgeUnit 的 UUID）获取节点的完整信息。

        完整信息 = 节点自身字段 + 关联的 KnowledgeProperty 逻辑属性。
        逻辑属性直接 RETURN p 并把网关返回的行原样透传给大模型——
        网关会把节点展开成 "p.属性" 的扁平标量列，任何提取/解析都会丢数据；
        这里只剥掉列名上的别名前缀 "p."，值原样保留。
        """
        rows = self.km_client.execute_cypher(
            "MATCH (n:KnowledgeUnit {id:$id}) "
            "RETURN n.id AS id, n.name AS name, n.content AS content, "
            "n.content_type AS content_type, n.knowledge_status AS knowledge_status, "
            "n.resource_type AS resource_type, "
            "n.logical_properties_json AS logical_properties_json, "
            "labels(n) AS labels "
            "LIMIT 1",
            {"id": qid},
        )
        if not rows:
            return None
        row = rows[0]
        props = {k: v for k, v in row.items() if v is not None}

        prop_rows = self.km_client.execute_cypher(
            "MATCH (n:KnowledgeUnit {id:$id})-[:HAS_LOGICAL_PROPERTY]->(p:KnowledgeProperty) "
            "RETURN p "
            "LIMIT 50",
            {"id": qid},
        )
        cleaned_props: List[Dict[str, Any]] = []
        for row in prop_rows:
            entry: Dict[str, Any] = {}
            for k, v in row.items():
                key = k.split(".", 1)[1] if isinstance(k, str) and "." in k else k
                if key == "properties_json":
                    continue  # 原始 JSON 不对外暴露，只保留 id/name/property_type
                entry[key] = v
            cleaned_props.append(entry)
        props["logical_properties"] = cleaned_props

        node_labels = row.get("labels")
        if not isinstance(node_labels, list):
            node_labels = []
        return {
            "qid": qid,
            "name": props.get("name", ""),
            "labels": node_labels or ["KnowledgeUnit"],
            "properties": props,
        }

    def _get_entity_label(self, qid: str, default: str) -> str:
        """获取实体名称（KnowledgeUnit 按 id 查 name）"""
        try:
            rows = self.km_client.execute_cypher(
                "MATCH (n:KnowledgeUnit {id:$id}) RETURN n.name AS name LIMIT 1",
                {"id": qid},
            )
            if rows and rows[0].get("name"):
                return rows[0]["name"]
        except Exception:
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
            response = run_llm(prompt, 0.1, 1000, self.args.GRAPH_LLM_API_KEY, self.args.GRAPH_LLM_MODEL)
            
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

    def _backfill_candidate_attributes(self, candidates: List[Dict]) -> None:
        """按 qid 从 TermVectorIndex 回填 definition/synonyms/enriched_text。

        字面(CONTAINS)召回的候选只带节点字段（name/content），而 Term 的真属性
        （TermDefinition/Synonyms）在 KnowledgeProperty 里、build 时已解析进索引。
        这里让所有候选都拿到『真属性』，保证进 LLM 重排时信息一致。
        索引未加载时静默跳过，不影响原行为。
        """
        index = load_term_vector_index()
        if index is None:
            return
        by_id: Dict[str, Dict] = {}
        for record in index.records:
            key = record.get("term_id") or record.get("id")
            if key:
                by_id[str(key)] = record
        for candidate in candidates:
            record = by_id.get(str(candidate.get("qid") or ""))
            if not record:
                continue
            props = candidate.setdefault("properties", {})
            if not props.get("definition") and record.get("definition"):
                props["definition"] = record["definition"]
            if not props.get("synonyms") and record.get("synonyms"):
                props["synonyms"] = record["synonyms"]
            if not props.get("content") and record.get("definition"):
                props["content"] = record["definition"]
            if record.get("enriched_text") and not props.get("enriched_text"):
                props["enriched_text"] = record["enriched_text"]

    @staticmethod
    def _append_candidates(all_candidates: List[Dict], seen_qids: set, candidates: List[Dict]) -> None:
        for cand in candidates:
            qid = cand.get("qid")
            if qid and qid not in seen_qids:
                all_candidates.append(cand)
                seen_qids.add(qid)

    def search_entities_by_vector(self, keyword: str, top_k: int = 5, min_score: float = 0.0) -> List[Dict]:
        """Term 向量语义召回（兜底）：embedding 查询短语 → 进程内余弦 top-K。

        返回与 search_entities_with_full_info 同构的候选，仅用于扩宽候选池；
        最终仍由 LLM 重排把关。索引未配置/加载失败时返回空列表，不影响原链路。
        """
        index = load_term_vector_index()
        if index is None:
            return []
        try:
            # 与后端 embedding 同源：自动按 env 选择本地模型或 remote 回退
            from algorithm.NL_to_cypher.vector import batch_text_to_vectors

            query_vector = batch_text_to_vectors([keyword])[0]
        except Exception as exc:
            LOGGER.debug("向量召回 embedding 失败，跳过（keyword=%s）：%s", keyword, exc)
            return []

        try:
            hits = index.search(query_vector, top_k=max(top_k, 0), min_score=min_score)
        except Exception as exc:
            LOGGER.debug("向量召回检索失败（keyword=%s）：%s", keyword, exc)
            return []

        candidates: List[Dict] = []
        for hit in hits:
            qid = hit.get("term_id") or hit.get("id")
            if not qid:
                continue
            name = str(hit.get("name") or "")
            definition = str(hit.get("definition") or "")
            content = str(hit.get("content") or "") or definition
            props: Dict[str, Any] = {"id": qid, "name": name, "content": content}
            if definition:
                props["definition"] = definition
            if hit.get("synonyms"):
                props["synonyms"] = hit["synonyms"]
            candidates.append({
                "qid": qid,
                "properties": props,
                "labels": ["KnowledgeUnit"],
                "matched_properties": {"name": name},
                "matched_keyword": keyword,
                "vector_score": hit.get("score", 0.0),
            })
        return candidates

    # ==================== 整句向量召回：LLM 拆分子问题 ====================

    def _decompose_question(self, question: str) -> List[str]:
        """把复合问题拆成自洽的简单子问题（每句针对一个对象/意图），用于整句向量召回。

        只按问题里已有的对象/意图切分与改写，禁止补充问题外实体；保持完整名词短语。
        失败或无需拆分时回退为 [原问题]。
        """
        q = (question or "").strip()
        if not q:
            return []
        cache_key = f"dq_{q}"
        if cache_key in self.llm_cache:
            return self.llm_cache[cache_key]

        subs: List[str] = []
        try:
            response = run_llm(_DECOMPOSE_PROMPT.format(question=q), 0.2, 600,
                               self.args.GRAPH_LLM_API_KEY, self.args.GRAPH_LLM_MODEL)
            parsed = self._parse_question_list(response)
            for item in parsed:
                item = (item or "").strip()
                if item and len(item) >= 2 and item not in subs:
                    subs.append(item)
        except Exception:
            subs = []

        if not subs:
            subs = [q]
        subs = subs[:8]
        self.llm_cache[cache_key] = subs
        return subs

    def _parse_question_list(self, text: str) -> List[str]:
        """从 LLM 响应提取 questions 数组；失败返回空列表。"""
        if not text:
            return []
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        text = re.sub(r"<think>.*", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"^```(?:json)?\n|```$", "", text).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", text)
            if not m:
                return []
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return []
        if isinstance(data, dict):
            questions = data.get("questions") or data.get("sub_questions") or []
            if isinstance(questions, list):
                return [str(x) for x in questions]
        return []

    def _vector_seeds(self, question: str, keywords: list) -> List[str]:
        """向量召回的查询源：LLM 拆分的整句子问题 + 原问题（去重）。

        没有可拆分的子问题时，至少用原问题整句；连问题都没有时退回关键词。
        """
        q = (question or "").strip()
        if q:
            subs = self._decompose_question(q)
            # 能拆成多个自洽子问题时，只对各子问题整句做向量召回（各自独立、语义聚焦）；
            # 拆不出子问题（单目标）时才用原问题整句作查询源。
            if len(subs) >= 2:
                return [s for s in subs if s and s.strip()]
            return [q]
        return [k for k in (keywords or []) if k and str(k).strip()]

    def link_entities_with_llm_judge(
        self, question: str, keywords: list = None, space_id: str = None
    ) -> Dict[str, Any]:
        """把问题关键词对齐到图谱实体。

        ``space_id`` 由调用方（Agent 从知识空间定位结果中选出）传入，
        用于圈定术语范围；不给则退化为按 resource_type 里的 'Term' 过滤。
        """
        if keywords is None:
            keywords = self.recognize_entities(question)
        LOGGER.debug("linking %d graph keywords (space_id=%s)", len(keywords), space_id or "-")

        all_candidates = []
        seen_qids = set()

        try:
            vector_min_score = float(os.getenv("TERM_VECTOR_MIN_SCORE", "0.0"))
            vector_top_k = int(os.getenv("TERM_VECTOR_TOP_K", "5"))
        except ValueError:
            vector_min_score, vector_top_k = 0.0, 5

        # 词面召回：按关键词（name CONTAINS），保持现有逻辑；标记 recall=lexical 供回退优先使用
        for keyword in keywords:
            candidates = self.search_entities_with_full_info(keyword, limit=5, space_id=space_id)
            for cand in candidates:
                cand["recall"] = "lexical"
            self._append_candidates(all_candidates, seen_qids, candidates)

        # 向量语义召回：查询源 = LLM 拆分的整句子问题（+原问题整句），避免用拆碎的小词做向量匹配；
        # 只扩宽候选池，最终仍由 LLM 按原问题重排校验。未配置向量索引时跳过，不白做拆分。
        if load_term_vector_index() is not None:
            for seed in self._vector_seeds(question, keywords):
                vector_candidates = self.search_entities_by_vector(seed, top_k=vector_top_k, min_score=vector_min_score)
                for cand in vector_candidates:
                    cand["recall"] = "vector"
                self._append_candidates(all_candidates, seen_qids, vector_candidates)

        # 候选构建完统一回填『真属性』（definition/synonyms/enriched_text），
        # 确保字面候选与向量候选进 LLM 重排时信息一致。
        self._backfill_candidate_attributes(all_candidates)

        if not all_candidates:
            return {
                "linked_entities": {},
                "best_match": None,
                "all_candidates": []
            }

        ranked_candidates = self.rank_entities_by_llm(question, all_candidates)

        linked_entities = {}
        degraded = False
        exact_qids = {c.get('qid') for c in all_candidates if c.get('exact_match')}
        # 名称精确命中置顶，并在下面保证“必留”，不被 LLM 分数或向量候选挤掉
        if exact_qids:
            ranked_candidates.sort(key=lambda c: 0 if c.get('qid') in exact_qids else 1)
        for cand in ranked_candidates:
            is_exact = cand.get('qid') in exact_qids
            if cand.get('relevance_score', 0) >= 0.7 or is_exact:
                if is_exact:
                    if cand.get('relevance_score', 0) < 0.7:
                        cand['relevance_score'] = 0.8
                    cand['is_relevant'] = True
                    cand['reason'] = ((cand.get('reason') or '').strip() + '；名称精确命中，强制保留').strip('；')
                label = cand['properties'].get('name', cand['qid'])
                linked_entities[cand['qid']] = label

        # LLM 重排无结果（content 为空 / 解析失败等）时回退：
        # 优先用字符(CONTAINS)命中（recall=lexical）——至少与关键词字面匹配，更可靠；
        # 仅当没有词面命中时才回退向量分数。两种情况都标记 degraded。
        if not linked_entities:
            lexical = [c for c in all_candidates if c.get('recall') == 'lexical']
            if lexical:
                lexical.sort(key=lambda c: c.get('name') or '')
                pool, source = lexical[:5], '词面(CONTAINS)'
            else:
                vector = [c for c in all_candidates if c.get('vector_score') is not None]
                vector.sort(key=lambda c: float(c.get('vector_score', 0.0)), reverse=True)
                pool, source = vector[:5], '向量分数'
            if pool:
                degraded = True
                ranked_candidates = pool
                for cand in ranked_candidates:
                    cand['relevance_score'] = 0.8 if source.startswith('词面') else float(cand.get('vector_score', 0.0))
                    cand['is_relevant'] = True
                    cand['reason'] = f'LLM 重排无结果，回退{source}（降级）'
                    qid = cand.get('qid')
                    if qid:
                        linked_entities[qid] = cand['properties'].get('name', qid)

        return {
            "linked_entities": linked_entities,
            "all_candidates": ranked_candidates,
            "degraded": degraded,
        }
