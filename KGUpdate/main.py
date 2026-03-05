"""
知识图谱更新与删除测试入口
"""

import json
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.append('.')

from logging_config import setup_logging
import logging
from KGUpdator import create_kg_updator
from utils import load_config


def load_json_data(filename):
    """从 data 目录加载 JSON 数据"""
    data_dir = Path(__file__).parent / "data"
    filepath = data_dir / filename
    if not filepath.exists():
        logging.error(f"文件不存在: {filepath}")
        return None
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"加载 JSON 失败 {filename}: {e}")
        return None


def main():
    # 设置日志
    log_file = setup_logging("logs", "INFO")
    logging.info("知识图谱更新与删除测试启动")

    # 加载配置
    config = load_config()
    logging.info(f"配置加载成功，阈值: {config['system']['confidence_threshold']}")

    # 创建更新器
    kg_updator = create_kg_updator(
        neo4j_uri=config["neo4j"]["uri"],
        neo4j_username=config["neo4j"]["username"],
        neo4j_password=config["neo4j"]["password"],
        database=config["neo4j"]["database"],
        confidence_threshold=config["system"]["confidence_threshold"]
    )

    # 1. 添加三元组
    logging.info("\n=== 1. 添加三元组 ===")
    add_data = load_json_data("add_example.json")
    if add_data:
        # 确保每个三元组包含 confidence 字段
        valid_add = [t for t in add_data if "confidence" in t]
        if len(valid_add) != len(add_data):
            logging.warning("部分三元组缺少 confidence 字段，已过滤")
        add_result = kg_updator.add_triples(valid_add)
        logging.info(f"添加结果: {add_result['message']}")
        # 打印详细结果
        for i, detail in enumerate(add_result["details"]):
            triple = detail["triple"]
            logging.info(f"  {i+1}. ({triple['subject']})-[:{triple['predicate']}]->({triple['object']})")
            logging.info(f"     动作: {detail['action']}, 置信度: {detail['final_confidence']:.2f}, 原因: {detail['reason']}")
    else:
        logging.warning("跳过添加：数据文件不存在")

    # # 2. 删除三元组
    # logging.info("\n=== 2. 删除三元组 ===")
    # delete_data = load_json_data("delete_example.json")
    # if delete_data:
    #     delete_result = kg_updator.delete_triples(delete_data)
    #     logging.info(f"删除结果: {delete_result['message']}")
    #     for i, detail in enumerate(delete_result["details"]):
    #         triple = detail["triple"]
    #         logging.info(f"  {i+1}. ({triple['subject']})-[:{triple['predicate']}]->({triple['object']})")
    #         if detail.get("confidence") is not None:
    #             logging.info(f"     成功: {detail['success']}, 已删除（原置信度: {detail['confidence']:.2f}）, 消息: {detail['message']}")
    #         else:
    #             logging.info(f"     成功: {detail['success']}, 消息: {detail['message']}")
    # else:
    #     logging.warning("跳过删除：数据文件不存在")

    kg_updator.close()
    logging.info(f"测试完成，日志文件: {log_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())