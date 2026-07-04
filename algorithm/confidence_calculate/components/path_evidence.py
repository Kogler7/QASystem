import os
import yaml
import math
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from neo4j import GraphDatabase
from dotenv import load_dotenv

# 加载 backend/.env 补充环境变量（不覆盖已有变量）
env_path = Path(__file__).parent.parent.parent.parent / 'backend' / '.env'
if env_path.exists():
    load_dotenv(env_path, override=False)


class PathEvidence:
    """
    路径证据组件（精简版）。
    通过 Neo4j 查找头尾实体之间的路径，基于路径长度、关系频率等因素
    计算一个 0-1 的标量路径证据分数。
    """

    def __init__(self, config: Optional[Dict] = None):
        # 加载配置文件（默认同目录下 config.yaml）
        config_path = Path(__file__).parent.parent / 'config.yaml'
        with open(config_path, 'r', encoding='utf-8') as f:
            yaml_config = yaml.safe_load(f)

        path_config = yaml_config.get('path', {})
        if config:
            path_config.update(config)

        # Neo4j 连接配置（从环境变量读取）
        uri = os.getenv('NEO4J_URI', 'bolt://127.0.0.1:7687')
        user = os.getenv('NEO4J_USER', 'neo4j')
        password = os.getenv('NEO4J_PASSWORD', 'neo4j2025')
        self.database = os.getenv('NEO4J_DATABASE', 'neo4j')
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

        # 路径搜索参数
        self.max_path_length = path_config.get('max_path_length', 3)
        self.max_paths = path_config.get('max_paths_per_triple', 20)

        # 关系频率缓存（避免重复查询）
        self.relation_freq_cache = {}

    def _get_relation_freq(self, rel_type: str) -> float:
        """获取某关系类型的归一化频率（对数压缩）。"""
        if rel_type in self.relation_freq_cache:
            return self.relation_freq_cache[rel_type]

        try:
            with self.driver.session(database=self.database) as session:
                result = session.run(
                    f"MATCH ()-[r:{rel_type}]->() RETURN count(r) AS cnt"
                ).single()
                freq = result['cnt'] if result else 0
                # 对数归一化，避免高频关系分数过高
                norm = math.log(1 + freq) / 20.0 if freq > 0 else 0.1
                self.relation_freq_cache[rel_type] = norm
                return norm
        except Exception:
            return 0.1

    def _find_paths(self, start_name: str, end_name: str) -> List[Tuple[List[Tuple[str, str]], int]]:
        """
        查找头尾实体之间的所有简单路径。
        返回列表，每个元素为 (路径, 长度)。
        """
        # Neo4j 要求变量长度模式的上下界不能是参数，因此用 f-string 直接拼接整数值
        query = f"""
            MATCH path = (a:Entity {{name: $start}})-[rels*1..{self.max_path_length}]-(b:Entity {{name: $end}})
            WHERE ALL(n IN nodes(path) WHERE SIZE([m IN nodes(path) WHERE m = n]) = 1)
            RETURN [r IN rels | type(r)] AS rel_types,
                   [n IN nodes(path)[1..-1] | n.name] AS mid_nodes,
                   length(path) AS length
            ORDER BY length
            LIMIT {self.max_paths}
        """
        try:
            with self.driver.session(database=self.database) as session:
                res = session.run(query, start=start_name, end=end_name)
                paths = []
                for record in res:
                    # 将关系类型和中间节点名组成对
                    path = list(zip(record['rel_types'], record['mid_nodes']))
                    paths.append((path, record['length']))
                return paths
        except Exception as e:
            print(f"路径查询失败: {e}")
            return []

    def get_score(self, triple: Dict) -> float:
        """
        计算路径证据分数 (0-1)。
        如果不存在路径，返回 0.0。
        """
        start = triple.get('subject', '')
        end = triple.get('object', '')
        if not start or not end or start == end:
            return 0.5

        paths = self._find_paths(start, end)
        if not paths:
            return 0.0

        total_score = 0.0
        for path, length in paths:
            # 长度惩罚（指数衰减）
            len_score = math.exp(-0.5 * length)
            # 关系频率因子（平均频率，1 + avg_freq 作为乘数）
            rel_scores = [self._get_relation_freq(rel) for rel, _ in path]
            avg_rel = sum(rel_scores) / len(rel_scores) if rel_scores else 0.5
            total_score += len_score * (1.0 + avg_rel)

        # 归一化到 0-1，平均每条路径得分
        normalized = min(1.0, round(total_score / len(paths), 4))
        return normalized

    def close(self):
        if self.driver:
            self.driver.close()