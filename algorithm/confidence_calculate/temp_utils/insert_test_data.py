#!/usr/bin/env python
# filepath: /home/admin_user_8/sxm/qasystem/algorithm/confidence_calculate/temp_utils/insert_test_data.py

import sys
import json
from pathlib import Path

# 添加 KGUpdator 所在目录到路径
KG_UPDATE_PATH = Path(__file__).parent.parent.parent / "KGUpdate"
sys.path.insert(0, str(KG_UPDATE_PATH))

# 同时也添加当前项目的上级目录（如果需要其他模块）
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from KGUpdator import create_kg_updator
except ImportError:
    print("❌ 无法导入 KGUpdator，请检查路径")
    print(f"尝试导入路径: {KG_UPDATE_PATH}")
    sys.exit(1)

# test.json 的路径
TEST_JSON_PATH = "algorithm/confidence_calculate/data/test.json"

def main():
    # 加载测试数据
    if not Path(TEST_JSON_PATH).exists():
        print("❌ 失败: test.json 文件不存在")
        return
    
    with open(TEST_JSON_PATH, 'r', encoding='utf-8') as f:
        test_data = json.load(f)
    
    # 准备要插入的三元组
    triples_to_add = []
    for item in test_data:
        triples_to_add.append({
            "subject": item["triple"]["subject"],
            "predicate": item["triple"]["predicate"],
            "object": item["triple"]["object"],
            "confidence": item["label"]
        })
    
    print(f"准备插入 {len(triples_to_add)} 条数据...")
    
    # 创建更新器并插入
    updator = create_kg_updator(
        neo4j_uri="bolt://127.0.0.1:7687",
        neo4j_username="neo4j",
        neo4j_password="neo4j2025",
        database="neo4j",
        confidence_threshold=0.7
    )
    
    try:
        result = updator.add_triples(triples_to_add)
        if result['success'] and result['statistics']['failed'] == 0:
            print("✅ 成功插入所有数据")
        else:
            print(f"❌ 插入失败: {result['message']}")
    except Exception as e:
        print(f"❌ 插入异常: {e}")
    finally:
        updator.close()

if __name__ == "__main__":
    main()