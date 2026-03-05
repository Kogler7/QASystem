# utils.py

from openai import OpenAI
from neo4j import GraphDatabase
from llamafactory.chat import ChatModel

import logging
import time
import os
import json
import re

logger = logging.getLogger(__name__)
    
class BaseLLM:
    """
    统一的本体抽取后端接口：
    - 输入：system_prompt + text
    - 输出：模型原始字符串结果（不做 JSON 解析）
    """

    def run(self, system_prompt: str, text: str) -> str:
        raise NotImplementedError


class LocalLLM(BaseLLM):
    """
    使用本地微调模型进行本体抽取。

    可以在构造时传入自定义模型路径。
    """

    def __init__(
        self,
        model_name_or_path: str = "/mnt/data3/srr/Qwen2.5-7B-Instruct",
        adapter_name_or_path: str = "/mnt/data3/srr/saves/rule_v4",
        max_new_tokens: int = 2048,
        temperature: float = 0.1,
    ):
        args = dict(
            model_name_or_path=model_name_or_path,
            adapter_name_or_path=adapter_name_or_path,
            template="qwen",
            finetuning_type="lora",
            quantization_bit=4,
            flash_attn="fa2",
            max_new_tokens=max_new_tokens,
            temperature=temperature,
        )
        logger.info(f"加载本地本体微调模型: {args}")
        self._chat_model = ChatModel(args)

    def run(self, system_prompt: str, text: str) -> str:
        logger.info("LocalLLM.run: text_len=%d prompt_len=%d", len(text), len(system_prompt))
        t0 = time.perf_counter()
        messages = [{"role": "user", "content": text}]
        full_text = ""
        for new_text in self._chat_model.stream_chat(messages, system=system_prompt):
            full_text += new_text
        logger.info("LocalLLM.run done: elapsed=%.2fs output_len=%d", time.perf_counter() - t0, len(full_text))
        return full_text


class ApiLLM(BaseLLM):
    """
    使用外部 OpenAI 进行本体抽取。

    默认参数与之前的 LLM 封装保持一致：
    - base_url: 本地或远程 OpenAI 兼容服务地址
    - model:    模型名称
    """

    def __init__(
        self,
        api_key: str = "EMPTY",
        base_url: str = "http://127.0.0.1:8000/v1",
        model: str = "Qwen/Qwen3-8B",
    ):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def run(self, system_prompt: str, text: str) -> str:
        logger.info("ApiLLM.run: text_len=%d prompt_len=%d model=%s", len(text), len(system_prompt), self.model)
        t0 = time.perf_counter()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
        )
        content = response.choices[0].message.content
        logger.info("ApiLLM.run done: elapsed=%.2fs output_len=%d", time.perf_counter() - t0, len(content or ""))
        return content


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