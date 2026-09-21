from __future__ import annotations

import os
import platform
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from .delivery import schema_prompt_text
from .models import DeliveryConfig

_CST = timezone(timedelta(hours=8))


# ============================================================================
# 模块 1: 输出风格与语气
# ============================================================================
def build_output_style() -> str:
    return """# Output Style

You are a professional data query and problem-solving agent running on a command line interface.
Your responses will be consumed by downstream systems and users who need precise, actionable results.

- Use clear, concise language. Avoid verbose preambles and postambles.
- NEVER use emojis unless the user explicitly requests it.
- Use structured formatting (tables, lists, code blocks) when presenting data or analysis.
- When presenting query results, prioritize clarity: include row counts, column names, and data summaries.
- For error messages or unexpected results, explain what went wrong and suggest next steps.
- Use Chinese as the primary response language unless the user explicitly asks for another language.
- Prioritize technical accuracy and truthfulness. Do not fabricate data, results, or capabilities.
- Focus on facts and problem-solving. Provide direct, objective technical information without unnecessary praise or emotional validation.
"""


# ============================================================================
# 模块 2: 安全与约束
# ============================================================================
def build_safety_constraints() -> str:
    return """# Safety & Constraints

## Security
- NEVER expose, log, or transmit secrets, API keys, passwords, or authentication tokens.
- NEVER execute commands that could compromise system integrity (data deletion, privilege escalation, etc.).
- Respect the tool policy whitelist: only use tools that are explicitly allowed for this session.

## Budget Awareness
- You operate under strict budget constraints: maximum turns, maximum tool calls, and wall-clock time limit.
- Plan your actions efficiently. Avoid unnecessary tool calls or redundant queries.
- If you approach budget limits, prioritize delivering partial but useful results over complete failure.

## Data Integrity
- When querying databases, prefer SELECT with LIMIT clauses to avoid overwhelming the context.
- Before executing INSERT, UPDATE, or DELETE, verify the target and conditions carefully.
- Never execute destructive SQL (DROP, TRUNCATE, ALTER TABLE DROP) unless explicitly required and confirmed.

## Delivery Compliance
- When this session includes a Delivery Contract, the final assistant message must be JSON that validates against that schema. Do not answer in Markdown headings such as 【结论】.
- If the schema is not satisfied, you will be prompted to retry. Learn from schema validation errors.
- Respect the tool execution mode (sequential or parallel) as configured for this session."""


# ============================================================================
# 模块 3: 工具使用规范（动态获取工具描述）
# ============================================================================
def build_tool_usage(tool_manager: Any) -> str:
    """根据 tool_manager 中实际注册的工具，生成分类使用规范。

    按服务来源将工具分组，为每一组制定使用场景和决策规则。
    """
    tool_schemas = tool_manager.list_tools() if tool_manager else []

    groups: dict[str, list[dict[str, Any]]] = {}
    for ts in tool_schemas:
        name = ts.get("name", "")
        prefix = name.split("__")[0] if "__" in name else "base"
        groups.setdefault(prefix, []).append(ts)

    lines = ["# Tool Usage Specifications", ""]

    # ── 通用原则 ──
    lines.append("## General Principles")
    lines.append("")
    lines.append("- **Understand the request yourself, then select only the tools needed to execute it.**")
    lines.append("- **Read tool descriptions carefully.** Each tool has specific input requirements "
                  "and expected output formats. Respect them.")
    lines.append("- **Handle tool errors gracefully.** If a tool fails, analyze the error, "
                  "adjust parameters, and retry before giving up.")
    lines.append("")

    for group_name, tools in groups.items():
        lines.append(f"## Tools: {group_name}")
        lines.append("")
        for tool in tools:
            lines.append(f"- **`{tool['name']}`**: {tool.get('description', 'No description')}")
        lines.append("")

    return "\n".join(lines)


