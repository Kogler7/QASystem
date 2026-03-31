import logging
import os
import sys
import time

from pymilvus import MilvusClient, MilvusException, Collection, connections

from ir_config.config import Config
from ir_utils.encoder import HybridEncoder
from data.intent_model import Intent
from init_database.init_database import DatabaseInit
from ir_utils.database_utils import DatabaseUtils

# 配置日志
config = Config()

class ModifyIntent:
    def __init__(self):
        self.config = Config()
        self.data_dir = self.config.data_dir
        self.collection_name = self.config.collection_name
        self.milvus_uri = self.config.milvus_uri
        self.database_utils = DatabaseUtils()

        print("正在初始化编码器...")
        self.encoder = HybridEncoder()
        self.dense_dim = self.encoder.dense_dim
        self.sparse_dim = self.encoder.sparse_dim

        print(f"正在连接到 Milvus: {self.milvus_uri}")
        connections.connect(hosts=self.milvus_uri)
        self.milvus_client = self.database_utils.init_milvus_client()
        self.collection = Collection(name=self.collection_name)

    def add_intent(self,intent:Intent) -> str:
        """添加意图"""
        print("正在添加意图...")

        # 检查意图ID是否已存在
        intent_ids = self.query_all_intents_id()
        if intent.intent_id in intent_ids:
            print(f"意图ID {intent.intent_id} 已存在")
            return f"意图ID {intent.intent_id} 已存在"

        # 生成当前时间
        current_time = self.database_utils.get_current_time_str()

        # 准备数据
        enbedding_data = {
                "intent_id": intent.intent_id,
                "version": "1.0",
                "domain": intent.domain,
                "create_time": current_time,
                "update_time": current_time,
                "status": intent.status or "active",
                "tags": intent.tags,
                "core_keywords": intent.core_keywords,
                "sample_utterances": intent.sample_utterances,
                "intent_description": intent.intent_description,
                "intent_name": intent.intent_name,
                "intent_summary": intent.intent_summary,
                "user_goal": intent.user_goal,
                "tool": intent.tools,
        }
        
        # 编码嵌入文本
        dense_text = self.database_utils.build_dense_embedding_text(enbedding_data)
        sparse_text = self.database_utils.build_sparse_embedding_text(enbedding_data)

        dense_encode_result = self.encoder.encode_text_hybrid(dense_text)
        sparse_encode_result = self.encoder.encode_text_hybrid(sparse_text)

        dense_vectors = dense_encode_result.get("dense")
        sparse_vectors = sparse_encode_result.get("sparse")
        print(f"dense_vectors: {dense_vectors} | sparse_vectors: {sparse_vectors}")

        data = [
            {
                "intent_id": intent.intent_id,
                "version": "1.0",
                "domain": intent.domain,
                "create_time": current_time,
                "update_time": current_time,
                "status": intent.status or "active",
                "tags": intent.tags,
                "core_keywords": intent.core_keywords,
                "sample_utterances": intent.sample_utterances,
                "intent_description_text": intent.intent_description,
                "intent_name_text": intent.intent_name,
                "intent_summary_text": intent.intent_summary,
                "user_goal_text": intent.user_goal,
                "dense_vector": dense_vectors,
                "sparse_vector": sparse_vectors,
                "tool": intent.tools,
            }
        ]

        # 添加数据
        res = self.milvus_client.upsert(
            collection_name=self.collection_name,
            data=data,
        )

        print(res)      #{'upsert_count': 1, 'ids': ['INT20260211009']}
        if res.get("upsert_count"):
            return {"status": "success", "message": "意图添加成功", "intent_id": intent.intent_id}
        else:
            return {"status": "error", "message": f"意图ID {intent.intent_id} 添加失败"}


    def delete_intent(self,intent_id:str) -> str:
        """删除意图"""
        print("正在删除意图...")
        res = self.milvus_client.delete(
            collection_name=self.collection_name,
            ids=[intent_id],
        )
        print(res)      #{'delete_count': 1}
        if res.get("delete_count"):
            return {"status": "success", "message": "意图删除成功", "intent_id": intent_id}
        else:
            return {"status": "error", "message": f"意图ID {intent_id} 删除失败"}

    def update_intent(self,intent:Intent) -> str:
        """更新意图"""
        print("正在更新意图...")
        # 检查意图ID是否已存在
        intent_ids = self.query_all_intents_id()
        if intent.intent_id not in intent_ids:
            print(f"意图ID {intent.intent_id} 不存在")
            return f"意图ID {intent.intent_id} 不存在"
        
        # 生成当前时间
        current_time = self.database_utils.get_current_time_str()
        
        # 准备数据
        enbedding_data = {
                "intent_id": intent.intent_id,
                "version": str(float(intent.version) + 0.1),
                "domain": intent.domain,
                "create_time": intent.create_time,
                "update_time": current_time,
                "status": intent.status,
                "tags": intent.tags,
                "core_keywords": intent.core_keywords,
                "sample_utterances": intent.sample_utterances,
                "intent_description_text": intent.intent_description,
                "intent_name_text": intent.intent_name,
                "intent_summary_text": intent.intent_summary,
                "user_goal_text": intent.user_goal,
        }

        res = self.milvus_client.upsert(
            collection_name=self.collection_name,
            data=[enbedding_data],
            partial_update=True,
        )
        if res.get("upsert_count"):
            return {"status": "success", "message": "意图更新成功", "intent_id": intent.intent_id}
        else:
            return {"status": "error", "message": f"意图ID {intent.intent_id} 更新失败"}


    def query_intent(self,intent_id:str) -> Intent | None:
        """查询意图"""
        print("正在查询意图...")
        res = self.milvus_client.get(
            collection_name=self.collection_name,
            ids=[intent_id],
            output_fields=["intent_id", "version", "domain", "create_time", "update_time", "status",
                           "intent_name_text", "intent_description_text", "intent_summary_text", "user_goal_text", "sample_utterances",
                           "tags","core_keywords"],
        )
        if res:
            # 映射字段名
            intent_data = res[0]
            mapped_data = {
                "intent_id": intent_data.get("intent_id"),
                "version": intent_data.get("version"),
                "domain": intent_data.get("domain"),
                "create_time": intent_data.get("create_time"),
                "update_time": intent_data.get("update_time"),
                "status": intent_data.get("status"),
                "tags": intent_data.get("tags"),
                "core_keywords": intent_data.get("core_keywords"),
                "sample_utterances": intent_data.get("sample_utterances"),
                "intent_name": intent_data.get("intent_name_text"),
                "intent_description": intent_data.get("intent_description_text"),
                "intent_summary": intent_data.get("intent_summary_text"),
                "user_goal": intent_data.get("user_goal_text")
            }
            print(type(mapped_data.get("tags")))
            return Intent(**mapped_data).model_dump()  # <class 'dict'>
        else:
            return None

    def query_all_intents_id(self)->list:
        """查询所有意图的id"""
        print("正在查询所有意图...")
        intent_id_list = []
        res = self.milvus_client.query(
            collection_name=self.collection_name,
            filter="status == 'active'",
            output_fields=["intent_id"],
            limit=100  # 添加 limit 参数
        )
        if res:
            for item in res:
                intent_id_list.append(item["intent_id"])
            return intent_id_list   # <class 'list'>
        else:
            return []

    def query_all_intents(self)->list:
        """查询所有意图"""
        print("正在查询所有意图...")
        intent_list = []
        iterator = self.milvus_client.query_iterator(
            collection_name=self.collection_name,
            output_fields=["intent_id", "version", "domain", "create_time", "update_time", "status",
                           "intent_name_text", "intent_description_text", "intent_summary_text", "user_goal_text", "sample_utterances",
                           "tags","core_keywords"],
            batch_size=10,
            filter=""
        )
        print("iterator:", iterator)
        while True:
            try:
                result = iterator.next()

                if not result:
                    iterator.close()
                    break
                
                for item in result:
                    # 映射字段名
                    mapped_data = {
                        "intent_id": item.get("intent_id"),
                        "version": item.get("version"),
                        "domain": item.get("domain"),
                        "create_time": item.get("create_time"),
                        "update_time": item.get("update_time"),
                        "status": item.get("status"),
                        "tags": item.get("tags"),
                        "core_keywords": item.get("core_keywords"),
                        "sample_utterances": item.get("sample_utterances"),
                        "intent_name": item.get("intent_name_text"),
                        "intent_description": item.get("intent_description_text"),
                        "intent_summary": item.get("intent_summary_text"),
                        "user_goal": item.get("user_goal_text")
                    }
                    intent_list.append(Intent(**mapped_data).model_dump())
            except StopIteration:
                break
        return intent_list




