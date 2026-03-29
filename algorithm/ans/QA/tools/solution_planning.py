from QA.models import SolutionModel, ProblemSolutionMatch
from QA.entity_linker import EntityLinker
from QA.wiki_func import relation_search_prune, entity_search, del_all_unknown_entity, entity_score, update_history, entity_prune, reasoning, if_finish_list, generate_without_explored_paths
from typing import Dict, List, Tuple
from datetime import datetime


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
        try:
            # 从问题模型中提取信息
            problem_id = problem_model.get("问题ID", "PROB-001")
            problem_description = problem_model.get("问题描述", "")
            related_entities = problem_model.get("相关实体", [])
            
            # 1. 实体链接
            topic_entity = {}
            if related_entities:
                # 将实体文本转换为知识图谱中的实体ID
                for entity_text in related_entities:
                    try:
                        qids = self.client.query_all("label2qid", entity_text)
                        for qid in qids:
                            if qid != "Not Found!":
                                # 获取实体标签
                                labels = self.client.query_all("qid2label", qid)
                                label = next(iter(labels)) if labels else entity_text
                                topic_entity[qid] = label
                                break
                    except Exception:
                        continue
            
            # 如果没有找到实体，使用实体链接器
            if not topic_entity:
                linked_entities = self.entity_linker.link_entities_in_question(problem_description)
                topic_entity = linked_entities
            
            # 2. 调用 ToG 方法在知识图谱上检索
            answer, reasoning_chains, search_result = self._run_tog_search(
                question=problem_description,
                topic_entity=topic_entity,
                config=self.args
            )
            
            # 3. 将检索结果转换为方案模型
            solution = self._convert_tog_result_to_solution(
                problem_id=problem_id,
                problem_description=problem_description,
                answer=answer,
                reasoning_chains=reasoning_chains,
                search_result=search_result
            )
            
            # 4. 创建匹配关系
            match = ProblemSolutionMatch(
                问题ID=problem_id,
                方案ID=solution.方案ID,
                匹配度=0.9 if answer and answer != "信息不足" else 0.5,
                匹配理由="基于ToG知识图谱检索"
            )
            
            # 5. 构建图谱路径信息
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
                            path["nodes"].append({
                                "id": entity1,
                                "label": entity1
                            })
                            path["nodes"].append({
                                "id": entity2,
                                "label": entity2
                            })
                            path["relationships"].append({
                                "from": entity1,
                                "type": relation,
                                "to": entity2
                            })
                        elif isinstance(item, list):
                            # 格式为列表，可能包含多个实体和关系
                            for i, sub_item in enumerate(item):
                                if isinstance(sub_item, dict):
                                    # 处理实体
                                    if "entity" in sub_item:
                                        entity_id = sub_item["entity"]
                                        entity_name = sub_item.get("label", entity_id)
                                        path["nodes"].append({
                                            "id": entity_id,
                                            "label": entity_name
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
            
            return {
                "success": True,
                "solution": solution.dict(),
                "match": match.dict(),
                "graph_paths": graph_paths,
                "answer": answer,
                "entities": topic_entity
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
        复用你原有的 ToG 推理逻辑
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
            # 获取第一个关系的LLM响应（所有关系都包含相同的LLM响应）
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
                total_topic_entities, total_head, total_scores, config, self.client
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
            "topic_entity": topic_entity
        }
    
    
    def _convert_tog_result_to_solution(self, problem_id: str, problem_description: str, 
                                       answer: str, reasoning_chains: List, 
                                       search_result: Dict) -> SolutionModel:
        """将 ToG 结果转换为方案模型"""
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
