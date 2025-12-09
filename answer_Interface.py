import argparse
import json
from datetime import datetime
from typing import Dict, Any, List, Optional
import traceback

from neo4j_client import MultiServerWikidataQueryClient
from entity_linker import EntityLinker
from wiki_func import *
from utils import *

class Neo4jQAService:
    """Neo4j知识图谱问答服务类"""

    def __init__(self, config=None):
        """
        初始化服务

        Args:
            config: 配置参数，如果为None则使用默认配置
        """
        if config is None:
            config = self.create_default_config()
        self.config = config

        # 初始化客户端
        self._initialize_clients()

        print("Neo4j知识图谱问答服务初始化完成")

    @staticmethod
    def create_default_config():
        """创建默认配置"""
        class Config:
            pass
        config = Config()
        # 设置默认参数
        config.max_length = 256
        config.temperature_exploration = 0.4
        config.temperature_reasoning = 0
        config.width = 3
        config.depth = 3
        config.remove_unnecessary_rel = True
        config.LLM_type = "Qwen/Qwen3-8B"
        config.opeani_api_keys = "none"
        config.num_retain_entity = 5
        config.prune_tools = "llm"
        config.auto_entity_linking = True

        # Neo4j连接参数
        config.neo4j_uri = "bolt://localhost:7687"
        config.neo4j_user = "neo4j"
        config.neo4j_password = "123456"

        return config

    def _initialize_clients(self):
        """初始化客户端"""
        try:
            self.wiki_client = MultiServerWikidataQueryClient(
                neo4j_uri=self.config.neo4j_uri,
                username=self.config.neo4j_user,
                password=self.config.neo4j_password
            )
            print("Neo4j客户端初始化成功")

            # 初始化实体链接器
            if self.config.auto_entity_linking:
                self.entity_linker = EntityLinker(self.wiki_client, self.config)
                print("实体链接器初始化成功")
            else:
                self.entity_linker = None

        except Exception as e:
            print(f"客户端初始化失败: {e}")
            raise

    def answer_question(self, question: str) -> Dict[str, Any]:
        """
        回答单个问题
        Args:
            question: 问题字符串
        Returns:
            包含答案和元数据的字典
        """
        print(f"\n{'=' * 50}")
        try:
            # 自动实体识别和链接
            topic_entity = {}
            if self.config.auto_entity_linking and self.entity_linker:
                linked_entities = self.entity_linker.link_entities_in_question(question)
                # 转换格式
                for qid, label in linked_entities.items():
                    topic_entity[qid] = label

                if not topic_entity:
                    print("未识别到实体，使用LLM直接回答")
                    answer = generate_without_explored_paths(question, self.config)
                    return {
                        "success": True,
                        "question": question,
                        "answer": answer,
                        "entities": [],
                        "reasoning_chains": [],
                        "error": None,
                        "timestamp": datetime.now().isoformat()
                    }

            # 如果没有实体，直接回答
            if len(topic_entity) == 0:
                answer = generate_without_explored_paths(question, self.config)
                return {
                    "success": True,
                    "question": question,
                    "answer": answer,
                    "entities": [],
                    "reasoning_chains": [],
                    "error": None,
                    "timestamp": datetime.now().isoformat()
                }

            # ToG推理过程
            cluster_chain_of_entities = []
            pre_relations = []
            pre_heads = [-1] * len(topic_entity)
            found_answer = False

            for depth in range(1, self.config.depth + 1):
                print(f"深度 {depth} 搜索...")

                current_entity_relations_list = []
                i = 0
                for entity in topic_entity:
                    if entity != "[FINISH_ID]":
                        pre_head_value = pre_heads[i] if i < len(pre_heads) else -1

                        retrieve_relations_with_scores = relation_search_prune(
                            entity, topic_entity[entity], pre_relations,
                            pre_head_value, question, self.config, self.wiki_client
                        )
                        current_entity_relations_list.extend(retrieve_relations_with_scores)
                    i += 1

                # 如果没有找到关系，跳出
                if not current_entity_relations_list:
                    print(f"深度 {depth} 未找到关系")
                    break

                # 收集候选实体
                total_candidates, total_scores, total_relations, total_entities_id, total_topic_entities, total_head = [], [], [], [], [], []

                for entity in current_entity_relations_list:
                    direction_outgoing = entity.get('head', True)

                    if direction_outgoing:
                        entity_candidates_id, entity_candidates_name = entity_search(
                            entity['entity'], entity['relation'], self.wiki_client, True
                        )
                    else:
                        entity_candidates_id, entity_candidates_name = entity_search(
                            entity['entity'], entity['relation'], self.wiki_client, False
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
                        entity['score'], entity['relation'], self.config
                    )

                    total_candidates, total_scores, total_relations, total_entities_id, total_topic_entities, total_head = update_history(
                        entity_candidates, entity, scores, entity_candidates_id,
                        total_candidates, total_scores, total_relations, total_entities_id,
                        total_topic_entities, total_head, len(entity_candidates_id) == 0
                    )

                if len(total_candidates) == 0:
                    print(f"深度 {depth} 未找到候选实体")
                    break

                # 实体剪枝
                flag, chain_of_entities, entities_id, pre_relations, pre_heads = entity_prune(
                    total_entities_id, total_relations, total_candidates,
                    total_topic_entities, total_head, total_scores, self.config, self.wiki_client
                )
                cluster_chain_of_entities.append(chain_of_entities)
                if flag:
                    # 推理
                    stop, answer = reasoning(question, cluster_chain_of_entities, self.config)
                    if stop:
                        print(f"在深度 {depth} 找到答案")
                        return {
                            "success": True,
                            "question": question,
                            "answer": answer,
                            "entities": list(topic_entity.items()),
                            "reasoning_chains": cluster_chain_of_entities,
                            "error": None,
                            "timestamp": datetime.now().isoformat()
                        }
                    else:
                        print(f"深度 {depth} 未找到答案，继续搜索")
                        flag_finish, entities_id = if_finish_list(entities_id)
                        if flag_finish:
                            break
                        else:
                            # 更新主题实体
                            topic_entity = {}
                            for entity in entities_id:
                                if entity != "[FINISH_ID]":
                                    labels = self.wiki_client.query_all("qid2label", entity)
                                    label = next(iter(labels)) if labels else "未知实体"
                                    topic_entity[entity] = label
                            continue
                else:
                    print(f"深度 {depth} 实体剪枝失败")
                    break

            # 如果遍历完深度仍未找到答案
            answer = generate_without_explored_paths(question, self.config)
            return {
                "success": True,
                "question": question,
                "answer": answer,
                "entities": list(topic_entity.items()),
                "reasoning_chains": cluster_chain_of_entities,
                "error": None,
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            print(f"处理问题失败: {e}")
            traceback.print_exc()

            return {
                "success": False,
                "question": question,
                "answer": f"处理失败: {str(e)}",
                "entities": [],
                "reasoning_chains": [],
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }


    def close(self):
        """关闭连接"""
        if hasattr(self, 'wiki_client') and self.wiki_client:
            self.wiki_client.close()
            print("已关闭Neo4j连接")

