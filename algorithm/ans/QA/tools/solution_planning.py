import sys
import os

# 添加项目根目录到Python路径
current_file = os.path.abspath(__file__)
tools_dir = os.path.dirname(current_file)
qa_dir = os.path.dirname(tools_dir)
ans_dir = os.path.dirname(qa_dir)
algorithm_dir = os.path.dirname(ans_dir)
project_root = os.path.dirname(algorithm_dir)
sys.path.insert(0, project_root)

from algorithm.ans.QA.models import SolutionModel, ProblemSolutionMatch
from algorithm.ans.QA.entity_linker import EntityLinker
from algorithm.ans.QA.wiki_func import relation_search_prune, entity_search, del_all_unknown_entity, entity_score, update_history, entity_prune, reasoning, if_finish_list, generate_without_explored_paths
from typing import Dict, List, Tuple
from datetime import datetime
import re
import json


class SolutionPlanningTool:
    """
    方案规划工具：基于问题模型生成解决方案
    """
    
    def __init__(self, neo4j_client, args):
        self.client = neo4j_client
        self.args = args
        self.entity_linker = EntityLinker(neo4j_client, args)
        self.reasoning_logs = []  # 收集推理过程中的日志
    
    def _log(self, step: str, message: str, data: any = None):
        """记录推理日志"""
        log_entry = {
            "step": step,
            "message": message,
            "data": data
        }
        self.reasoning_logs.append(log_entry)
    
    def plan_solution(self, problem_model: Dict) -> Dict:
        """
        规划解决方案
        输入：问题模型
        输出：解决方案 + 图谱路径
        """
        self.reasoning_logs = []  # 清空之前的日志
        
        try:
            # 从问题模型中提取信息
            problem_id = problem_model.get("问题ID", "PROB-001")
            problem_description = problem_model.get("问题描述", "")
            related_entities = problem_model.get("相关实体", [])
            
            # 记录问题信息
            self._log("问题分析", f"开始处理问题: {problem_description}", {
                "问题ID": problem_id,
                "问题类型": problem_model.get("问题类型"),
                "相关实体": related_entities
            })
            
            # 1. 智能实体链接（带大模型判断）
            best_match = None
            topic_entity = {}
            
            # 首先尝试智能实体链接
            self._log("实体链接", "开始智能实体链接...")
            link_result = self.entity_linker.link_entities_with_llm_judge(problem_description)
            
            # 提取所有候选实体
            all_candidates = link_result.get("all_candidates", [])
            answerable_entities = [cand for cand in all_candidates if cand.get("can_answer", False)]
            
            print(f"  找到 {len(answerable_entities)} 个可回答的实体")
            
            # 记录所有候选实体的详细信息
            self._log("实体链接", f"找到 {len(all_candidates)} 个候选实体，其中 {len(answerable_entities)} 个可回答", {
                "所有候选实体": [
                    {
                        "QID": cand["qid"],
                        "名称": cand["properties"].get("name", cand["qid"]),
                        "相关性分数": round(cand.get("relevance_score", 0), 2),
                        "可回答": cand.get("can_answer", False),
                        "判断理由": cand.get("reason", "")
                    }
                    for cand in all_candidates[:10]  # 只显示前10个，避免日志过长
                ]
            })
            
            # 如果有可回答的实体，尝试直接使用它们生成答案
            if answerable_entities:
                # 构建候选实体列表（用于 _check_if_entity_can_answer）
                candidates_for_check = []
                for cand in answerable_entities:
                    candidates_for_check.append({
                        "id": cand["qid"],
                        "name": cand["properties"].get("name", cand["qid"]),
                        "properties": cand["properties"]
                    })
                
                # 使用大模型判断这些实体是否可以直接回答问题
                self._log("答案判断", "使用大模型判断实体是否可以直接回答问题", {
                    "待判断实体": [
                        {
                            "QID": c["id"],
                            "名称": c["name"],
                            "属性数量": len(c["properties"])
                        }
                        for c in candidates_for_check
                    ]
                })
                
                can_answer, answer_from_entities, source_entities = self._check_if_entity_can_answer(
                    problem_description, candidates_for_check
                )
                
                # 记录判断结果
                self._log("答案判断结果", f"判断结果: {'可以回答' if can_answer else '不能回答'}", {
                    "是否可以回答": can_answer,
                    "生成的答案": answer_from_entities if can_answer else "无",
                    "判断依据": "大模型根据实体属性判断是否可以直接回答问题"
                })
                
                if can_answer and answer_from_entities:
                    print(f"  从实体属性直接生成答案")
                    self._log("答案生成", "从实体属性直接生成答案", {
                        "答案": answer_from_entities,
                        "使用实体": [c["name"] for c in candidates_for_check]
                    })
                    # 实体属性足够回答问题
                    reasoning_chains = []
                    search_result = {
                        "found_answer": True,
                        "depth_reached": 0,
                        "topic_entity": link_result["linked_entities"],
                        "answerable_entities": answerable_entities,
                        "source_entities": source_entities  # 保存源实体，用于构建graph_paths
                    }
                    answer = answer_from_entities
                else:
                    # 实体属性不足以直接回答，执行ToG深度搜索
                    print(f"  实体属性不足以直接回答，执行ToG深度搜索...")
                    self._log("知识图谱检索", "实体属性不足以直接回答，开始ToG深度搜索")
                    topic_entity = link_result["linked_entities"]
                    answer, reasoning_chains, search_result = self._run_tog_search(
                        question=problem_description,
                        topic_entity=topic_entity,
                        config=self.args
                    )
            else:
                # 没有找到可直接回答的实体，使用链接结果作为起始实体
                topic_entity = link_result["linked_entities"]
                print(f"  起始实体: {topic_entity}")
                
                # 记录实体识别与链接结果
                self._log("实体识别与链接", f"识别到 {len(topic_entity)} 个实体", topic_entity)

                # 调用 ToG 方法在知识图谱上检索
                answer, reasoning_chains, search_result = self._run_tog_search(
                    question=problem_description,
                    topic_entity=topic_entity,
                    config=self.args
                )
            
            # 2. 将检索结果转换为方案模型
            solution = self._convert_tog_result_to_solution(
                problem_id=problem_id,
                problem_description=problem_description,
                answer=answer,
                reasoning_chains=reasoning_chains,
                search_result=search_result
            )
            
            # 3. 创建匹配关系
            match = ProblemSolutionMatch(
                问题ID=problem_id,
                方案ID=solution.方案ID,
                匹配度=0.9 if answer and answer != "信息不足" else 0.5,
                匹配理由="基于ToG知识图谱检索"
            )
            
            # 4. 构建图谱路径信息
            graph_paths = {
                "answer_path": [],
                "reasoning_chains": reasoning_chains
            }
            
            # 从推理链中提取路径信息
            for depth, chain in enumerate(reasoning_chains):
                if chain:
                    # 直接处理 chain 中的元素
                    for item in chain:
                        path = {
                            "depth": depth + 1,
                            "nodes": [],
                            "relationships": []
                        }
                        
                        # 检查 item 的类型
                        if isinstance(item, tuple) and len(item) == 3:
                            # 格式为 (实体1, 关系, 实体2)
                            entity1, relation, entity2 = item
                            
                            # 处理实体1
                            entity1_id = entity1
                            entity1_name = entity1
                            entity1_properties = {}
                            
                            # 检查entity1是否是字典格式
                            if isinstance(entity1, dict):
                                entity1_id = entity1.get('id', entity1)
                                entity1_name = entity1.get('name', str(entity1))
                                entity1_properties = entity1.get('properties', {})
                            else:
                                # 首先检查是否在topic_entity中（键是ID，值是名称）
                                for id, name in topic_entity.items():
                                    if name == entity1:
                                        entity1_id = id
                                        break
                                # 如果不是NEO_开头的ID，尝试通过实体链接器搜索
                                if isinstance(entity1_id, str) and not entity1_id.startswith("NEO_"):
                                    try:
                                        # 尝试搜索实体
                                        candidates = self.entity_linker.search_entities_with_full_info(entity1, limit=1)
                                        if candidates:
                                            entity1_id = candidates[0].get("qid", entity1)
                                    except Exception:
                                        pass
                                # 获取实体1的属性
                                try:
                                    entity1_info = self.entity_linker.get_node_full_info(entity1_id)
                                    if entity1_info:
                                        entity1_properties = entity1_info.get("properties", {})
                                except Exception:
                                    pass
                            
                            # 处理实体2
                            entity2_id = entity2
                            entity2_name = entity2
                            entity2_properties = {}
                            
                            # 检查entity2是否是字典格式
                            if isinstance(entity2, dict):
                                entity2_id = entity2.get('id', entity2)
                                entity2_name = entity2.get('name', str(entity2))
                                entity2_properties = entity2.get('properties', {})
                            else:
                                # 首先检查是否在topic_entity中（键是ID，值是名称）
                                for id, name in topic_entity.items():
                                    if name == entity2:
                                        entity2_id = id
                                        break
                                # 如果不是NEO_开头的ID，尝试通过实体链接器搜索
                                if isinstance(entity2_id, str) and not entity2_id.startswith("NEO_"):
                                    try:
                                        # 尝试搜索实体
                                        candidates = self.entity_linker.search_entities_with_full_info(entity2, limit=1)
                                        if candidates:
                                            entity2_id = candidates[0].get("qid", entity2)
                                    except Exception:
                                        pass
                                # 获取实体2的属性
                                try:
                                    entity2_info = self.entity_linker.get_node_full_info(entity2_id)
                                    if entity2_info:
                                        entity2_properties = entity2_info.get("properties", {})
                                except Exception:
                                    pass
                            path["nodes"].append({
                                "id": entity1_id,
                                "label": entity1_name,
                                "properties": entity1_properties
                            })
                            path["nodes"].append({
                                "id": entity2_id,
                                "label": entity2_name,
                                "properties": entity2_properties
                            })
                            path["relationships"].append({
                                "from": entity1_name,
                                "type": relation,
                                "to": entity2_name
                            })
                        elif isinstance(item, list):
                            # 格式为列表，可能包含多个实体和关系
                            for i, sub_item in enumerate(item):
                                if isinstance(sub_item, dict):
                                    # 处理实体
                                    if "entity" in sub_item:
                                        entity_id = sub_item["entity"]
                                        entity_name = sub_item.get("label", entity_id)
                                        # 获取实体的属性
                                        entity_properties = {}
                                        try:
                                            entity_info = self.entity_linker.get_node_full_info(entity_id)
                                            if entity_info:
                                                entity_properties = entity_info.get("properties", {})
                                        except Exception:
                                            pass
                                        path["nodes"].append({
                                            "id": entity_id,
                                            "label": entity_name,
                                            "properties": entity_properties
                                        })
                                    # 处理关系
                                    if "relation" in sub_item and i > 0:
                                        relation_type = sub_item["relation"]
                                        # 添加关系
                                        path["relationships"].append({
                                            "from": item[i-1].get("entity"),
                                            "type": relation_type,
                                            "to": entity_id
                                        })
                        
                        if path["nodes"]:
                            graph_paths["answer_path"].append(path)
            
            print(f"提取的 answer_path: {graph_paths['answer_path']}")
            
            # 如果没有从推理链中提取到路径，但从实体属性直接生成了答案
            # 使用source_entities构建answer_path
            if not graph_paths['answer_path'] and search_result.get('source_entities'):
                graph_paths['answer_path'] = []
                for entity in search_result['source_entities']:
                    path = {
                        "depth": 0,
                        "nodes": [
                            {
                                "id": entity["id"],
                                "label": entity["name"],
                                "properties": entity["properties"]
                            }
                        ],
                        "relationships": []
                    }
                    graph_paths['answer_path'].append(path)
                print(f"为直接从实体属性生成答案构建answer_path: {graph_paths['answer_path']}")
            
            return {
                "success": True,
                "solution": solution.dict(),
                "match": match.dict(),
                "graph_paths": graph_paths,
                "answer": answer,
                "entities": topic_entity,
                "reasoning_logs": self.reasoning_logs
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "message": "方案规划失败"
            }
    
    def _run_tog_search(self, question: str, topic_entity: Dict, config) -> tuple:
        """
        运行 ToG 知识图谱检索
        在检索过程中获取节点的完整属性，并让大模型判断是否可以直接回答
        """
        cluster_chain_of_entities = []
        pre_relations = []
        pre_heads = [-1] * len(topic_entity)
        found_answer = False
        final_answer = "信息不足"

        for depth in range(1, config.DEPTH + 1):
            print(f"    ToG深度 {depth} 搜索...")

            current_entity_relations_list = []
            i = 0
            for entity in topic_entity:
                if entity != "[FINISH_ID]":
                    pre_head_value = pre_heads[i] if i < len(pre_heads) else -1

                    retrieve_relations_with_scores = relation_search_prune(
                        entity, topic_entity[entity], pre_relations,
                        pre_head_value, question, config, self.client
                    )
                    current_entity_relations_list.extend(retrieve_relations_with_scores)
                i += 1

            if not current_entity_relations_list:
                print(f"    深度 {depth} 未找到关系")
                break

            # 记录大模型选择的关系和打分
            llm_response = current_entity_relations_list[0].get('llm_response', '') if current_entity_relations_list else ''
            
            self._log("知识图谱检索", f"深度 {depth} - 大模型关系选择", {
                "深度": depth,
                "大模型选择原因": llm_response,
                "候选关系": [
                    {
                        "实体": rel.get('entity_name', rel.get('entity', '')),
                        "关系": rel.get('relation', ''),
                        "方向": "出度" if rel.get('head', True) else "入度",
                        "评分": round(rel.get('score', 0), 2)
                    }
                    for rel in current_entity_relations_list[:5]
                ]
            })

            # 收集候选实体
            total_candidates, total_scores, total_relations, total_entities_id, total_topic_entities, total_head = [], [], [], [], [], []

            for entity in current_entity_relations_list:
                direction_outgoing = entity.get('head', True)

                if direction_outgoing:
                    entity_candidates_id, entity_candidates_name = entity_search(
                        entity['entity'], entity['relation'], self.client, True
                    )
                else:
                    entity_candidates_id, entity_candidates_name = entity_search(
                        entity['entity'], entity['relation'], self.client, False
                    )

                if len(entity_candidates_name) == 0:
                    continue

                # 处理候选实体
                if len(entity_candidates_id) == 0:  # 值类型
                    if len(entity_candidates_name) >= 20:
                        import random
                        entity_candidates_name = random.sample(entity_candidates_name, 10)
                    entity_candidates_id = ["[FINISH_ID]"] * len(entity_candidates_name)
                else:  # 实体类型
                    entity_candidates_id, entity_candidates_name = del_all_unknown_entity(
                        entity_candidates_id, entity_candidates_name
                    )
                    if len(entity_candidates_id) >= 20:
                        import random
                        indices = random.sample(range(len(entity_candidates_name)), 10)
                        entity_candidates_id = [entity_candidates_id[i] for i in indices]
                        entity_candidates_name = [entity_candidates_name[i] for i in indices]

                if len(entity_candidates_id) == 0:
                    continue

                # ===== 增强：获取候选实体的完整属性并进行智能判断 =====
                enriched_candidates = []
                for idx, candidate_id in enumerate(entity_candidates_id):
                    if candidate_id != "[FINISH_ID]":
                        # 获取候选实体的完整信息
                        candidate_info = self.entity_linker.get_node_full_info(candidate_id)
                        if candidate_info:
                            enriched_candidates.append({
                                "id": candidate_id,
                                "name": entity_candidates_name[idx],
                                "properties": candidate_info["properties"]
                            })
                        else:
                            enriched_candidates.append({
                                "id": candidate_id,
                                "name": entity_candidates_name[idx],
                                "properties": {}
                            })
                    else:
                        enriched_candidates.append({
                            "id": candidate_id,
                            "name": entity_candidates_name[idx],
                            "properties": {}
                        })

                # 使用大模型对候选实体进行智能判断
                if enriched_candidates:
                    can_answer, answer_from_entity, source_entities = self._check_if_entity_can_answer(
                        question, enriched_candidates
                    )
                    
                    if can_answer and answer_from_entity:
                        print(f"    深度 {depth} - 从实体属性中直接找到答案！")
                        final_answer = answer_from_entity
                        found_answer = True
                        
                        # 记录找到答案的实体信息
                        self._log("知识图谱检索", f"深度 {depth} - 从实体属性直接回答", {
                            "找到答案": True,
                            "答案": answer_from_entity,
                            "候选实体": [
                                {
                                    "id": c["id"],
                                    "name": c["name"],
                                    "属性数": len(c["properties"])
                                }
                                for c in enriched_candidates[:3]
                            ]
                        })
                        
                        # 继续搜索，尝试找到更多相关实体
                        # 不在这里直接返回，而是继续搜索其他可能的实体
                        # 这样可以找到多个相关实体，提供更完整的答案
                        print(f"    找到答案后继续搜索其他相关实体...")

                # 评分和更新
                scores, entity_candidates, entity_candidates_id = entity_score(
                    question, entity_candidates_id, entity_candidates_name,
                    entity['score'], entity['relation'], config
                )

                total_candidates, total_scores, total_relations, total_entities_id, total_topic_entities, total_head = update_history(
                    entity_candidates, entity, scores, entity_candidates_id,
                    total_candidates, total_scores, total_relations, total_entities_id,
                    total_topic_entities, total_head, len(entity_candidates_id) == 0
                )

            if len(total_candidates) == 0:
                print(f"    深度 {depth} 未找到候选实体")
                break

            # 实体剪枝
            flag, chain_of_entities, entities_id, pre_relations, pre_heads = entity_prune(
                total_entities_id, total_relations, total_candidates,
                total_topic_entities, total_head, total_scores, config, self.client, self.entity_linker
            )
            cluster_chain_of_entities.append(chain_of_entities)

            if flag:
                # 推理
                stop, answer = reasoning(question, cluster_chain_of_entities, config)
                if stop:
                    print(f"    在深度 {depth} 找到答案")
                    final_answer = answer
                    found_answer = True
                    break
                else:
                    print(f"    深度 {depth} 未找到答案，继续搜索")
                    flag_finish, entities_id = if_finish_list(entities_id)
                    if flag_finish:
                        break
                    else:
                        # 更新主题实体
                        topic_entity = {}
                        for entity in entities_id:
                            if entity != "[FINISH_ID]":
                                labels = self.client.query_all("qid2label", entity)
                                label = next(iter(labels)) if labels else "未知实体"
                                topic_entity[entity] = label
                        continue
            else:
                print(f"    深度 {depth} 实体剪枝失败")
                break

        # 如果没找到答案，使用LLM直接回答
        if not found_answer:
            final_answer = generate_without_explored_paths(question, config)

        # 记录最终推理结果
        self._log("知识图谱检索", "检索完成", {
            "是否找到答案": found_answer,
            "搜索深度": depth,
            "最终答案": final_answer,
            "推理链数量": len(cluster_chain_of_entities)
        })

        return final_answer, cluster_chain_of_entities, {
            "found_answer": found_answer,
            "depth_reached": depth,
            "topic_entity": topic_entity,
            "source_entities": source_entities if 'source_entities' in dir() else []
        }
    
    def _check_if_entity_can_answer(self, question: str, candidates: List[Dict]) -> tuple:
        """
        检查候选实体是否可以直接回答问题
        返回: (是否可以回答, 答案, 数据来源实体列表)
        """
        if not candidates:
            return False, None, []

        # 构建提示词，让大模型判断是否可以回答
        prompt = self._build_entity_answer_check_prompt(question, candidates)

        try:
            from algorithm.ans.QA.utils import run_llm
            response = run_llm(prompt, 0.1, 1500, self.args.LLM_API_KEY, self.args.LLM_TYPE)
            
            # 解析响应
            result = self._parse_entity_answer_check(response)
            
            if result.get("can_answer", False):
                answer = result.get("answer", "")
                # 再次检查答案是否真正回答了用户的问题
                if self._is_answer_sufficient(question, answer):
                    # 返回答案和相关的候选实体（用于构建answer_path）
                    return True, answer, candidates
                else:
                    print(f"  答案不足以回答问题: {answer}")
                    return False, None, []
            
            return False, None, []
            
        except Exception as e:
            print(f"实体回答检查异常: {e}")
            return False, None, []

    def _build_entity_answer_check_prompt(self, question: str, candidates: List[Dict]) -> str:
        """构建实体回答检查提示词"""
        
        candidates_text = ""
        for i, cand in enumerate(candidates[:5], 1):  # 最多检查前5个
            candidates_text += f"\n【候选实体 {i}】\n"
            candidates_text += f"名称: {cand['name']}\n"
            candidates_text += f"属性:\n"
            for key, value in cand['properties'].items():
                value_str = str(value)
                if len(value_str) > 300:
                    value_str = value_str[:300] + "..."
                candidates_text += f"  - {key}: {value_str}\n"

        # 使用原始字符串，避免f-string中的花括号问题
        prompt = """
你是一个智能问答助手。请根据用户的问题和候选实体的完整属性信息，判断是否可以直接从实体属性中找到答案。

用户问题："{question}"

候选实体信息：
{candidates_text}

请分析：
1. 候选实体的属性中是否包含回答问题的关键信息？
2. 如果需要多个实体的信息才能完整回答问题，请检查所有候选实体
3. 如果有足够信息，请直接生成答案
4. 如果只有部分信息，请基于现有信息生成尽可能完整的答案

输出格式（严格的JSON）：
{{
  "can_answer": true/false,
  "answer": "如果可以回答，请提供答案；如果不能，请填null",
  "reason": "判断理由"
}}

注意：
- 如果问题需要对比或计算（如：A和B的对比、A比B多多少等），请检查是否有多个相关实体
- 对于需要计算的问题（如：非公开采购金额 = 电子采购中标金额 - 公开采购中标金额），请检查是否能找到所有需要的实体
- 如果找到多个相关实体的SQL，请将所有SQL都返回，并说明计算逻辑
- 如果实体的属性中包含计算SQL等可以直接回答问题的信息，请设置 can_answer 为 true
- 答案应该直接、准确地回答用户的问题，包含所有必要的SQL语句和计算说明
- 例如：如果问题问"非公开采购金额"，但知识图谱中没有这个节点，可以通过"电子采购中标金额"和"公开采购中标金额"的SQL计算得到
- 对于"公开采购与非公开采购金额构成如何"的问题，只使用CG_001 电子采购中标金额和CG_003 公开采购中标金额这两个实体，不要使用CG_006 电子采购品类中标金额
- 不要过滤或忽略任何可能相关的属性，让用户自己判断哪些信息有用
""".format(question=question, candidates_text=candidates_text)

        return prompt

    def _parse_entity_answer_check(self, response: str) -> Dict:
        """解析实体回答检查的响应"""
        try:
            # 尝试提取JSON
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                return {
                    "can_answer": data.get("can_answer", False),
                    "answer": data.get("answer"),
                    "reason": data.get("reason", "")
                }
        except Exception as e:
            print(f"解析实体回答检查响应异常: {e}")
        
        return {"can_answer": False, "answer": None, "reason": "解析失败"}

    def _generate_answer_from_sql(self, sql: str, question: str) -> str:
        """
        根据SQL生成答案
        直接返回找到的SQL语句，供下游组件使用
        """
        try:
            # 直接返回找到的SQL语句，不做任何修改
            return f"我们找到了相关的SQL语句: {sql}，可以用于查询所需信息"
        except Exception as e:
            print(f"生成SQL答案异常: {e}")
            return f"SQL获取失败: {str(e)}"

    def _generate_answer_from_entity(self, entity: Dict, question: str) -> str:
        """
        根据实体信息生成答案
        """
        try:
            properties = entity.get('properties', {})
            
            if not properties:
                return "根据实体信息可以回答您的问题。"
            
            # 构建答案
            answer_parts = []
            
            # 动态处理所有属性，不预设优先级
            for key, value in properties.items():
                # 跳过空值
                if not value:
                    continue
                
                # 简单的属性值处理
                if isinstance(value, (str, int, float, bool)):
                    # 避免属性名重复或过于技术性的属性
                    if not key.startswith('_') and not key.lower() in ['id', 'qid', 'node_id']:
                        # 对于SQL语句，保留完整内容
                        if "sql" in key.lower() or "计算" in key:
                            answer_parts.append(f"{key}：{value}")
                        # 对于其他属性，限制长度
                        elif len(str(value)) <= 500:
                            answer_parts.append(f"{key}：{value}")
            
            # 限制答案长度，避免过长
            if len(answer_parts) > 10:
                answer_parts = answer_parts[:10]
            
            if answer_parts:
                return '；'.join(answer_parts) + '。'
            else:
                return "根据实体信息可以回答您的问题。"
        except Exception as e:
            print(f"生成实体答案异常: {e}")
            return "根据实体信息可以回答您的问题，但处理过程中出现异常。"

    def _is_answer_sufficient(self, question: str, answer: str) -> bool:
        """
        判断答案是否足够回答用户的问题
        """
        try:
            from algorithm.ans.QA.utils import run_llm
            
            prompt = f"""你是一个智能问答评估助手。请判断以下答案是否足够回答用户的问题。

用户问题："{question}"

提供的答案："{answer}"

请分析：
1. 答案是否直接回答了用户的问题？
2. 答案是否包含足够的信息来满足用户的需求？
3. 答案是否具体、明确，而不是泛泛而谈？

输出格式（严格的JSON）：
{{
  "sufficient": true/false,
  "reason": "判断理由"
}}

注意：
- 如果答案只是简单地重复实体名称或基本属性，而没有回答问题的核心内容，应判断为不足够
- 如果答案只是说"根据实体信息可以回答"但没有提供具体信息，应判断为不足够
- 只有当答案真正解决了用户的问题时，才判断为足够"""
            
            response = run_llm(prompt, 0.1, 500, self.args.LLM_API_KEY, self.args.LLM_TYPE)
            
            # 解析响应
            import re
            import json
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                sufficient = data.get("sufficient", False)
                reason = data.get("reason", "")
                print(f"  答案评估: {'足够' if sufficient else '不足够'}")
                print(f"  评估理由: {reason}")
                return sufficient
            
            return False
            
        except Exception as e:
            print(f"答案评估异常: {e}")
            return False

    def _convert_tog_result_to_solution(self, problem_id: str, problem_description: str, 
                                       answer: str, reasoning_chains: List, 
                                       search_result: Dict) -> SolutionModel:
        """将 ToG 结果转换为方案模型"""
        # 构建方案模型
        solution_data = {
            "方案ID": f"SOL-{problem_id.split('-')[1]}",
            "方案类别": "分析类问题解决方案",
            "方案目标": f"针对问题 '{problem_description}' 的解决方案",
            "输入": [problem_description],
            "输出": [answer] if answer else ["信息不足"],
            "约束": ["基于知识图谱检索", "遵循最佳实践"],
            "置信度": 0.9 if answer and answer != "信息不足" else 0.5
        }
        return SolutionModel(**solution_data)