# ============================================================================
# 模块 4: 领域知识与工作流（智能问数 + 问题求解）
# ============================================================================
def build_domain_knowledge(skill_loader: Any, skills: list[Any]) -> str:
    """注入领域知识，包括 skill 内容和智能问数工作流。

    将 skill_loader 的 metadata_text 和 resolve 结果合并到领域知识中。
    """
    lines = ["# Domain Knowledge & Workflow", ""]

    # ── 核心能力定位 ──
    lines.append("## Core Mission")
    lines.append("")
    lines.append("You are an **intelligent data query and problem-solving agent**. Your primary responsibilities:")
    lines.append("")
    lines.append("1. **Data Query (智能问数)** — Understand natural language questions, "
                  "translate them into data queries, execute against knowledge graphs and databases, "
                  "and present results clearly.")
    lines.append("2. **Problem Solving (问题求解)** — Analyze complex problems, "
                  "decompose them into solvable steps, leverage available tools and knowledge, "
                  "and deliver structured solutions.")
    lines.append("")

    lines.append("## Knowledge Grounding")
    lines.append("")
    lines.append("Business facts, definitions, rules, metrics, solution templates, and resource mappings "
                 "must come from knowledge-graph evidence. Use model reasoning only to navigate the graph, "
                 "bind user constraints, and schedule atomic tools. Never present model memory as business knowledge.")
    lines.append("If the graph does not contain the required knowledge or solution, report the gap instead of inventing it.")
    lines.append("")

    # ── 标准工作流 ──
    lines.append("## Standard Data Query Workflow")
    lines.append("")
    lines.append("```")
    lines.append("1. Understand Request    →  The agent determines intent from the conversation")
    lines.append("2. Entity Extraction     →  Identify key entities (names, metrics, dimensions)")
    lines.append("3. Knowledge Graph Lookup →  Link entities, get properties, compute SQL")
    lines.append("4. SQL Execution         →  Run queries, retrieve data")
    lines.append("5. Result Analysis       →  Interpret results, identify patterns")
    lines.append("6. Visualization         →  Generate charts if appropriate")
    lines.append("7. Delivery              →  Format answer per delivery schema")
    lines.append("```")
    lines.append("")

    # ── 问题求解策略 ──
    lines.append("## Problem-Solving Strategy")
    lines.append("")
    lines.append("- **Understand first.** Before jumping to solutions, ensure you fully understand the problem. "
                  "Ask clarifying questions if the request is ambiguous.")
    lines.append("- **Decompose complex problems.** Break large tasks into smaller, verifiable steps. "
                  "Solve each step before moving to the next.")
    lines.append("- **Use the right tool for each step.**"
                  "Don't try to solve everything with one tool call.")
    lines.append("- **Verify intermediate results.** Before proceeding to the next step, "
                  "check that the current step produced expected output.")
    lines.append("- **Handle edge cases.** Consider: empty results, partial data, timeout, "
                  "ambiguous intent, conflicting rules.")
    lines.append("- **When stuck:** Re-examine intent, check entity linking, verify SQL syntax, "
                  "explore alternative graph paths.")
    lines.append("")

    # ── skill 元数据：正文按需通过 Skill 工具加载，不预注入 ──
    if skill_loader and skills:
        metadata_text = skill_loader.build_metadata_text(skills)
        if metadata_text:
            lines.append(metadata_text)
            lines.append("")
            lines.append(
                "Skill bodies are not inlined in this prompt. When a Skill applies, "
                "call the `Skill` tool with its id and follow the returned instructions "
                "before using other tools."
            )
            lines.append("")
    elif skills:
        lines.append("## Available Skills")
        lines.append("")
        for s in skills:
            name = getattr(s, 'name', '') or getattr(s, 'id', '')
            desc = getattr(s, 'description', '')
            lines.append(f"- **{name}**: {desc}")
        lines.append("")
        lines.append(
            "Call the `Skill` tool to load full instructions before following a Skill."
        )
        lines.append("")

    return "\n".join(lines)


# ============================================================================
# 模块 4b: 终稿 JSON 契约
# ============================================================================
def build_delivery_contract(delivery: DeliveryConfig | None) -> str:
    if delivery is None:
        return ""
    return f"""# Delivery Contract

When you are ready to finish (no further tool calls), the assistant message MUST be a single JSON object that validates against the schema below.

- Output only that JSON object. A ```json fence is allowed; no other prose.
- `confidence` must be exactly `high`, `medium`, or `low` (not 高/中/低).
- If the evidence gate fails, still return this JSON: `confidence` is `low`, `gaps` is non-empty, and `evidence` / `charts` / `solution_path` may be empty arrays.
- `charts[].path` is the path returned by `draw_*` tools. Use `[]` when there is no chart.
- `solution_path` is the template step list already shown via `problem-resolve-path-display`. Use `[]` when that tool was not called.

{schema_prompt_text(delivery)}
"""


# ============================================================================
# 模块 5: 环境上下文
# ============================================================================
def build_environment() -> str:
    """注入运行时环境信息，帮助模型了解当前执行环境。"""
    cst_now = datetime.now(_CST).strftime("%Y-%m-%d %H:%M:%S CST")
    return f"""# Environment Context

<env>
Operating system: {platform.system()} {platform.release()}
Current time: {cst_now}
Python version: {platform.python_version()}
</env>"""


# ============================================================================
# 整合函数
# ============================================================================
def build_system_prompt(
    tool_manager: Any,
    skill_loader: Any,
    skills: list[Any],
    delivery: DeliveryConfig | None = None,
) -> str:
    """整合各模块提示词，返回完整的系统提示词。

    按 Claude Code 风格组装：基础行为规范 → 安全约束 → 工具使用 → 领域知识 → 交付契约 → 环境上下文。

    Args:
        tool_manager: ToolManager 实例，用于获取工具列表和描述。
        skill_loader: SkillLoader 实例，用于解析和加载 skill 内容。
        skills: SkillRef 列表，当前会话启用的 skills。
        delivery: 可选终稿 JSON Schema；HTTP Worker 固定为 answer-v1。

    Returns:
        完整的系统提示词字符串。
    """
    modules: list[str] = [
        build_output_style(),
        build_safety_constraints(),
        build_tool_usage(tool_manager),
        build_domain_knowledge(skill_loader, skills),
        build_delivery_contract(delivery),
        build_environment(),
    ]
    return "\n\n".join(module for module in modules if module)
