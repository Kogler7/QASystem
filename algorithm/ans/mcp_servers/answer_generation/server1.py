import json
import os
from mcp.server.fastmcp import FastMCP

MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "51006"))

mcp = FastMCP(
    "graph-search",
    instructions="知识图谱问答工具集：包含实体识别、实体链接、属性查询、图搜索等工具",
    host=MCP_HOST,
    port=MCP_PORT,
)


# @mcp.tool()
# def recognize_entities(question: str) -> str:
#     """从问题中提取关键词/实体，供后续在图谱中搜索使用。

#     Args:
#         question: 用户问题文本

#     Returns:
#         JSON格式，包含提取的关键词/实体列表
#     """
#     import sys
#     _root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
#     if _root not in sys.path:
#         sys.path.insert(0, _root)
    
#     from algorithm.ans.QA.tools.answer_generation import AnswerGenerationTool
#     from algorithm.ans.mcp_servers.shared.dependencies import get_neo4j_client, get_config
    
#     client = get_neo4j_client()
#     cfg = get_config()
#     tool = AnswerGenerationTool(client, cfg)
    
#     keywords = tool.entity_linker.recognize_entities(question)
#     return json.dumps({"keywords": keywords}, ensure_ascii=False)


@mcp.tool()
def link_entities(keywords_json: str, question: str = "") -> str:
    """在知识图谱中搜索匹配的实体。

    Args:
        keywords_json: 关键词列表的JSON字符串，格式为 ["关键词1", "关键词2", ...]
        question: 原始用户问题（可选）

    Returns:
        JSON格式，包含：
        - candidates: 候选实体列表
    """
    import sys
    _root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    
    from algorithm.ans.QA.tools.answer_generation import AnswerGenerationTool
    from algorithm.ans.mcp_servers.shared.dependencies import get_neo4j_client, get_config
    
    client = get_neo4j_client()
    cfg = get_config()
    tool = AnswerGenerationTool(client, cfg)
    
    # 解析关键词
    try:
        keywords = json.loads(keywords_json)
    except json.JSONDecodeError:
        keywords = [keywords_json]
    
    # 直接使用关键词搜索
    link_result = tool.entity_linker.link_entities_with_llm_judge(
        question, keywords=keywords
    )
    all_candidates = link_result.get("all_candidates", [])
    
    candidates = []
    for entity in all_candidates:
        entity_name = entity.get('properties', {}).get('name', entity.get('qid', ''))
        candidates.append({
            "name": entity_name,
            "id": entity.get("qid", ""),
            "score": entity.get("relevance_score", 0),
            "is_relevant": entity.get("is_relevant", False),
            "properties": entity.get("properties", {}),
            "llm_judgment": entity.get("llm_judgment", "")
        })
    
    return json.dumps({
        "candidates": candidates
    }, ensure_ascii=False)


@mcp.tool()
def query_entity_properties(entity_id: str) -> str:
    """查询实体的完整属性信息，返回实体的所有属性供 Claude Code 判断。

    Args:
        entity_id: 实体的ID（如NEO_390）

    Returns:
        JSON对象，包含实体的完整属性信息
    """
    import sys
    _root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    
    from algorithm.ans.QA.tools.answer_generation import AnswerGenerationTool
    from algorithm.ans.mcp_servers.shared.dependencies import get_neo4j_client, get_config
    
    client = get_neo4j_client()
    cfg = get_config()
    tool = AnswerGenerationTool(client, cfg)
    
    try:
        node_info = tool.entity_linker.get_node_full_info(entity_id)
        if node_info:
            return json.dumps({
                "entity_id": entity_id,
                "name": node_info.get("name", ""),
                "labels": node_info.get("labels", []),
                "properties": node_info.get("properties", {})
            }, ensure_ascii=False)
        else:
            return json.dumps({"error": f"未找到实体: {entity_id}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool()
def search_graph(start_entity_id: str, question: str = "", depth: int = 1) -> str:
    """从起始实体进行单轮图搜索，查找相关实体和关系。

    这是一个单轮搜索工具，每次调用只搜索指定深度的一层。
    返回原始三元组数据不做判断，是否需要继续搜索更深层由 Claude Code 决定。

    Args:
        start_entity_id: 起始实体的ID（如NEO_390）
        question: 用户问题（可选，用于关系剪枝）
        depth: 当前搜索深度，默认1

    Returns:
        JSON格式，包含：
        - start_entity_id: 起始实体ID
        - depth: 当前搜索深度
        - triples: 三元组列表（head_entity, relation, tail_entity）
        - has_more: 是否还有更多结果可探索
        - found_entities: 找到的新实体列表（用于继续搜索）
    """
    import sys
    _root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    
    from algorithm.ans.QA.tools.answer_generation import AnswerGenerationTool
    from algorithm.ans.mcp_servers.shared.dependencies import get_neo4j_client, get_config
    
    client = get_neo4j_client()
    cfg = get_config()
    tool = AnswerGenerationTool(client, cfg)
    
    # 先获取实体名称
    try:
        node_info = tool.entity_linker.get_node_full_info(start_entity_id)
        entity_name = node_info.get("name", start_entity_id) if node_info else start_entity_id
    except Exception:
        entity_name = start_entity_id
    
    # 调用单深度搜索方法
    result = tool.search_single_depth(
        question=question,
        topic_entity={start_entity_id: entity_name},
        depth=depth
    )
    
    result["start_entity_id"] = start_entity_id
    return json.dumps(result, ensure_ascii=False)


# @mcp.tool()
# def get_rules(question: str = "") -> str:
    """获取适用的业务规则。

    Args:
        question: 用户问题（可选），如果提供则只返回适用的规则

    Returns:
        JSON数组，包含业务规则信息
    """
    import sys
    _root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    
    from algorithm.ans.QA.rules_config import format_rules_for_prompt, get_applicable_rules
    
    if question:
        rules = get_applicable_rules(question)
    else:
        from algorithm.ans.QA.rules_config import REASONING_RULES
        rules = REASONING_RULES["rules"]
    
    return json.dumps(rules, ensure_ascii=False)


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
    mcp.run(transport=transport)
