from __future__ import annotations

import logging
import traceback
from typing import Any, Union

from algorithm.intent_recognition_model.models.agent_intent import intent_recognition

logger = logging.getLogger(__name__)


class IntentRecognitionExecutor:
    def __init__(self):
        """初始化意图识别执行器"""

    def recognize(
        self,
        problem_model: Union[str, dict]
    ) -> dict:
        """
        执行意图识别
        
        Args:
            problem_model: 问题模型，可以是字符串或字典
        
        Returns:
            dict: intent_recognition 的原始返回结果，添加 execution_time_ms
        """
        import time
        start_time = time.monotonic()
        
        try:
            result = intent_recognition(problem_model)
            execution_time = (time.monotonic() - start_time) * 1000
            
            # 添加执行时间到返回结果中
            if isinstance(result, dict):
                result["execution_time_ms"] = round(execution_time, 2)
            
            return result
            
        except Exception as e:
            execution_time = (time.monotonic() - start_time) * 1000
            error_msg = str(e)
            logger.error(f"意图识别失败: {error_msg}")
            logger.error(traceback.format_exc())
            
            return {
                "understanding": "意图识别失败",
                "pass": False,
                "intent": {},
                "detail": {},
                "error_message": error_msg,
                "execution_time_ms": round(execution_time, 2)
            }


def execute_intent_recognition(
    problem_model: Union[str, dict]
) -> dict:
    """
    便捷函数：执行意图识别
    
    Args:
        problem_model: 问题模型，可以是字符串或字典
    
    Returns:
        dict: 原始返回结果 + execution_time_ms
    """
    executor = IntentRecognitionExecutor()
    return executor.recognize(problem_model)
