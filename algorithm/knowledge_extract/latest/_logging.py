"""本包的日志入口。

同一个包可能以两种方式被导入：正常情况是 `algorithm.knowledge_extract.latest.*`，
而测试加载器在缺少 algorithm 父包时会把包父目录加入 sys.path、按顶层包
`knowledge_extract_latest.*` 导入。这里把差异收敛到一处，其它模块只用相对导入。
"""
from __future__ import annotations

try:  # 正常以 algorithm.knowledge_extract.latest.* 导入（logging_setup 位于 algorithm/）
    from ...logging_setup import ensure_logging, get_step_logger, preview
except ImportError:  # 测试加载器按顶层包导入时的兜底（需 algorithm/ 在 sys.path，root conftest 保证）
    from logging_setup import ensure_logging, get_step_logger, preview  # type: ignore[no-redef]

__all__ = ["ensure_logging", "get_step_logger", "preview"]
