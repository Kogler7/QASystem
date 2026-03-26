# utils.py

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

# 统一大模型调用：ApiLLM 委托 algorithm/common/llm_client
_ALGO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ALGO_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALGO_ROOT))


class BaseLLM:
    """
    统一的本体抽取后端接口：
    - 输入：system_prompt + text
    - 输出：模型原始字符串结果（不做 JSON 解析）
    """

    def run(self, system_prompt: str, text: str) -> str:
        raise NotImplementedError


class ApiLLM(BaseLLM):
    """
    使用 OpenAI 兼容 API 进行本体抽取（如 vLLM、OpenAI 等）。

    默认连接部署的微调模型服务（vLLM OpenAI API 兼容）：
    - base_url: OpenAI 兼容服务地址
    - model:    模型名称
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        # uie 抽取默认依赖的微调模型
        model: str = "rule_v4",
    ):
        from common.llm_client import LLMClient

        # 兼容：uie 历史上默认走远端微调服务；若调用方不传 base_url，则保留该默认值。
        if not base_url:
            base_url = (
                os.getenv("LLM_BASE_URL")
                or os.getenv("OPENAI_BASE_URL")
                or os.getenv("VLLM_BASE_URL")
                or "http://10.126.62.88:8003/v1"
            )

        self._client = LLMClient(
            api_key=api_key or "",
            base_url=base_url or "",
            model=model or "",
        )

    def run(self, system_prompt: str, text: str) -> str:
        logger.info(
            "ApiLLM.run: text_len=%d prompt_len=%d model=%s",
            len(text),
            len(system_prompt),
            getattr(self._client, "model", ""),
        )
        t0 = time.perf_counter()
        
        # rule_v4 等 4k 上下文模型：max_tokens 过大时服务端会把可用输入压到 context-max_tokens，易触发 400。
        max_out = int(os.getenv("UIE_MAX_OUTPUT_TOKENS", "768"))
        out = self._client.run(system_prompt, text, temperature=0.1, max_tokens=max_out)

        logger.info(
            "ApiLLM.run done: elapsed=%.2fs output_len=%d",
            time.perf_counter() - t0,
            len(out or ""),
        )
        return out or ""


def simple_post_process(text: str):
    """
    公共后处理：
    - 去掉 <think>...</think>
    - 尝试解析为 JSON，失败则返回原始字符串
    """
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return cleaned


class GraphDB:
    def __init__(self, uri="bolt://localhost:7687", user="neo4j", password="neo4j2025"):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def run(self, query, parameters=None):
        with self.driver.session() as session:
            result = session.run(query, parameters or {})
            return [record.data() for record in result]

    def create_node(self, label, properties):
        props_str = ", ".join(f"{k}: ${k}" for k in properties.keys())
        query = f"CREATE (n:{label} {{ {props_str} }}) RETURN n"
        return self.run(query, properties)

    def create_relationship(self, from_label, to_label, rel_type, from_props, to_props):
        from_match = " AND ".join(f"n.{k} = ${'from_'+k}" for k in from_props.keys())
        to_match = " AND ".join(f"m.{k} = ${'to_'+k}" for k in to_props.keys())
        query = (
            f"MATCH (n:{from_label}), (m:{to_label}) "
            f"WHERE {from_match} AND {to_match} "
            f"CREATE (n)-[r:{rel_type}]->(m) RETURN r"
        )
        parameters = {f"from_{k}": v for k, v in from_props.items()}
        parameters.update({f"to_{k}": v for k, v in to_props.items()})
        return self.run(query, parameters)
    
    def query_subgraph_by_class(self, class_name: str, max_hops: int = 3):
        query = f"""
        MATCH (start:Class {{name: $class_name}})
        MATCH (start)-[r*1..{max_hops}]-(n)
        WHERE ALL(rel IN r WHERE type(rel) <> 'instance')
        WITH COLLECT(DISTINCT start) + COLLECT(DISTINCT n) AS all_nodes

        UNWIND all_nodes AS a
        UNWIND all_nodes AS b
        WITH DISTINCT a, b, all_nodes
        WHERE id(a) < id(b)

        MATCH (a)-[rel]-(b)
        WHERE type(rel) <> 'instance'

        RETURN all_nodes AS nodes,
            COLLECT(DISTINCT a.name + ' ' + type(rel) + ' ' + b.name) AS relationships
        """
        result = self.run(query, {"class_name": class_name})
        if result:
            return result[0]
        else:
            return {"nodes": [], "relationships": []}
        

if __name__ == "__main__":
    graph = GraphDB()

    subgraph = graph.query_subgraph_by_class(
        class_name="RuleType",
        max_hops=3
    )

    print("节点数量:", len(subgraph['nodes']))

    for node in subgraph['nodes']:
        print(node)

    print("关系数量:", len(subgraph['relationships']))

    for rel in subgraph['relationships']:
        print(rel)