import re
import logging
from neo4j import GraphDatabase
from src.config import NEO4J_URI, NEO4J_USER, NEO4J_PWD

# 强制显示 INFO 级别的日志，确保你能看到每一步执行 [cite: 2026-03-08]
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

class Neo4jRepairExecutor:
    def __init__(self):
        self.driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PWD))

    def close(self):
        self.driver.close()

    def execute_repairs(self, actions_list: list, dry_run=False):
        """
        执行修复：add 对应 MERGE (有则改之无则加之)，del 对应 REMOVE 属性 [cite: 2026-03-05]
        """
        if not actions_list:
            logger.info("ℹ️ 没有待执行的数据库操作。")
            return

        with self.driver.session() as session:
            for action in actions_list:
                # 兼容不同格式的正则解析 [cite: 2026-03-06]
                m = re.match(r'(add|del)\("([^"]+)","([^"]+)",(.+)\)', action.strip())
                if not m: continue
                
                act, eid, prop, val = m.groups()
                val = val.strip().strip('"')
                
                # 转换数值类型，确保 Neo4j 存储的是数字而非字符串 [cite: 2026-01-26]
                real_val = int(val) if val.isdigit() else val

                if act == "add":
                    # 使用 MERGE 代替 MATCH，解决 G005 等新节点无法显示的问题 [cite: 2026-03-05]
                    query = f"MERGE (n:FuxingTrain {{id: $id}}) SET n.{prop} = $val"
                    session.run(query, id=eid, val=real_val)
                    logger.info(f"✅ [Neo4j] 已物理写入: {eid}.{prop} = {real_val}")
                
                elif act == "del":
                    # 如果数据本身就在库里或是刚尝试插入的错数据，执行移除 [cite: 2026-03-06]
                    query = f"MATCH (n:FuxingTrain {{id: $id}}) REMOVE n.{prop}"
                    session.run(query, id=eid)
                    logger.info(f"🗑️ [Neo4j] 已物理删除违规数据: {eid}.{prop}")