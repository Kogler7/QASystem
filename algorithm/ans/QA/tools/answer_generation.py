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


from algorithm.ans.QA.entity_linker import EntityLinker
from algorithm.ans.QA.wiki_func import relation_search_prune, entity_search, entity_score, update_history, entity_prune, generate_without_explored_paths
from typing import Dict, List, Tuple
from datetime import datetime
import re
import json


class AnswerGenerationTool:
    """
    答案生成工具：基于知识图谱的问答功能
    """
    
    def __init__(self, neo4j_client, args):
        self.client = neo4j_client
        self.args = args
        self.entity_linker = EntityLinker(neo4j_client, args)
    
    def generate_answer(self, question_or_intent: any) -> Dict:
        """
        生成最终答案
        输入：可以是意图识别的结构化输出，也可以是简单的问题字符串
        输出：最终答案
        """
        try:
            self.reasoning_logs = []  # 清空之前的日志
            
            # 初始化answer_history：用于存放与答案相关的实体信息
            # 结构：{实体名称: {属性: 值, 判断理由: "...", 有用信息: "..."}}
            answer_history = {}
            
            # 处理输入
            # 支持的输入格式：{"intent_id": "...", "user_goal": "...", ...}
            if isinstance(question_or_intent, str):
                # 如果是字符串，直接作为问题
                problem_description = question_or_intent
            else:
                # 从user_goal提取问题描述
                problem_description = question_or_intent.get("user_goal", "")
                if not problem_description:
                    problem_description = question_or_intent.get("goal", "")
            
            problem_id = f"PROB-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            
            print(f"\n{'═' * 60}")
            print(f"[1/4] 实体识别")
            print(f"{'═' * 60}")
            print(f"  问题: {problem_description}")
            
            topic_entity = {}
            link_result = self.entity_linker.link_entities_with_llm_judge(problem_description)
            
            all_candidates = link_result.get("all_candidates", [])
            relevant_entities = [cand for cand in all_candidates if cand.get("is_relevant", False)]
            
            print(f"\n{'─' * 60}")
            print(f"[2/4] 实体链接与排序")
            print(f"{'─' * 60}")
            print(f"  候选实体: {len(all_candidates)} 个")
            if all_candidates:
                print(f"  所有候选实体详情:")
                for i, cand in enumerate(all_candidates[:10], 1):
                    entity_name = cand['properties'].get('name', cand['qid'])
                    score = cand.get('relevance_score', 0)
                    mark = "✓相关" if cand.get('is_relevant') else "✗不相关"
                    properties = cand.get('properties', {})
                    print(f"    {i}. {entity_name} ({score:.2f}) {mark}")
                    print(f"       属性: {', '.join([f'{k}={v}' for k, v in properties.items()][:5])}")
                    if cand.get('llm_judgment'):
                        print(f"       LLM判断: {cand['llm_judgment'][:50]}...")
            print(f"  相关实体: {len(relevant_entities)} 个")
            
            is_direct_answer = False
            direct_answer_entities = []
            
            if relevant_entities:
                print(f"\n{'─' * 60}")
                print(f"[3/4] 答案判断")
                print(f"{'─' * 60}")
                for cand in relevant_entities:
                    entity_id = cand["qid"]
                    entity_name = cand["properties"].get("name", cand["qid"])
                    
                    print(f"\n  ┌──────────────────────────────────────────────")
                    print(f"  │ 实体: {entity_name}")
                    print(f"  │ ID: {entity_id}")
                    
                    try:
                        full_info = self.entity_linker.get_node_full_info(entity_id)
                        if full_info:
                            entity_properties = full_info.get("properties", cand["properties"])
                        else:
                            entity_properties = cand["properties"]
                    except Exception:
                        entity_properties = cand["properties"]
                    
                    print(f"  │ 属性查询结果:")
                    if entity_properties:
                        for key, value in entity_properties.items():
                            print(f"  │   {key}: {value}")
                    else:
                        print(f"  │   (无属性信息)")
                    
                    can_answer, answer_text = self._judge_entity_can_answer(
                        problem_description, entity_id, entity_name, entity_properties
                    )
                    
                    mark = "✓可回答" if can_answer else "✗不足以回答"
                    print(f"  │")
                    print(f"  │ LLM判断结果: {mark}")
                    if answer_text:
                        print(f"  │ 判断理由:")
                        # 按行输出，每行添加缩进
                        for line in answer_text.split('\n'):
                            print(f"  │   {line}")
                    else:
                        print(f"  │ 判断理由: N/A")
                    print(f"  └──────────────────────────────────────────────")
                    
                    if can_answer:
                        # 将可回答实体的信息存入answer_history
                        if entity_name not in answer_history:
                            answer_history[entity_name] = {
                                "entity_id": entity_id,
                                "properties": entity_properties.copy(),
                                "judgment_reason": answer_text,
                                "useful_info": []
                            }
                        # 提取有用的信息（如计算SQL等）
                        useful_info = self._extract_useful_info(entity_properties, answer_text)
                        if useful_info:
                            answer_history[entity_name]["useful_info"].append(useful_info)
                        
                        direct_answer_entities.append({
                            "id": entity_id,
                            "name": entity_name,
                            "properties": entity_properties,
                            "answer_text": answer_text
                        })
            
            # 判断当前history是否足以回答问题
            if answer_history:
                is_sufficient = self._is_history_sufficient(problem_description, answer_history)
            else:
                is_sufficient = False
            
            if is_sufficient:
                print(f"\n  → 当前收集的信息足以回答问题，无需继续搜索")
                reasoning_chains = []
                search_result = {
                    "found_answer": True,
                    "depth_reached": 0,
                    "topic_entity": link_result["linked_entities"],
                    "source_entities": direct_answer_entities
                }
                is_direct_answer = True
            elif direct_answer_entities:
                # 有可回答的实体但信息可能还不够完整
                print(f"\n  → 找到 {len(direct_answer_entities)} 个相关实体，继续搜索以获取更完整信息")
                topic_entity = link_result["linked_entities"]
                entity_names = list(topic_entity.values())
                print(f"\n{'─' * 60}")
                print(f"[4/4] ToG 图搜索")
                print(f"{'─' * 60}")
                print(f"  起始实体: {', '.join(entity_names[:5])}")
                
                answer, reasoning_chains, search_result = self._run_tog_search(
                    question=problem_description,
                    topic_entity=topic_entity,
                    config=self.args,
                    answer_history=answer_history
                )
                # 更新answer_history
                if 'answer_history' in search_result:
                    answer_history = search_result['answer_history']
                is_direct_answer = False
            else:
                topic_entity = link_result["linked_entities"]
                entity_names = list(topic_entity.values())
                print(f"\n{'─' * 60}")
                print(f"[3/4] 答案判断 → 全部不足以回答")
                print(f"{'─' * 60}")
                print(f"\n{'─' * 60}")
                print(f"[4/4] ToG 图搜索")
                print(f"{'─' * 60}")
                print(f"  起始实体: {', '.join(entity_names[:5])}")

                answer, reasoning_chains, search_result = self._run_tog_search(
                    question=problem_description,
                    topic_entity=topic_entity,
                    config=self.args,
                    answer_history=answer_history
                )
                # 更新answer_history
                if 'answer_history' in search_result:
                    answer_history = search_result['answer_history']
                is_direct_answer = False
            
            # 构建图谱路径信息
            graph_paths = {
                "answer_path": [],
                "reasoning_chains": reasoning_chains
            }
            
            # 从推理链中提取路径信息
            for depth, chain in enumerate(reasoning_chains):
                if chain:
                    for item in chain:
                        path = {
                            "depth": depth + 1,
                            "nodes": [],
                            "relationships": []
                        }
                        
                        if isinstance(item, tuple) and len(item) == 3:
                            entity1, relation, entity2 = item
                            
                            entity1_id = entity1
                            entity1_name = entity1
                            entity1_properties = {}
                            
                            if isinstance(entity1, dict):
                                entity1_id = entity1.get('id', entity1)
                                entity1_name = entity1.get('name', str(entity1))
                                entity1_properties = entity1.get('properties', {})
                            else:
                                for id, name in topic_entity.items():
                                    if name == entity1:
                                        entity1_id = id
                                        entity1_name = name
                                        break
                                if isinstance(entity1_id, str) and not entity1_id.startswith("NEO_"):
                                    try:
                                        candidates = self.entity_linker.search_entities_with_full_info(entity1, limit=1)
                                        if candidates:
                                            entity1_id = candidates[0].get("qid", entity1)
                                            entity1_properties = candidates[0].get("properties", {})
                                    except Exception:
                                        pass
                                try:
                                    entity1_info = self.entity_linker.get_node_full_info(entity1_id)
                                    if entity1_info:
                                        entity1_properties = entity1_info.get("properties", {})
                                except Exception:
                                    pass
                            
                            entity2_id = entity2
                            entity2_name = entity2
                            entity2_properties = {}
                            
                            if isinstance(entity2, dict):
                                entity2_id = entity2.get('id', entity2)
                                entity2_name = entity2.get('name', str(entity2))
                                entity2_properties = entity2.get('properties', {})
                            else:
                                for id, name in topic_entity.items():
                                    if name == entity2:
                                        entity2_id = id
                                        break
                                if isinstance(entity2_id, str) and not entity2_id.startswith("NEO_"):
                                    try:
                                        candidates = self.entity_linker.search_entities_with_full_info(entity2, limit=1)
                                        if candidates:
                                            entity2_id = candidates[0].get("qid", entity2)
                                    except Exception:
                                        pass
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
                            for i, sub_item in enumerate(item):
                                if isinstance(sub_item, dict):
                                    entity_id = sub_item.get("id", sub_item.get("entity", str(sub_item)))
                                    entity_name = sub_item.get("name", entity_id)
                                    entity_properties = {}
                                    
                                    if not entity_id.startswith("NEO_"):
                                        try:
                                            candidates = self.entity_linker.search_entities_with_full_info(entity_name, limit=1)
                                            if candidates:
                                                entity_id = candidates[0].get("qid", entity_id)
                                                entity_properties = candidates[0].get("properties", {})
                                        except Exception:
                                            pass
                                    
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
                                    if "relation" in sub_item and i > 0:
                                        relation_type = sub_item["relation"]
                                        path["relationships"].append({
                                            "from": item[i-1].get("entity"),
                                            "type": relation_type,
                                            "to": entity_id
                                        })
                        
                        if path["nodes"]:
                            graph_paths["answer_path"].append(path)
            
            # 如果没有从推理链中提取到路径，但有source_entities（直接回答的情况）
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
                pass
            
            # 生成最终的结构化答案
            direct_answer_text = direct_answer_entities[0].get("answer_text", "") if direct_answer_entities else ""
            final_answer = self._generate_final_answer(
                question=problem_description,
                answer_path=graph_paths["answer_path"],
                answer_history=answer_history,
                is_direct_answer=is_direct_answer,
                direct_answer_text=direct_answer_text
            )
            
            return {
                "success": True,
                "answer": final_answer
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "message": "答案生成失败"
            }
    
    def _generate_final_answer(self, question: str, answer_path: List,
                              answer_history: Dict = None,
                              is_direct_answer: bool = False, direct_answer_text: str = "") -> str:
        """
        生成最终答案
        使用大模型和提示词生成结构化答案
        如果是直接回答（is_direct_answer=True），则使用第二步判断中生成的答案
        如果有answer_history，则基于history中的信息生成答案
        """
        matched_entities = []
        entities_info = []
        
        # 如果有answer_history，先使用history中的信息
        if answer_history:
            for entity_name, entity_data in answer_history.items():
                matched_entities.append(entity_name)
                entities_info.append({
                    "label": entity_name,
                    "properties": entity_data.get("properties", {}),
                    "judgment_reason": entity_data.get("judgment_reason", ""),
                    "useful_info": entity_data.get("useful_info", [])
                })
        
        if answer_path:
            for path in answer_path:
                nodes = path.get("nodes", [])
                for node in nodes:
                    node_label = node.get('label', node.get('id', ''))
                    node_properties = node.get('properties', {})
                    if node_label and len(node_label) > 2:
                        matched_entities.append(node_label)
                        entities_info.append({
                            "label": node_label,
                            "properties": node_properties
                        })
        
        if is_direct_answer and entities_info:
            matched_entity = entities_info[0].get('label', '未知实体')
            
            # 构建answer_history的详细文本
            history_detail = ""
            if answer_history and len(answer_history) > 0:
                history_detail = "\n".join([
                    f"    - {name}: {'; '.join(info.get('useful_info', []))}"
                    for name, info in answer_history.items()
                ])
            
            # 如果有answer_history，让LLM基于问题和history重新生成答案
            if history_detail:
                detailed_answer = self._generate_answer_from_history(
                    question=question,
                    answer_history=answer_history
                )
            elif direct_answer_text:
                detailed_answer = direct_answer_text
            else:
                detailed_answer = self._generate_detailed_answer(question, entities_info)
            
            # 使用更详细的答案格式
            if history_detail:
                structured_answer = f"""o 根据业务模型识别到该问题对应的业务活动是：{matched_entity.split(' ')[0] if ' ' in matched_entity else matched_entity}
o 根据指标模型匹配：{', '.join(answer_history.keys())}
o 关键发现：
{history_detail}
o 最终答案：{detailed_answer}"""
            else:
                structured_answer = f"""o 根据业务模型识别到该问题对应的业务活动是：{matched_entity.split(' ')[0] if ' ' in matched_entity else matched_entity}
o 根据指标模型匹配：{matched_entity}
o 推导：不适用
o 因此，业务被转化为用指标描述的业务，最终答案：{detailed_answer}"""
            return structured_answer
        
        # 使用大模型生成结构化答案
        try:
            from algorithm.ans.QA.utils import run_llm
            from algorithm.ans.QA.prompt_list import structured_answer_prompt
            
            matched_entities_text = "\n".join([f"- {e}" for e in matched_entities]) if matched_entities else "无"
            
            entities_info_text = ""
            for i, entity in enumerate(entities_info, 1):
                entities_info_text += f"\n【实体{i}】{entity['label']}\n"
                for key, value in entity['properties'].items():
                    value_str = str(value)
                    if len(value_str) > 200:
                        value_str = value_str[:200] + "..."
                    entities_info_text += f"  {key}: {value_str}\n"
            
            prompt = structured_answer_prompt.format(
                question=question,
                matched_entities_text=matched_entities_text,
                entities_info_text=entities_info_text
            )
            
            response = run_llm(prompt, 0.3, 1500, self.args.LLM_API_KEY, self.args.LLM_TYPE)
            
            import re
            response = re.sub(r'<think>.*?</think>', '', response, flags=re.DOTALL)
            response = re.sub(r'<think>.*', '', response, flags=re.DOTALL)
            response = response.strip()
            
            if response:
                return response
            else:
                return "根据知识图谱中的信息，可以回答您的问题。"
                
        except Exception as e:
            print(f"生成结构化答案异常: {e}")
            return "根据知识图谱中的信息，可以回答您的问题。"
    
    def _extract_useful_info(self, entity_properties: Dict, judgment_reason: str) -> str:
        """
        从实体属性中提取对回答问题有用的信息
        现在简化为直接使用LLM的判断理由作为有用信息
        """
        return judgment_reason if judgment_reason else ""
    
    def _is_history_sufficient(self, question: str, answer_history: Dict) -> bool:
        """
        判断当前answer_history中的信息是否足以回答问题
        返回: True表示信息充足，可以生成答案；False表示信息不足，需要继续搜索
        """
        if not answer_history:
            return False
        
        try:
            from algorithm.ans.QA.utils import run_llm
            
            # 构建history信息文本
            history_text = ""
            for entity_name, entity_data in answer_history.items():
                history_text += f"\n【{entity_name}】\n"
                history_text += f"  判断理由: {entity_data.get('judgment_reason', '')}\n"
                if entity_data.get('useful_info'):
                    history_text += f"  关键信息: {'; '.join(entity_data['useful_info'])}\n"
            
            # 构建prompt
            prompt = f"""请判断以下收集到的信息是否足以回答用户的问题。

问题：{question}

已收集到的信息：
{history_text}

请判断：这些信息是否足以回答问题？不需要生成完整答案，只需要判断是或否。

输出格式（严格的JSON）：
{{
  "sufficient": true/false,
  "reason": "判断理由"
}}

判断原则：
1. 如果收集到的信息包含回答问题所需的关键要素（如指标定义、计算方法、数据来源、业务流程等），则认为信息充足
2. 如果明显缺少关键信息（如缺少计算公式、缺少必要的数据表信息等），则认为信息不足
3. 即使信息不完整但包含关键线索，也可以认为信息充足，让最终答案生成阶段处理
"""
            
            response = run_llm(prompt, 0.1, 500, self.args.LLM_API_KEY, self.args.LLM_TYPE)
            
            import re
            import json
            
            response = re.sub(r'<think>.*?</think>', '', response, flags=re.DOTALL)
            response = response.strip()
            
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                try:
                    json_data = json.loads(json_match.group())
                    print(f"\n  → History充足性判断: {'✓充足' if json_data.get('sufficient') else '✗不足'}")
                    print(f"    判断理由: {json_data.get('reason', '')[:50]}...")
                    return json_data.get('sufficient', False)
                except Exception:
                    return len(answer_history) >= 2  # 默认规则：至少2个相关实体
            
            return len(answer_history) >= 2  # 默认规则：至少2个相关实体
            
        except Exception as e:
            print(f"判断history充足性异常: {e}")
            return len(answer_history) >= 2  # 默认规则
    
    def _generate_answer_from_history(self, question: str, answer_history: Dict) -> str:
        """
        根据问题和answer_history生成最终答案
        让LLM综合所有收集到的信息来生成更准确的答案
        """
        try:
            from algorithm.ans.QA.utils import run_llm
            
            # 构建history信息文本
            history_text = ""
            for entity_name, entity_data in answer_history.items():
                history_text += f"\n【{entity_name}】\n"
                history_text += f"  判断理由: {entity_data.get('judgment_reason', '')}\n"
                if entity_data.get('useful_info'):
                    history_text += f"  关键信息: {'; '.join(entity_data['useful_info'])}\n"
            
            # 构建prompt
            prompt = f"""请根据以下问题和收集到的信息，生成一个详细、准确的答案。

问题：{question}

收集到的相关信息：
{history_text}

请基于以上信息，用自然、友好的语言回答问题。如果信息中有SQL语句，请解释如何使用该SQL获取所需数据。
"""
            
            # 调用LLM生成答案
            response = run_llm(prompt, 0.3, 1500, self.args.LLM_API_KEY, self.args.LLM_TYPE)
            
            # 清理响应
            import re
            response = re.sub(r'<think>.*?</think>', '', response, flags=re.DOTALL)
            response = re.sub(r'<think>.*', '', response, flags=re.DOTALL)
            response = response.strip()
            
            return response if response else "根据收集到的信息，可以回答您的问题。"
            
        except Exception as e:
            print(f"基于history生成答案异常: {e}")
            # 如果LLM调用失败，返回第一个实体的判断理由
            if answer_history:
                first_entity = list(answer_history.values())[0]
                return first_entity.get('judgment_reason', '无法生成答案')
            return "无法生成答案"
    
    def _judge_entity_can_answer(self, question: str, entity_id: str, entity_name: str, entity_properties: Dict) -> tuple:
        """
        第二步判断：基于实体完整信息判断是否能回答问题
        返回: (can_answer: bool, answer_text: str)
        """
        from algorithm.ans.QA.utils import run_llm
        from algorithm.ans.QA.prompt_list import entity_answer_judge_prompt
        from algorithm.ans.QA.rules_config import format_rules_for_prompt
        
        entity_info_text = f"实体名称: {entity_name}\n实体ID: {entity_id}\n"
        for key, value in entity_properties.items():
            value_str = str(value)
            if len(value_str) > 500:
                value_str = value_str[:500] + "..."
            entity_info_text += f"  {key}: {value_str}\n"
        
        # 加载适用的业务规则
        rules_text = format_rules_for_prompt(question)
        
        prompt = entity_answer_judge_prompt.format(
            rules_text=rules_text,
            question=question,
            entity_info_text=entity_info_text
        )
        
        try:
            response = run_llm(prompt, 0.1, 1500, self.args.LLM_API_KEY, self.args.LLM_TYPE)
            
            import re
            response = re.sub(r'熟虑.*?总结', '', response, flags=re.DOTALL)
            response = re.sub(r'熟虑.*', '', response, flags=re.DOTALL)
            response = response.strip()
            
            json_data = None
            try:
                json_match = re.search(r'\{[\s\S]*\}', response)
                if json_match:
                    json_data = json.loads(json_match.group())
            except Exception:
                pass
            
            if json_data:
                can_answer = json_data.get("can_answer", False)
                answer_text = json_data.get("answer", "")
                reason = json_data.get("reason", "")
                
                # 检查该实体是否是业务规则中所需的实体
                from algorithm.ans.QA.rules_config import get_applicable_rules
                applicable_rules = get_applicable_rules(question)
                is_required_entity = False
                for rule in applicable_rules:
                    if entity_name in rule.get("required_entities", []):
                        is_required_entity = True
                        break
                
                # 如果实体是业务规则所需的，强制标记为可回答
                if is_required_entity:
                    new_reason = f"{reason} 根据业务规则，该实体是计算非公开采购金额所需的关键实体。"
                    return True, new_reason
                elif can_answer and answer_text:
                    return True, answer_text
                else:
                    return False, reason
            else:
                return False, "解析大模型响应失败"
                
        except Exception as e:
            return False, str(e)
    
    def _judge_triple_can_answer(self, question: str, triple_info: Dict) -> tuple:
        """
        判断三元组是否对回答问题有用
        triple_info: {h_name, h_id, h_properties, relation, t_name, t_id, t_properties}
        返回: (is_useful: bool, reason: str)
        """
        from algorithm.ans.QA.utils import run_llm
        from algorithm.ans.QA.prompt_list import triple_answer_judge_prompt
        
        h_name = triple_info.get('h_name', '')
        h_properties = triple_info.get('h_properties', {})
        relation = triple_info.get('relation', '')
        t_name = triple_info.get('t_name', '')
        t_properties = triple_info.get('t_properties', {})
        
        # 构建三元组信息文本
        triple_text = f"三元组: {h_name} —[{relation}]→ {t_name}\n"
        
        # 添加头实体属性
        if h_properties:
            triple_text += f"{h_name} 的属性:\n"
            for key, value in h_properties.items():
                value_str = str(value)
                if len(value_str) > 200:
                    value_str = value_str[:200] + "..."
                triple_text += f"  {key}: {value_str}\n"
        
        # 添加尾实体属性
        if t_properties:
            triple_text += f"{t_name} 的属性:\n"
            for key, value in t_properties.items():
                value_str = str(value)
                if len(value_str) > 200:
                    value_str = value_str[:200] + "..."
                triple_text += f"  {key}: {value_str}\n"
        
        prompt = triple_answer_judge_prompt.format(
            question=question,
            triple_info=triple_text
        )
        
        try:
            response = run_llm(prompt, 0.1, 1500, self.args.LLM_API_KEY, self.args.LLM_TYPE)
            
            import re
            response = response.strip()
            
            json_data = None
            try:
                json_match = re.search(r'\{[\s\S]*\}', response)
                if json_match:
                    json_data = json.loads(json_match.group())
            except Exception:
                pass
            
            if json_data:
                is_useful = json_data.get("is_useful", False)
                reason = json_data.get("reason", "")
                
                return is_useful, reason
            else:
                # 如果解析失败，简单判断
                if "有用" in response or "可以回答" in response or "有助于" in response:
                    return True, response
                return False, "解析大模型响应失败"
                
        except Exception as e:
            return False, str(e)
    
    def _generate_detailed_answer(self, question: str, entities_info: List[Dict]) -> str:
        """
        根据实体属性生成详细的自然语言答案
        """
        if not entities_info:
            return "未找到相关实体信息"
        
        entities_info_text = ""
        for i, entity in enumerate(entities_info, 1):
            entities_info_text += f"\n【实体{i}】{entity['label']}\n"
            for key, value in entity['properties'].items():
                value_str = str(value)
                if len(value_str) > 500:
                    value_str = value_str[:500] + "..."
                entities_info_text += f"  {key}: {value_str}\n"
        
        prompt = f"""你是一个智能问答助手。请根据用户的问题和实体的完整属性信息，直接生成详细的自然语言答案。

用户问题："{question}"

实体信息：
{entities_info_text}

请基于实体的属性信息，直接生成自然语言的答案。
答案要求：
1. 直接回答用户问题
2. 包含实体中的关键属性信息
3. 如果实体有计算SQL或数据来源等信息，可以一并说明
4. 答案要准确、清晰、完整

直接输出答案，不要输出其他内容。"""
        
        try:
            from algorithm.ans.QA.utils import run_llm
            response = run_llm(prompt, 0.1, 2000, self.args.LLM_API_KEY, self.args.LLM_TYPE)
            return response.strip()
        except Exception as e:
            print(f"从实体属性生成详细答案异常: {e}")
            return "\n".join([f"{entity['label']}: {', '.join(entity['properties'].keys())}" for entity in entities_info])

    def _run_tog_search(self, question: str, topic_entity: Dict, config, answer_history: Dict = None) -> tuple:
        cluster_chain_of_entities = []
        pre_relations = []
        pre_heads = [-1] * len(topic_entity)
        found_answer = False
        final_answer = "信息不足"
        depth = 0

        for depth in range(1, config.DEPTH + 1):
            print(f"\n  [深度{depth}] 关系搜索:")
            total_entities_id = []
            total_relations = []
            total_candidates = []
            total_topic_entities = []
            total_head = []
            total_scores = []

            i = 0
            for entity in topic_entity:
                pre_head_value = pre_heads[i] if i < len(pre_heads) else -1

                retrieve_relations_with_scores = relation_search_prune(
                    entity, topic_entity[entity], pre_relations,
                    pre_head_value, question, config, self.client
                )

                for rel_info in retrieve_relations_with_scores:
                    relation = rel_info['relation']
                    score = rel_info['score']
                    head = rel_info['head']
                    source_entity = rel_info['entity']
                    entity_name = topic_entity.get(entity, entity)
                    print(f"    {entity_name} → {relation} ({score:.2f})")

                    entity_candidates_id, entity_candidates = entity_search(
                        source_entity, relation, self.client, head
                    )

                    if entity_candidates_id:
                        scores, entity_candidates, entity_candidates_id = entity_score(
                            question, entity_candidates_id, entity_candidates,
                            score, relation, config
                        )

                        total_candidates, total_scores, total_relations, total_entities_id, total_topic_entities, total_head = update_history(
                            entity_candidates, rel_info, scores, entity_candidates_id,
                            total_candidates, total_scores, total_relations, total_entities_id,
                            total_topic_entities, total_head, False
                        )
                i += 1

            if not total_entities_id:
                print(f"  [深度{depth}] 未找到候选实体，搜索终止")
                break

            success, triple_list, entities_id, relations, heads = entity_prune(
                total_entities_id, total_relations, total_candidates,
                total_topic_entities, total_head, total_scores,
                config, self.client, self.entity_linker
            )

            if success:
                cluster_chain_of_entities.append(triple_list)
                print(f"\n  [深度{depth}] 实体搜索:")
                for item in triple_list:
                    if isinstance(item, tuple) and len(item) >= 3:
                        h, r, t = item
                        h_name = h.get('name', str(h)) if isinstance(h, dict) else str(h)
                        t_name = t.get('name', str(t)) if isinstance(t, dict) else str(t)
                        print(f"    {h_name} —[{r}]→ {t_name}")
                        
                        # 获取实体属性
                        h_properties = {}
                        t_properties = {}
                        h_id = h.get('id', '') if isinstance(h, dict) else ''
                        t_id = t.get('id', '') if isinstance(t, dict) else ''
                        
                        if h_id:
                            try:
                                h_full_info = self.entity_linker.get_node_full_info(h_id)
                                h_properties = h_full_info.get("properties", {}) if h_full_info else {}
                            except Exception:
                                pass
                        
                        if t_id:
                            try:
                                t_full_info = self.entity_linker.get_node_full_info(t_id)
                                t_properties = t_full_info.get("properties", {}) if t_full_info else {}
                            except Exception:
                                pass
                        
                        # 将三元组作为一个整体判断是否对回答问题有用
                        triple_info = {
                            "h_name": h_name,
                            "h_id": h_id,
                            "h_properties": h_properties,
                            "relation": r,
                            "t_name": t_name,
                            "t_id": t_id,
                            "t_properties": t_properties
                        }
                        
                        # 使用LLM判断这个三元组是否对回答问题有用
                        can_answer, answer_text = self._judge_triple_can_answer(question, triple_info)
                        
                        print(f"      └─ 三元组: {'✓有用' if can_answer else '✗无用'}")
                        
                        # 如果有用，将三元组信息加入answer_history
                        if can_answer:
                            triple_key = f"{h_name} —[{r}]→ {t_name}"
                            if triple_key not in answer_history:
                                answer_history[triple_key] = {
                                    "type": "triple",
                                    "triple_info": triple_info,
                                    "judgment_reason": answer_text,
                                    "useful_info": [triple_key]
                                }
                        
                        # 同时对每个实体单独判断（保留原有逻辑）
                        for entity_item, entity_name, entity_id, entity_properties in [(h, h_name, h_id, h_properties), (t, t_name, t_id, t_properties)]:
                            if isinstance(entity_item, dict) and entity_id and entity_name:
                                try:
                                    # 使用LLM判断该实体是否能回答问题
                                    can_answer_entity, answer_text_entity = self._judge_entity_can_answer(
                                        question, entity_id, entity_name, entity_properties
                                    )
                                    
                                    print(f"      └─ {entity_name}: {'✓可回答' if can_answer_entity else '✗不足以回答'}")
                                    
                                    # 如果可回答，加入answer_history
                                    if can_answer_entity:
                                        if entity_name not in answer_history:
                                            answer_history[entity_name] = {
                                                "type": "entity",
                                                "entity_id": entity_id,
                                                "properties": entity_properties.copy(),
                                                "judgment_reason": answer_text_entity,
                                                "useful_info": []
                                            }
                                            useful_info = self._extract_useful_info(entity_properties, answer_text_entity)
                                            if useful_info:
                                                answer_history[entity_name]["useful_info"].append(useful_info)
                                except Exception as e:
                                    print(f"      └─ {entity_name}: 属性查询失败 - {str(e)[:30]}")

                # 统一使用answer_history判断信息是否充足
                if answer_history:
                    is_sufficient = self._is_history_sufficient(question, answer_history)
                else:
                    is_sufficient = False
                
                if is_sufficient:
                    print(f"  [深度{depth}] History信息充足 ✓，停止搜索")
                    found_answer = True
                    break
                else:
                    print(f"  [深度{depth}] History信息不足，继续搜索")
                    topic_entity = {}
                    for entity in entities_id:
                        labels = self.client.query_all("qid2label", entity)
                        label = next(iter(labels)) if labels else "未知实体"
                        topic_entity[entity] = label
                    pre_relations = relations
                    pre_heads = heads
                    continue
            else:
                print(f"  [深度{depth}] 实体剪枝失败，搜索终止")
                break

        if not found_answer:
            final_answer = generate_without_explored_paths(question, config)

        return final_answer, cluster_chain_of_entities, {
            "found_answer": found_answer,
            "depth_reached": depth,
            "topic_entity": topic_entity,
            "source_entities": [],
            "answer_history": answer_history
        }

    def search_single_depth(self, question: str, topic_entity: Dict, depth: int = 1, args=None) -> Dict:
        """单深度图搜索：执行一个深度的图搜索，返回原始三元组（不做LLM判断）

        此方法供 MCP 细粒度工具调用，只返回原始数据，由 Claude Code 自行判断。
        
        Args:
            question: 问题文本
            topic_entity: 起始实体 {entity_id: entity_name}
            depth: 当前搜索深度
            args: 配置参数
        
        Returns:
            {"depth": int, "triples": [...], "found_entities": [...], "has_more": bool}
        """
        if args is None:
            args = self.args
        
        if isinstance(args, dict):
            width = args.get("WIDTH", 5)
        else:
            width = getattr(args, 'WIDTH', 5)
        
        total_entities_id = []
        total_relations = []
        total_candidates = []
        total_topic_entities = []
        total_head = []
        total_scores = []
        
        i = 0
        for entity_id in topic_entity:
            entity_name = topic_entity[entity_id]
            
            retrieve_relations_with_scores = relation_search_prune(
                entity_id, entity_name, [], -1, question, args, self.client
            )
            
            for rel_info in retrieve_relations_with_scores:
                relation = rel_info['relation']
                score = rel_info['score']
                head = rel_info['head']
                source_entity = rel_info['entity']
                
                entity_candidates_id, entity_candidates = entity_search(
                    source_entity, relation, self.client, head
                )
                
                if entity_candidates_id:
                    scores, entity_candidates, entity_candidates_id = entity_score(
                        question, entity_candidates_id, entity_candidates,
                        score, relation, args
                    )
                    
                    total_candidates, total_scores, total_relations, total_entities_id, total_topic_entities, total_head = update_history(
                        entity_candidates, rel_info, scores, entity_candidates_id,
                        total_candidates, total_scores, total_relations, total_entities_id,
                        total_topic_entities, total_head, False
                    )
            i += 1
        
        if not total_entities_id:
            return {"depth": depth, "triples": [], "found_entities": [], "has_more": False}
        
        success, triple_list, entities_id, relations, heads = entity_prune(
            total_entities_id, total_relations, total_candidates,
            total_topic_entities, total_head, total_scores,
            args, self.client, self.entity_linker
        )
        
        if not success:
            return {"depth": depth, "triples": [], "found_entities": [], "has_more": False}
        
        triples = []
        found_entities = []
        seen_entity_ids = set()
        start_entity_ids = set(topic_entity.keys())
        
        for item in triple_list:
            if isinstance(item, tuple) and len(item) >= 3:
                h, r, t = item
                h_name = h.get('name', str(h)) if isinstance(h, dict) else str(h)
                h_id = h.get('id', '') if isinstance(h, dict) else ''
                h_props = h.get('properties', {}) if isinstance(h, dict) else {}
                t_name = t.get('name', str(t)) if isinstance(t, dict) else str(t)
                t_id = t.get('id', '') if isinstance(t, dict) else ''
                t_props = t.get('properties', {}) if isinstance(t, dict) else {}
                
                if t_id == '未知ID':
                    t_id = ''
                
                triples.append({
                    "head_entity": {"id": h_id, "name": h_name, "properties": h_props},
                    "relation": r,
                    "tail_entity": {"id": t_id, "name": t_name, "properties": t_props}
                })
                
                if t_id and t_id not in seen_entity_ids and t_id not in start_entity_ids:
                    seen_entity_ids.add(t_id)
                    found_entities.append({
                        "id": t_id, "name": t_name, "properties": t_props
                    })
        
        return {
            "depth": depth,
            "triples": triples,
            "found_entities": found_entities,
            "has_more": len(found_entities) > 0
        }