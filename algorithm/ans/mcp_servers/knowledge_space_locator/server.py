"""知识空间定位 MCP 服务

提供知识子空间定位能力，供智能体调用：
- 根据用户问题定位到最合适的知识子空间
- 查询所有可用的知识子空间
"""

import json
import os
import sys
from mcp.server.fastmcp import FastMCP

MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "51015"))

mcp = FastMCP(
    "knowledge-space-locator",
    instructions="知识空间定位MCP服务：根据用户问题定位到最合适的知识子空间，支持运维诊断域和采购管理域",
    host=MCP_HOST,
    port=MCP_PORT,
)


def _get_locator():
    """延迟加载知识空间定位器"""
    _root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    _local = os.path.join(_root, ".local")
    if os.path.isdir(_local) and _local not in sys.path:
        sys.path.insert(0, _local)

    from algorithm.ans.QA.tools.knowledge_space_locator import KnowledgeSpaceLocator
    from algorithm.ans.QA.config import Config

    cfg = Config.load()
    return KnowledgeSpaceLocator(cfg)


@mcp.tool()
def locate_knowledge_space(user_question: str) -> str:
    """根据用户问题定位到最合适的知识子空间。

    支持三个知识子空间：
    1. 运维诊断域-规则：故障诊断、设备异常、规则推理（如：轨道电路红光带原因）
    2. 采购管理域-信息：采购指标查询、数据统计（如：2024年采购金额）
    3. 采购管理域-流程：采购流程优化、业务过程分析（如：采购审批流程）

    Args:
        user_question: 用户输入的原始问题文本

    Returns:
        JSON格式，包含目标子空间、领域、方面、置信度和选择原因
    """
    locator = _get_locator()
    result = locator.locate(user_question)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def get_all_subspaces() -> str:
    """获取当前所有可用的知识子空间配置。

    Returns:
        JSON格式，包含所有子空间的名称、领域、方面、描述等信息
    """
    locator = _get_locator()
    result = locator.get_all_subspaces()
    return json.dumps(result, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
    mcp.run(transport=transport)
