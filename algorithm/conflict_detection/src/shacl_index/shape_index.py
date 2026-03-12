from __future__ import annotations
import logging
from typing import Dict, Set, Tuple, Optional, Any
from functools import cached_property
from rdflib import Graph, URIRef, BNode, Namespace, RDF
from rdflib.term import Node

from src.config import SHAPE_TTL
from ..pathexpr import path_to_sparql_pattern

logger = logging.getLogger(__name__)

SH = Namespace("http://www.w3.org/ns/shacl#")

def strip_ns(uri_node: Any) -> str:
    """彻底标准化：将 URIRef 或字符串统一转为短名称，如 'speed'"""
    if not uri_node: return ""
    return str(uri_node).split('/')[-1].split('#')[-1]

class _LazySingleton(type):
    _instance = None
    def __call__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__call__(*args, **kwargs)
        return cls._instance

class ShapeIndex(metaclass=_LazySingleton):
    """
    SHACL 规则索引中心。
    负责建立 '属性 -> 形状' 的映射，是 Delta 引擎的心脏。
    """
    def __init__(self, shapes_path: str = str(SHAPE_TTL)):
        self.shapes_path = shapes_path

    @cached_property
    def graph(self) -> Graph:
        g = Graph()
        try:
            p = str(self.shapes_path)
            g.parse(p, format="turtle")
            logger.info(f"🧠 [ShapeIndex] 规则图谱加载成功，三元组总数: {len(g)}")
        except Exception as e:
            logger.error(f"❌ 规则解析失败: {e}")
        return g

    # ────────────────────────────────────────────────────────────────
    # 🎯 核心逻辑 1：建立属性到 Shape 的全量索引
    # ────────────────────────────────────────────────────────────────
    @cached_property
    def prop_to_parent_shapes(self) -> Dict[str, Set[Node]]:
        """
        核心映射：'speed' -> {TrainSpeedShape, ...}
        Delta 引擎根据此字典决定哪些三元组变更需要触发验证。
        """
        g = self.graph
        mapping: Dict[str, Set[Node]] = {}
        
        for shape in g.subjects(RDF.type, SH.NodeShape):
            # A. 处理显式目标：sh:targetSubjectsOf
            for target in g.objects(shape, SH.targetSubjectsOf):
                mapping.setdefault(strip_ns(target), set()).add(shape)
            
            # B. 处理显式目标：sh:targetObjectsOf
            for target in g.objects(shape, SH.targetObjectsOf):
                mapping.setdefault(strip_ns(target), set()).add(shape)

            # C. 处理形状内部的所有路径属性 (sh:path)
            # 这涵盖了 sh:property 及其嵌套的逻辑
            for prop_uri in self._extract_all_path_predicates(g, shape):
                mapping.setdefault(strip_ns(prop_uri), set()).add(shape)

        logger.info(f"🔎 [ShapeIndex] 监控属性对齐完成: {list(mapping.keys())}")
        return mapping

    # ────────────────────────────────────────────────────────────────
    # 🎯 核心逻辑 2：递归抓取所有受监控的谓词
    # ────────────────────────────────────────────────────────────────
    def _extract_all_path_predicates(self, g: Graph, node: Node) -> Set[Node]:
        """递归遍历 SHACL 树，提取所有 sh:path 指向的谓词"""
        preds = set()
        visited = set()

        def walk(current: Node):
            if current in visited: return
            visited.add(current)
            
            # 1. 提取直接路径
            path = g.value(current, SH.path)
            if path and isinstance(path, (URIRef, BNode)):
                # 如果是复杂路径（BNode），这里需要更深的解析，目前暂取直接谓词
                preds.add(path)
            
            # 2. 递归 property 块
            for ps in g.objects(current, SH.property):
                walk(ps)
                
            # 3. 递归逻辑块 (sh:or, sh:and, etc.)
            for op in [SH.or_, SH.and_, SH.xone, SH.not_]:
                for l_node in g.objects(current, op):
                    # 处理 RDF List 结构
                    for item in g.items(l_node):
                        walk(item)
                    # 处理单节点
                    walk(l_node)

        walk(node)
        return preds

    # ────────────────────────────────────────────────────────────────
    # 🎯 核心逻辑 3：Target 地图
    # ────────────────────────────────────────────────────────────────
    @cached_property
    def target_map(self) -> Dict[Node, Dict[str, Set[str]]]:
        g = self.graph
        m = {}
        for shape in g.subjects(RDF.type, SH.NodeShape):
            targets = {
                "nodes": set(), "classes": set(), 
                "subjectsOf": set(), "objectsOf": set()
            }
            # 转换 URI 到短字符串以对齐 delta_triple
            for t in g.objects(shape, SH.targetNode): targets["nodes"].add(strip_ns(t))
            for t in g.objects(shape, SH.targetClass): targets["classes"].add(strip_ns(t))
            for t in g.objects(shape, SH.targetSubjectsOf): targets["subjectsOf"].add(strip_ns(t))
            for t in g.objects(shape, SH.targetObjectsOf): targets["objectsOf"].add(strip_ns(t))
            m[shape] = targets
        return m

    @property
    def node_properties(self) -> Set[str]:
        """Delta 引擎调用的接口：返回所有受监控的短属性名"""
        return set(self.prop_to_parent_shapes.keys())

    def shape_targets(self):
        return self.target_map