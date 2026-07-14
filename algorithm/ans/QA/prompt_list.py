entity_ranking_prompt = """
你是知识图谱实体消歧器。只能根据用户问题和下方候选节点的真实属性评估相关性，
不得引入候选信息之外的业务规则、计算公式、实体或事实。

用户问题：{question}

候选实体：
{candidates_text}

请为每个候选返回：
- qid：候选原始 QID；
- relevance_score：0 到 1；
- is_relevant：是否可作为当前问题的图谱探索起点；
- reason：仅引用问题文本和候选属性说明理由。

严格输出 JSON：
{{
  "rankings": [
    {{
      "qid": "NEO_1",
      "relevance_score": 0.9,
      "is_relevant": true,
      "reason": "问题中的术语与节点 name/definition 相符"
    }}
  ]
}}

必须覆盖输入中的每个候选，不得创造新 QID，不得使用 Markdown 代码块。
"""
