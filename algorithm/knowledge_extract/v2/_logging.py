"""本包的日志入口。

同一个包可能以两种方式被导入：正常情况是 `algorithm.knowledge_extract_v2.*`，
而 benchmark 脚本在缺少 algorithm 父包时会把包父目录加入 sys.path、按顶层包
`knowledge_extract_v2.*` 导入。这里把差异收敛到一处，其它模块只用相对导入。
"""
from __future__ import annotations

try:  # 正常以 algorithm.knowledge_extract_v2.* 导入
    from ..logging_setup import ensure_logging, get_step_logger, preview
except ImportError:  # 顶层包名导入（benchmark 脚本）时的兜底
    from logging_setup import ensure_logging, get_step_logger, preview  # type: ignore[no-redef]

__all__ = ["ensure_logging", "get_step_logger", "preview"]
