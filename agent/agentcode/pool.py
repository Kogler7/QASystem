"""ToolManager 连接池 — 预创建 N 个实例，Agent 执行时借用 / 归还。

工业设计：
  - 启动时在独立线程中并行创建所有 ToolManager（复用 MCP 连接）
  - borrow/return 用 threading.Condition 实现（零 CPU 空转）
  - 每个 ToolManager 绑定自己的 asyncio 事件循环，同一时刻只服务一个 Agent
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .models import SessionConfig
from .tools.mcp_tools.tool_manager import ToolManager

logger = logging.getLogger("agentcode.pool")


@dataclass
class _Slot:
    tm: ToolManager
    in_use: bool = False
    created_at: float = 0.0
    borrow_count: int = 0
    error_count: int = 0


class ToolManagerPool:
    """ToolManager 对象池。

    用法:
        pool = ToolManagerPool(size=10, config=base_config)
        pool.start()

        with pool.borrow(timeout=30) as tm:
            runtime = AgentRuntime(tool_manager=tm, ...)
            runtime.run_session(...)
    """

    def __init__(self, size: int, config: SessionConfig, include_shell: bool = False):
        if size < 1:
            raise ValueError("pool size must be >= 1")
        self._size = size
        self._config = config
        self._include_shell = include_shell
        self._slots: list[_Slot] = []
        self._cond = threading.Condition()
        self._closed = False

    def start(self) -> None:
        logger.info("ToolManagerPool: 正在创建 %d 个连接...", self._size)

        def _init_one(idx: int) -> tuple[int, ToolManager | None]:
            try:
                return idx, ToolManager(config=self._config, include_shell=self._include_shell)
            except Exception as e:
                logger.error("ToolManagerPool[%d]: 创建失败: %s", idx, e)
                return idx, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=self._size) as ex:
            futures = [ex.submit(_init_one, i) for i in range(self._size)]
            for fut in concurrent.futures.as_completed(futures):
                idx, tm = fut.result()
                if tm is not None:
                    self._slots.append(_Slot(tm=tm, created_at=time.time()))

        logger.info("ToolManagerPool: %d/%d 就绪", len(self._slots), self._size)
        if len(self._slots) == 0:
            raise RuntimeError("ToolManagerPool: 所有连接创建失败")

    @contextmanager
    def borrow(self, timeout: float = 30.0):
        """从池中借出一个空闲 ToolManager（阻塞等待，线程安全）。"""
        deadline = time.time() + timeout

        with self._cond:
            if self._closed:
                raise RuntimeError("ToolManagerPool 已关闭")

            while True:
                for slot in self._slots:
                    if not slot.in_use:
                        slot.in_use = True
                        slot.borrow_count += 1
                        break
                else:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise RuntimeError(
                            f"ToolManagerPool: borrow 超时 ({timeout}s), "
                            f"total={len(self._slots)}, available=0"
                        )
                    self._cond.wait(timeout=min(remaining, 1.0))
                    continue
                break

        try:
            yield slot.tm
        except Exception:
            slot.error_count += 1
            raise
        finally:
            try:
                # 只失效"本任务已绑身份"标记：保留 _bound_headers 让下一个同身份
                # 任务复用会话；而任何未绑身份就调工具的路径都会被 MCP 层拒绝。
                slot.tm.end_task()
            except Exception:
                pass
            with self._cond:
                slot.in_use = False
                self._cond.notify_all()

    def stats(self) -> dict[str, Any]:
        with self._cond:
            return {
                "total": len(self._slots),
                "in_use": sum(1 for s in self._slots if s.in_use),
                "available": sum(1 for s in self._slots if not s.in_use),
                "total_errors": sum(s.error_count for s in self._slots),
                "total_borrows": sum(s.borrow_count for s in self._slots),
            }

    def shutdown(self) -> None:
        with self._cond:
            self._closed = True
            for slot in self._slots:
                try:
                    slot.tm.close()
                except Exception:
                    pass
            self._slots.clear()
            self._cond.notify_all()
        logger.info("ToolManagerPool: 已关闭")
