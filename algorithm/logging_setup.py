"""运行日志兜底：让关键阶段的 INFO 日志一定可见。

容器入口是 `python -m app.execution.supervisor`，它 fork 出 uvicorn API 与
cpu/gpu worker 子进程；`supervisor.main()` 里的 `logging.basicConfig` 只作用于
supervisor 自己那个进程，**不会被子进程继承**。子进程里 root logger 既没有
handler、级别也停在默认的 WARNING，于是模块级 `logger.info(...)` 会被
`logging.lastResort`（只放 WARNING 及以上）直接丢掉，日志看起来"整条流程都没输出"。

这里提供一个幂等入口：任何模块调用 `ensure_logging(logger)` 之后，该 logger
一定能把 INFO 打到 stderr，且不依赖 root 的 handler / 级别。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_HANDLER_ATTR = "_qasystem_stage_handler"


def log_level() -> int:
    """读取 QASYSTEM_LOG_LEVEL / EXECUTION_LOG_LEVEL，默认 INFO。"""
    raw = (
        os.getenv("QASYSTEM_LOG_LEVEL")
        or os.getenv("EXECUTION_LOG_LEVEL")
        or "INFO"
    ).strip().upper()
    value = getattr(logging, raw, None)
    return value if isinstance(value, int) else logging.INFO


def ensure_logging(logger: logging.Logger, level: int | None = None) -> logging.Logger:
    """确保 ``logger`` 稳定输出指定级别（默认 INFO）的日志，可重复调用。

    与 ``logging.basicConfig`` 不同，这里不检查 root 是否已有 handler：判断
    依据是"**自己**有没有挂过这个 handler"，因此别的库先调用过 basicConfig
    也不会把本模块的日志降级成哑巴。
    """
    resolved = log_level() if level is None else level
    handler = getattr(logger, _HANDLER_ATTR, None)
    if not isinstance(handler, logging.StreamHandler):
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
        setattr(logger, _HANDLER_ATTR, handler)
        # 自己已经输出过一次，禁止再冒泡，避免与 root/uvicorn 的 handler 重复打印。
        logger.propagate = False
    handler.setLevel(resolved)
    logger.setLevel(resolved)
    return logger


def get_step_logger(name: str) -> logging.Logger:
    """取一个保证 INFO 可见的步骤 logger（等价于 ensure_logging(getLogger(name))）。"""
    return ensure_logging(logging.getLogger(name))


def preview(value: Any, limit: int = 120) -> str:
    """把输入/输出压成单行预览，既不刷屏又能看出内容对不对。"""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    flattened = " ".join(str(text).split())
    if len(flattened) <= limit:
        return flattened
    return f"{flattened[:limit]}…（共 {len(flattened)} 字符）"


__all__ = ["LOG_FORMAT", "ensure_logging", "get_step_logger", "log_level", "preview"]
