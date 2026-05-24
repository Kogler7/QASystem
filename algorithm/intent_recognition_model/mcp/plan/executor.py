from __future__ import annotations

import logging
import traceback
import os
import sys
from typing import Any

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "..", ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

try:
    from models.plan_app import PlanApp
except ImportError:
    try:
        sys.path.insert(0, os.path.join(project_root, "models"))
        from plan_app import PlanApp
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"导入 PlanApp 失败: {e}")
        PlanApp = None

logger = logging.getLogger(__name__)


class PlanExecutor:
    def __init__(self):
        if PlanApp is None:
            raise ImportError("无法加载 PlanApp 模块，请检查路径配置")
        self._plan_app = PlanApp()

    def get_plan_from_intent(
        self,
        intent_id: str,
        domain: str,
    ) -> dict:
        import time
        start_time = time.monotonic()

        try:
            result = self._plan_app.get_plan_from_intent(intent_id, domain)
            execution_time = (time.monotonic() - start_time) * 1000
            if isinstance(result, dict):
                result["execution_time_ms"] = round(execution_time, 2)
            return result
        except Exception as e:
            execution_time = (time.monotonic() - start_time) * 1000
            error_msg = str(e)
            logger.error(f"从意图获取计划失败: {error_msg}")
            logger.error(traceback.format_exc())
            return {
                "understanding": f"获取计划失败: {error_msg}",
                "pass": False,
                "error_message": error_msg,
                "execution_time_ms": round(execution_time, 2),
            }
