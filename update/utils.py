import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


def load_config(config_file: str = "config.json") -> Dict[str, Any]:
    """加载配置文件"""
    try:
        config_path = Path(config_file)
        if not config_path.exists():
            logger.warning(f"配置文件不存在，使用默认配置")
            return _get_default_config()
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"加载配置失败: {e}")
        return _get_default_config()


def _get_default_config() -> Dict[str, Any]:
    return {
        "neo4j": {
            "uri": "bolt://localhost:7687",
            "username": "neo4j",
            "password": "password123",
            "database": "openkg"
        },
        "system": {
            "confidence_threshold": 0.7
        }
    }


def validate_triple(triple: Dict[str, Any]) -> bool:
    """
    验证三元组是否包含必要字段，置信度是否合法且保留两位小数
    """
    required = ["subject", "predicate", "object"]
    for key in required:
        if key not in triple or not triple[key]:
            logger.warning(f"缺少必要字段: {key}")
            return False
    
    # 如果有 confidence 字段，检查其类型、范围，并强制保留两位小数
    if "confidence" in triple:
        conf = triple["confidence"]
        try:
            conf = float(conf)
            conf = round(max(0.0, min(1.0, conf)), 2)
            triple["confidence"] = conf  # 更新为两位小数
            if conf < 0 or conf > 1:
                logger.warning(f"置信度超出0-1范围: {conf}")
                return False
        except (ValueError, TypeError):
            logger.warning(f"置信度格式错误: {conf}")
            return False
    return True


def validate_triples(triples: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """批量验证，返回有效列表"""
    valid = []
    for i, t in enumerate(triples):
        if validate_triple(t):
            valid.append(t)
        else:
            logger.warning(f"跳过无效三元组 #{i}: {t}")
    return valid