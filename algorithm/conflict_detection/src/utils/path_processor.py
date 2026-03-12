from __future__ import annotations

from rdflib import Graph, URIRef, BNode
from rdflib.term import Node
from typing import Set, Tuple, List, Optional, Dict, Union
import os
import logging

# 👉 引入统一配置，确保不再去桌面找 data/shapes.ttl
from src.config import SHAPE_TTL, neo4j_db
from src.utils.custom_types import PathExpr, Triple
from src.utils.path_parser import parse_shacl_path

logger = logging.getLogger(__name__)

class SH:
    path = URIRef("http://www.w3.org/ns/shacl#path")
    equals = URIRef("http://www.w3.org/ns/shacl#equals")
    disjoint = URIRef("http://www.w3.org/ns/shacl#disjoint")
    or_ = URIRef("http://www.w3.org/ns/shacl#or")
    and_ = URIRef("http://www.w3.org/ns/shacl#and")
    xone = URIRef("http://www.w3.org/ns/shacl#xone")
    not_ = URIRef("http://www.w3.org/ns/shacl#not")
    property = URIRef("http://www.w3.org/ns/shacl#property")

class PathProcessor:
    def __init__(self):
        self._visited_shapes: Set[Node] = set()
        self._path_exprs: Dict[Node, Set[PathExpr]] = {}

    def _load_shapes_graph(self) -> Graph:
        """👉 核心改造：不再使用硬编码路径，统一读取 config.SHAPE_TTL"""
        g = Graph()
        # 🎯 直接使用 Path 对象，避免桌面路径漂移问题
        shape_path = SHAPE_TTL 
        
        if shape_path.exists():
            # 转换为字符串供 rdflib 解析
            g.parse(str(shape_path), format="turtle")
            logger.info(f"🛤️ [PathProcessor] 成功加载规则文件: {shape_path}")
        else:
            logger.warning(f"⚠️ [PathProcessor] 未找到规则文件: {shape_path}")
        return g

    def extract_path_exprs(self, shape_node: Node) -> Set[PathExpr]:
        if shape_node in self._path_exprs:
            return self._path_exprs[shape_node]

        shapes_graph = self._load_shapes_graph()
        self._visited_shapes.clear()
        paths: Set[PathExpr] = set()
        self._recurse(shape_node, paths, shapes_graph)
        self._path_exprs[shape_node] = paths
        return paths

    def _recurse(self, node: Node, paths: Set[PathExpr], graph: Graph):
        if node in self._visited_shapes:
            return
        self._visited_shapes.add(node)

        for p, o in graph.predicate_objects(node):
            if p == SH.path:
                parsed = parse_shacl_path(graph, o)
                if parsed:
                    paths.add(parsed)
            elif p in {SH.equals, SH.disjoint} and isinstance(o, URIRef):
                paths.add(PathExpr(predicate=str(o)))
            elif p in {SH.or_, SH.and_, SH.xone}:
                for item in graph.items(o):
                    self._recurse(item, paths, graph)
            elif p == SH.not_:
                self._recurse(o, paths, graph)
            elif p == SH.property:
                self._recurse(o, paths, graph)

    def _traverse_path_expr(self, start_nodes: Set[str], path: PathExpr, depth: int = 3) -> List[Triple]:
        triples: List[Triple] = []
        
        # 从 RDF URI 提取短名称作为 Neo4j 关系类型
        rel_type = ""
        if hasattr(path, 'predicate') and path.predicate:
            rel_type = str(path.predicate).split("#")[-1].split("/")[-1]
            
        if not rel_type:
            return triples

        for src in start_nodes:
            # 🚀 Neo4j 变长路径查询，自动适配 Cypher 语法
            cypher_query = f"""
            MATCH (s {{id: $src}})-[r:{rel_type}*1..{depth}]->(o)
            RETURN s.id AS subject, o.id AS object
            """
            
            try:
                # 使用 config 中注入的全局 neo4j_db 单例执行查询
                records = neo4j_db.query(cypher_query, parameters={"src": src})
                
                for record in records:
                    s_val = record.get("subject")
                    o_val = record.get("object")
                    if s_val and o_val:
                        # 兼容旧系统的三元组返回格式
                        triples.append((s_val, "?", (o_val, "uri", None)))
                        
            except Exception as e:
                logger.error(f"🚨 Neo4j Cypher 执行错误: {e}")

        return triples

    def traverse_all(self, start_nodes: Set[str], paths: Set[PathExpr], depth: int = 3) -> List[Triple]:
        all_triples = []
        for path_expr in paths:
            all_triples.extend(self._traverse_path_expr(start_nodes, path_expr, depth))
        return all_triples