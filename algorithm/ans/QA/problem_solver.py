from models import ProblemModel, SolutionModel, ProblemSolutionMatch
from typing import Dict, List, Optional
from datetime import datetime
from wiki_func import *
from utils import *
from entity_linker import EntityLinker
import json


class ProblemSolver:

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

    def solve(self, user_input: str) -> Dict:
        """
        主求解流程 - 对应文档的四个步骤
        """
        self.reasoning_logs = []  # 清空推理日志
        print(f"\n{'=' * 60}")
        print(f"开始问题求解: {user_input}")

        # ===== 步骤1：问题定义 =====
        print("\n问题定义...")
        problem_model = self._define_problem(user_input)
        print(f"问题模型: {problem_model.问题ID} - {problem_model.问题类型}")

        # ===== 步骤2：问题分解 =====
        print("\n问题分解...")
        subproblems = self._decompose_problem(problem_model)
        print(f"分解为 {len(subproblems)} 个子问题")

        # ===== 步骤3：方案获取 =====
        print("\n方案获取...")
        solutions = []
        matches = []

        graph_paths = {"answer_path": [], "reasoning_chains": []}
        for i, subproblem in enumerate(subproblems):
            print(f"  处理子问题 {i + 1}/{len(subproblems)}: {subproblem.问题ID}")
            solution, match, sub_graph_paths = self._get_solution(subproblem)
            solutions.append(solution)
            matches.append(match)
            # 合并图谱路径
            graph_paths["answer_path"].extend(sub_graph_paths.get("answer_path", []))
            graph_paths["reasoning_chains"].extend(sub_graph_paths.get("reasoning_chains", []))

        # ===== 步骤4：物理组件匹配 =====
        print("\n物理组件匹配...")
        execution_plans = []

        # ===== 组装最终结果 =====
        result = {
            "输入问题": user_input,
            "问题模型": problem_model.dict(),
            "子问题": [sp.dict() for sp in subproblems],
            "方案模型": [s.dict() for s in solutions],
            "问题-方案匹配": [m.dict() for m in matches],
            "执行计划": [ep.dict() for ep in execution_plans],
            "图谱路径": graph_paths,
            "求解时间": datetime.now().isoformat(),
            "推理日志": self.reasoning_logs
        }

        print(f"\n{'=' * 60}")
        print("求解完成!")
        return result

    def _define_problem(self, user_input: str) -> ProblemModel:
        """
        步骤1：问题定义
        输入：自然语言问题
        输出：结构化的问题模型
        """
        # 1. 先识别实体
        entity_texts = self.entity_linker.recognize_entities(user_input)

        # 2. 用LLM生成问题模型
        prompt = f"""
        你是一个问题分析专家。请将以下用户问题转化为结构化的问题模型。

        用户问题："{user_input}"

        从问题中提取的实体：{entity_texts}

        问题模型包含以下字段：
        - 问题ID：格式为 "QKB-三位数字"，如 QKB-001
        - 问题描述：清晰描述问题（可以用原问题）
        - 问题类型：从以下选择 [描述型问题, 诊断型问题, 预测型问题, 指导型问题, 评估型问题, 规划型问题]
        - 约束：解决问题的限制条件（列表，至少1项）
        - 目标对象：问题针对的主要对象
        - 干系人：涉及的角色（列表，至少1项）
        - 目标：要达到的目的（列表，至少1项）
        - 当前状态：当前所处阶段（如"初始阶段"、"分析阶段"等）

        请用JSON格式输出，只输出JSON，不要其他内容。
        """
 
        # 记录问题定义提示词
        self._log("问题定义", "生成问题模型提示词", {"prompt": prompt})

        response = run_llm(prompt, 0.2, 800, self.args.LLM_API_KEY, self.args.LLM_TYPE)

        # 记录LLM响应
        self._log("问题定义", "LLM响应", {"response": response})

        try:
            data = self._extract_json(response)
            if data:
                # 确保所有必要字段都存在
                if "问题ID" not in data:
                    data["问题ID"] = "PROB-001"
                if "相关实体" not in data:
                    data["相关实体"] = entity_texts
                if "创建时间" not in data:
                    data["创建时间"] = datetime.now().isoformat()

                # 记录问题模型生成结果
                self._log("问题定义", "生成问题模型结果", data)
                
                return ProblemModel(**data)
        except Exception as e:
            print(f"解析问题模型失败: {e}")
            self._log("问题定义", f"生成问题模型失败: {e}")



    def _decompose_problem(self, problem: ProblemModel) -> List[ProblemModel]:
        """
        步骤2：问题分解
        输入：问题模型
        输出：子问题列表
        """
        print(f"\n  问题分解：当前版本暂不分解，直接使用主问题 {problem.问题ID}")

        # 记录问题分解过程
        self._log("问题分解", f"当前版本暂不分解，直接使用主问题 {problem.问题ID}")

        # 确保返回的子问题中，问题类型是字符串，而不是枚举对象
        problem_dict = problem.dict()
        # 如果问题类型是枚举，转换为字符串
        if hasattr(problem_dict['问题类型'], 'value'):
            problem_dict['问题类型'] = problem_dict['问题类型'].value

        return [ProblemModel(**problem_dict)]

    def _get_solution(self, problem: ProblemModel) -> tuple[SolutionModel, ProblemSolutionMatch, Dict]:
        """
        步骤3：方案获取 - 使用 ToG 方法在知识图谱上检索
        输入：问题模型
        输出：方案模型 + 匹配关系 + 图谱路径
        """
        print(f"\n  开始为问题 {problem.问题ID} 获取方案...")
        print(f"  问题描述: {problem.问题描述}")

        # 从问题模型中提取实体作为起始实体
        topic_entity = {}
        if problem.相关实体:
            # 将实体文本转换为知识图谱中的实体ID
            for entity_text in problem.相关实体:
                try:
                    qids = self.client.query_all("label2qid", entity_text)
                    for qid in qids:
                        if qid != "Not Found!":
                            # 获取实体标签
                            labels = self.client.query_all("qid2label", qid)
                            label = next(iter(labels)) if labels else entity_text
                            topic_entity[qid] = label
                            break
                except Exception as e:
                    print(f"    实体 '{entity_text}' 转换失败: {e}")
                    continue

        # 如果没有找到实体，使用原有的实体链接器
        if not topic_entity and hasattr(self, 'entity_linker'):
            linked_entities = self.entity_linker.link_entities_in_question(problem.问题描述)
            topic_entity = linked_entities

        print(f"  起始实体: {topic_entity}")
        
        # 记录实体识别与链接结果
        self._log("实体识别与链接", f"识别到 {len(topic_entity)} 个实体", topic_entity)

        # 调用 ToG 方法在知识图谱上检索
        answer, reasoning_chains, search_result = self._run_tog_search(
            question=problem.问题描述,
            topic_entity=topic_entity,
            config=self.args
        )

        # 3. 将 ToG 的检索结果转换为方案模型
        solution = self._convert_tog_result_to_solution(
            problem=problem,
            answer=answer,
            reasoning_chains=reasoning_chains,
            search_result=search_result
        )

        #创建匹配关系
        match = ProblemSolutionMatch(
            问题ID=problem.问题ID,
            方案ID=solution.方案ID,
            匹配度=0.9 if answer and answer != "信息不足" else 0.5,
            匹配理由="基于ToG知识图谱检索"
        )

        # 构建图谱路径信息
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

        # 打印提取的路径信息，以便调试
        print(f"提取的 answer_path: {graph_paths['answer_path']}")

        return solution, match, graph_paths

    def _match_physical_components(self, solutions: List[SolutionModel]) -> List:
        """
        步骤4：物理组件匹配 - 当前暂不实现
        直接返回空列表
        """
        print("  物理组件匹配：暂不实现")
        return []

    def _load_available_components(self) -> Dict:
        """加载可用物理组件"""
        # 可以从配置文件加载
        return {}

    def _extract_json(self, response: str) -> Optional[Dict]:
        """从响应中提取JSON"""
        try:
            # 匹配JSON代码块
            json_match = re.search(r'```json\n(.*?)\n```', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(1))

            # 匹配大括号内容
            brace_match = re.search(r'\{.*\}', response, re.DOTALL)
            if brace_match:
                return json.loads(brace_match.group())

            # 尝试直接解析
            return json.loads(response)
        except:
            return None

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

    def _convert_tog_result_to_solution(self, problem: ProblemModel, answer: str,
                                        reasoning_chains: List, search_result: Dict) -> SolutionModel:
        """
        将 ToG 检索结果转换为方案模型
        """

        # 将问题类型转换为对应的方案类别
        problem_type_value = problem.问题类型
        # 如果是枚举对象，获取其值
        if hasattr(problem_type_value, 'value'):
            problem_type_value = problem_type_value.value

        # 问题类型到方案类别的映射
        type_to_category = {
            "描述型问题": "评估类问题解决方案",
            "诊断型问题": "诊断类问题解决方案",
            "预测型问题": "预测类问题解决方案",
            "指导型问题": "规划类问题解决方案",
            "评估型问题": "评估类问题解决方案",
            "规划型问题": "规划类问题解决方案"
        }

        # 获取对应的方案类别，如果没有映射则使用默认值
        category = type_to_category.get(problem_type_value, "分析类问题解决方案")

        # 计算置信度
        confidence = 0.5
        if reasoning_chains and any(chain for chain in reasoning_chains if chain):
            confidence += 0.2
        if answer and answer != "信息不足" and "无法" not in answer and "不能" not in answer:
            confidence += 0.2
        if search_result.get("depth_reached", 0) >= 3:
            confidence += 0.1
        confidence = min(confidence, 1.0)

        # 构建控制逻辑描述
        if reasoning_chains and any(chain for chain in reasoning_chains if chain):
            chain_count = sum(1 for chain in reasoning_chains if chain)
            control_logic = f"通过ToG在知识图谱上进行{chain_count}层搜索"
        else:
            control_logic = "未找到推理路径，使用LLM直接生成答案"

        # 方案模型的输出就是答案本身
        output = [answer] if answer and answer != "信息不足" else ["未找到明确答案"]

        # 构建方案模型
        solution = SolutionModel(
            方案ID=f"SOL-{problem.问题ID}",
            方案类别=category,  # 使用映射后的方案类别
            方案目标=f"通过知识图谱检索回答：{problem.问题描述}",
            输入=["问题模型"] + (problem.相关实体 or []),
            输出=output,
            约束=problem.约束,
            控制逻辑=control_logic,
            置信度=confidence
        )
        
        # 记录方案模型生成过程
        self._log("方案规划", "生成方案模型", {
            "方案ID": solution.方案ID,
            "方案类别": solution.方案类别,
            "方案目标": solution.方案目标,
            "输入": solution.输入,
            "输出": solution.输出,
            "约束": solution.约束,
            "控制逻辑": solution.控制逻辑,
            "置信度": solution.置信度
        })
        
        return solution