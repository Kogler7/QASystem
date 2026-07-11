from __future__ import annotations

import logging
import traceback
from typing import Any

from algorithm.intent_recognition_model.models.plan_app import PlanApp

logger = logging.getLogger(__name__)


class PlanExecutor:
    def __init__(self):
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
