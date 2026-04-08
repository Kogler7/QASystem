import os
import yaml
import torch
import torch.nn as nn
import numpy as np
from typing import Dict, List, Tuple, Optional
from neo4j import GraphDatabase
import logging
from pathlib import Path
from dotenv import load_dotenv

# 加载 backend/.env
env_path = Path(__file__).parent.parent.parent.parent / 'backend' / '.env'
load_dotenv(env_path)

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PathEvidence(nn.Module):
    """
    基于Neo4j的路径证据组件 - 从 backend/.env 读取连接配置，从 config.yaml 读取模型参数
    """
    
    def __init__(self, config: Optional[Dict] = None, config_path: Optional[str] = None):
        super().__init__()
        
        # 加载 config.yaml
        if config_path is None:
            config_path = Path(__file__).parent.parent / 'config.yaml'
        
        with open(config_path, 'r', encoding='utf-8') as f:
            yaml_config = yaml.safe_load(f)
        
        path_config = yaml_config.get('path', {})
        
        # 合并传入的 config（如果有）
        if config:
            path_config.update(config)
        
        # 从环境变量读取 Neo4j 连接配置
        self.uri = os.getenv('NEO4J_URI', 'bolt://localhost:7687')
        self.username = os.getenv('NEO4J_USER', 'neo4j')
        self.password = os.getenv('NEO4J_PASSWORD', 'neo4j2025')
        self.database = os.getenv('NEO4J_DATABASE', 'neo4j')
        
        # 从 config.yaml 读取路径参数
        self.max_path_length = path_config.get('max_path_length', 3)
        self.max_paths = path_config.get('max_paths_per_triple', 20)
        self.path_embedding_dim = path_config.get('path_embedding_dim', 64)
        
        # 建立Neo4j连接
        self.driver = None
        self._connect_neo4j()
        
        # 路径编码器：将路径编码为向量
        self.path_encoder = nn.Sequential(
            nn.Linear(self.path_embedding_dim * self.max_path_length, 128),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(128, 64),
            nn.ReLU()
        )
        
        # 路径聚合层：将多条路径聚合成一个特征向量
        self.path_aggregator = nn.Sequential(
            nn.Linear(64, 32),
            nn.ReLU(),
            nn.Linear(32, 1)
        )
        
        # 可学习的关系权重
        self.relation_weight = nn.Parameter(torch.ones(1) * 0.8)
        
        # 关系类型缓存（减少Neo4j查询）
        self.relation_stats = {}
        
        logger.info(f"PathEvidence初始化完成，Neo4j: {self.uri}")
    
    def _connect_neo4j(self):
        """建立Neo4j连接"""
        try:
            self.driver = GraphDatabase.driver(
                self.uri,
                auth=(self.username, self.password)
            )
            # 测试连接
            with self.driver.session(database=self.database) as session:
                result = session.run("RETURN 1 as test")
                test = result.single()['test']
                logger.info(f"✅ Neo4j连接成功: {self.uri}")
        except Exception as e:
            logger.error(f"❌ Neo4j连接失败: {e}")
            self.driver = None
    
    def _ensure_connected(self):
        """确保Neo4j连接正常"""
        if self.driver is None:
            self._connect_neo4j()
        return self.driver is not None
    
    def _find_paths_cypher(self, start: str, end: str) -> List[List[Tuple[str, str]]]:
        """
        使用Cypher查询查找两个实体之间的路径
        """
        if not self._ensure_connected():
            return []
        
        try:
            with self.driver.session(database=self.database) as session:
                # 使用参数化查询避免注入
                query = """
                MATCH path = (start:Entity)-[rels*1..$max_length]-(end:Entity)
                WHERE start.name = $start AND end.name = $end
                AND ALL(n IN nodes(path) WHERE SINGLE(m IN nodes(path) WHERE m = n))
                RETURN [r IN rels | type(r)] AS relations,
                    [n IN nodes(path)[1..] | n.name] AS nodes,
                    length(path) AS path_length
                ORDER BY path_length
                LIMIT $max_paths
                """
                
                result = session.run(query, {
                    'start': start,
                    'end': end,
                    'max_length': self.max_path_length,
                    'max_paths': self.max_paths
                })
                
                paths = []
                for record in result:
                    relations = record['relations']
                    nodes = record['nodes']
                    path = [(relations[i], nodes[i]) for i in range(len(relations))]
                    paths.append(path)
                
                return paths
                
        except Exception as e:
            logger.error(f"路径查询失败: {e}")
            return []  
    
    def _get_relation_frequency(self, relation: str) -> float:
        """
        获取关系在知识图谱中的出现频率
        用于计算关系可靠性
        """
        if relation in self.relation_stats:
            return self.relation_stats[relation]
        
        if not self._ensure_connected():
            return 0.5
        
        try:
            with self.driver.session(database=self.database) as session:
                # 使用参数化查询
                query = """
                MATCH ()-[r:$rel]->()
                RETURN count(r) as freq
                """
                # 由于参数不能用于关系类型，需要特殊处理
                query = f"""
                MATCH ()-[r:{relation}]->()
                RETURN count(r) as freq
                """
                result = session.run(query)
                freq = result.single()['freq']
                
                # 查询所有关系的总数
                total_query = """
                MATCH ()-[r]->()
                RETURN count(r) as total
                """
                total_result = session.run(total_query)
                total = total_result.single()['total']
                
                # 计算归一化频率
                normalized_freq = min(1.0, freq / (total or 1) * 100)  # 乘以100放大
                self.relation_stats[relation] = normalized_freq
                return normalized_freq
                
        except Exception as e:
            logger.error(f"关系频率查询失败: {e}")
            return 0.5
    
    def _score_path(self, path: List[Tuple[str, str]]) -> float:
        """
        计算路径的置信度分数
        
        考虑因素:
        - 路径长度（短路径更好）
        - 关系可靠性（高频关系更可靠）
        - 路径多样性（不同类型的关系更好）
        """
        if not path:
            return 0.0
        
        # 1. 长度惩罚：指数衰减
        length_penalty = np.exp(-len(path) / 3)
        
        # 2. 关系可靠性：基于出现频率
        rel_scores = []
        for rel, _ in path:
            freq = self._get_relation_frequency(rel)
            rel_scores.append(freq)
        
        avg_rel_score = np.mean(rel_scores) if rel_scores else 0.5
        
        # 3. 多样性奖励：使用不同关系的比例
        unique_rels = len(set([rel for rel, _ in path]))
        diversity_bonus = unique_rels / len(path) if path else 0
        
        # 组合分数
        path_score = (
            length_penalty * 0.4 +
            avg_rel_score * 0.4 +
            diversity_bonus * 0.2
        ) * self.relation_weight.item()
        
        return min(1.0, max(0.0, path_score))  # 限制在[0,1]区间
    
    def _encode_path(self, path: List[Tuple[str, str]]) -> torch.Tensor:
        """
        将路径编码为向量
        简化实现：使用关系名的hash作为特征
        """
        path_vec = torch.zeros(self.path_embedding_dim * self.max_path_length)
        
        for i, (rel, node) in enumerate(path):
            if i < self.max_path_length:
                # 使用关系名的hash值作为特征
                rel_hash = abs(hash(rel)) % self.path_embedding_dim
                node_hash = abs(hash(node)) % self.path_embedding_dim
                
                # 混合关系特征和节点特征
                start_idx = i * self.path_embedding_dim
                path_vec[start_idx + rel_hash] = 1.0
                path_vec[start_idx + node_hash] += 0.5
        
        return path_vec
    
    def forward(self, triple: Dict) -> Dict:
        """
        前向传播
        """
        subject = triple.get('subject', '')
        obj = triple.get('object', '')
        
        if not subject or not obj:
            return self._empty_result()
        
        # 获取模型所在设备
        device = next(self.parameters()).device if list(self.parameters()) else torch.device('cpu')
        
        # 1. 在Neo4j中查找路径
        try:
            paths = self._find_paths_cypher(subject, obj)
        except Exception as e:
            logger.error(f"路径查询异常: {e}")
            return self._empty_result()
        
        if not paths:
            return self._empty_result()
        
        # 2. 编码路径并计算分数
        path_vectors = []
        path_scores = []
        
        for path in paths:
            path_vec = self._encode_path(path)
            path_vec = path_vec.to(device)  # 移到正确设备
            path_vectors.append(path_vec)
            path_scores.append(self._score_path(path))
        
        # 转换为tensor
        path_vectors = torch.stack(path_vectors)
        encoded_paths = self.path_encoder(path_vectors)
        
        # 3. 使用分数加权聚合路径
        if path_scores:
            scores_tensor = torch.tensor(path_scores, dtype=torch.float32, device=device).softmax(dim=0)
            aggregated = (encoded_paths * scores_tensor.unsqueeze(1)).sum(dim=0)
            aggregate_score = float(np.mean(path_scores))
            
            # 找到最佳路径
            best_idx = np.argmax(path_scores)
            best_path = paths[best_idx]
        else:
            aggregated = torch.zeros(64, device=device)
            aggregate_score = 0.0
            best_path = None
        
        return {
            'path_features': aggregated,
            'path_scores': path_scores,
            'best_path': best_path,
            'aggregate_score': aggregate_score,
            'num_paths': len(paths)
        }

    def _empty_result(self):
        """返回空结果（0条路径时调用）"""
        device = next(self.parameters()).device if list(self.parameters()) else torch.device('cpu')
        return {
            'path_features': torch.zeros(64, device=device),
            'path_scores': [],
            'best_path': None,
            'aggregate_score': 0.0,
            'num_paths': 0
        }
    
    def get_path_details(self, triple: Dict) -> Dict:
        """
        获取详细的路径信息（用于调试和分析）
        """
        result = self.forward(triple)
        
        if result['num_paths'] == 0:
            return {
                "subject": triple.get('subject', ''),
                "object": triple.get('object', ''),
                "message": "未找到路径",
                "num_paths": 0,
                "aggregate_score": 0.0,
                "paths": []
            }
        
        details = {
            "subject": triple['subject'],
            "object": triple['object'],
            "num_paths": result['num_paths'],
            "aggregate_score": result['aggregate_score'],
            "paths": []
        }
        
        for i, (path, score) in enumerate(zip(result['best_path'] if result['best_path'] else [], 
                                            result['path_scores'])):
            path_str = " -> ".join([f"[{rel}]{node}" for rel, node in path])
            details['paths'].append({
                "index": i,
                "path": path_str,
                "score": score
            })
        
        return details
    
    def close(self):
        """关闭Neo4j连接"""
        if self.driver:
            self.driver.close()
            logger.info("Neo4j连接已关闭")
    
    def __del__(self):
        """析构函数，确保连接关闭"""
        self.close()