if __name__ == "__main__":
    modify_intent = ModifyIntent()
    # # 生成当前时间
    # current_time = modify_intent.database_utils.get_current_time_str()
    # intent = Intent(
    #     intent_id="INT20260211009",
    #     version="1.0",
    #     domain="轨道交通设备运维",
    #     create_time=current_time,
    #     update_time=current_time,
    #     status="active",
    #     tags=[ "轨道交通", "预防性维护", "保养周期", "维护计划", "寿命管理"],
    #     core_keywords=[
    #         "轨道交通",
    #         "预防性维护",
    #         "保养周期",
    #         "易损件更换",
    #         "寿命评估"
    #     ],
    #     sample_utterances=[
    #         "轨道交通车辆转向架的预防性保养周期是多久？",
    #         "信号设备需要定期更换哪些易损件？",
    #         "轨道扣件系统的润滑维护计划是什么？",
    #         "供电接触网设备的寿命评估标准有哪些？",
    #         "通信设备年度预防性维护包含哪些项目？"
    #     ],
    #     intent_description="用于查询轨道交通各类设备（车辆、信号、供电、轨道、通信等）的预防性维护周期、保养项目清单、润滑要求、易损件更换周期及寿命评估标准，指导运维团队科学制定保养计划，延长设备使用寿命，降低突发故障率。",
    #     intent_name="轨道交通设备预防性维护计划查询",
    #     intent_summary="查询轨道交通设备的预防性维护周期、保养项目及寿命管理要求，支撑计划性保养工作。",
    #     user_goal="获取设备预防性维护计划与寿命管理标准，科学安排保养任务，预防设备劣化，提升系统可靠性。",
    # )
    # res = modify_intent.add_intent(intent)
    # print(res)
    # print("意图添加完成")

    # # 删除意图
    # res = modify_intent.delete_intent(intent_id="INT20260211009")
    # print(res)
    # print("意图删除完成")

    # res = modify_intent.query_intent(intent_id="INT20260211008")
    # print("单个意图查询结果:")
    # print(res)
    # print(type(res.get("tags")))
    # print("意图查询完成")

    # res = modify_intent.query_all_intents_id()
    # print("所有意图查询结果:")
    # print(res)
    # print(type(res))
    # print("所有意图id查询完成")

    # res = modify_intent.query_all_intents()
    # print(res)
    # print(type(res))
    # print("所有意图查询完成")

    intent = Intent(
        intent_id="INT20260211001",
        version="1.0",
        domain="轨道交通设备运维",
        create_time="2026-02-11 10:00:00",
        update_time="2026-02-11 11:30:00",
        status="active",
        tags=["轨道交通", "设备检查", "部件排查"],
        core_keywords=[
            "轨道交通",
            "设备部件",
            "检查要点",
            "关键部位",
            "设备组成"
        ],
        sample_utterances=[
            "轨道交通车辆的关键部件有哪些，怎么检查？",
            "信号设备的组成部分及检查要点是什么？",
            "轨道关键部位的检查流程请说明一下",
            "供电设备部件检查需要关注哪些方面？",
            "如何排查轨道交通设备的核心部件隐患"
        ],
        intent_description="用于查询轨道交通各类设备（含车辆、信号、供电、轨道等）的组成结构，以及关键部件的检查范围、检查要点、检查流程，支撑一线运维人员开展部件巡检工作。",
        intent_name="轨道交通设备部件检查",
        intent_summary="查询轨道交通设备组成及关键部件的检查相关内容，支撑巡检工作。",
        user_goal="获取轨道交通各类设备的组成信息，掌握关键部件的检查方法和要点，完成部件巡检任务。",
    )

    res = modify_intent.update_intent(intent)
    print(res)
    print("意图更新完成")