"""v1 通用抽取指令与 v3 base prompt 的一致性守卫。

v1 不再运行期导入 v3（见 algorithm/knowledge_extract/v1/generic_prompt.py），
代价是两边文本可能分叉。本测试是那条边的显式替代：一旦 v3 的 base prompt 有意升级，
这里会失败，提示维护者确认 v1 的通用抽取是否要跟随。
"""
from __future__ import annotations


def test_v1_generic_instruction_matches_v3_base_prompt():
    from algorithm.knowledge_extract.v1.generic_prompt import GENERIC_INSTRUCTION
    from algorithm.knowledge_extract.v3.prompt_router import BASE_SYSTEM_PROMPT

    if GENERIC_INSTRUCTION != BASE_SYSTEM_PROMPT:
        v1_lines = GENERIC_INSTRUCTION.splitlines()
        v3_lines = BASE_SYSTEM_PROMPT.splitlines()
        raise AssertionError(
            "v1 的通用抽取指令与 v3 当前 base prompt 已分叉（v1 %d 行 / v3 %d 行，首行 %r vs %r）。"
            "若 v3 的 prompt 是刻意升级，请同步更新 algorithm/knowledge_extract/v1/generic_prompt.py"
            "（确认 v1 的通用抽取是否应跟随），然后本条测试才会通过。"
            % (len(v1_lines), len(v3_lines), v1_lines[0] if v1_lines else "", v3_lines[0] if v3_lines else "")
        )
