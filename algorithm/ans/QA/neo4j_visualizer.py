import matplotlib.pyplot as plt
import networkx as nx
from pyvis.network import Network
from neo4j import GraphDatabase
import json
from typing import List, Dict, Any, Optional
import webbrowser
import os


class Neo4jPathVisualizer:
    """Neo4j 路径可视化工具"""

    def __init__(self, uri="bolt://10.126.62.88:7687", user="neo4j", password="neo4j2025"):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        """关闭连接"""
        self.driver.close()

    def get_answer_path(self, question: str, answer_entities: List[str]) -> Dict:
        """获取答案相关的完整路径"""
        with self.driver.session() as session:
            # 构建查询 - 查找所有相关实体和关系
            query = """
            MATCH path = (start)-[r*1..3]-(end)
            WHERE start.name IN $entities OR end.name IN $entities
            RETURN 
                [node IN nodes(path) | {
                    id: id(node),
                    name: node.name,
                    labels: labels(node)
                }] as nodes,
                [rel IN relationships(path) | {
                    from: id(startNode(rel)),
                    to: id(endNode(rel)),
                    type: type(rel)
                }] as relationships,
                length(path) as depth
            ORDER BY depth
            """

            result = session.run(query, entities=answer_entities)

            paths = []
            for record in result:
                paths.append({
                    "nodes": record["nodes"],
                    "relationships": record["relationships"],
                    "depth": record["depth"]
                })

            return self._merge_paths(paths)

    def _merge_paths(self, paths: List[Dict]) -> Dict:
        """合并多条路径为一个图"""
        merged_nodes = {}
        merged_rels = set()

        for path in paths:
            # 合并节点
            for node in path["nodes"]:
                node_id = node["id"]
                if node_id not in merged_nodes:
                    merged_nodes[node_id] = node

            # 合并关系
            for rel in path["relationships"]:
                rel_key = f"{rel['from']}-{rel['type']}-{rel['to']}"
                merged_rels.add(rel_key)

        return {
            "nodes": list(merged_nodes.values()),
            "relationships": [
                {
                    "from": int(k.split("-")[0]),
                    "type": k.split("-")[1],
                    "to": int(k.split("-")[2])
                }
                for k in merged_rels
            ]
        }

    def visualize_with_networkx(self, graph_data: Dict, highlight_nodes: List[str] = None):
        """使用 NetworkX + Matplotlib 可视化"""
        G = nx.DiGraph()

        # 添加节点
        node_labels = {}
        for node in graph_data["nodes"]:
            G.add_node(node["id"])
            node_labels[node["id"]] = node.get("name", f"Node_{node['id']}")

        # 添加边
        edge_labels = {}
        for rel in graph_data["relationships"]:
            G.add_edge(rel["from"], rel["to"])
            edge_labels[(rel["from"], rel["to"])] = rel["type"]

        # 绘制
        plt.figure(figsize=(15, 10))
        pos = nx.spring_layout(G, k=2, iterations=50)

        # 绘制普通节点
        nx.draw_networkx_nodes(G, pos,
                               node_color='lightblue',
                               node_size=2000,
                               alpha=0.8)

        # 高亮关键节点
        if highlight_nodes:
            highlight_ids = []
            for node in graph_data["nodes"]:
                if node.get("name") in highlight_nodes:
                    highlight_ids.append(node["id"])

            nx.draw_networkx_nodes(G, pos,
                                   nodelist=highlight_ids,
                                   node_color='red',
                                   node_size=2500,
                                   alpha=0.9)

        # 绘制边
        nx.draw_networkx_edges(G, pos,
                               edge_color='gray',
                               arrows=True,
                               arrowsize=20,
                               width=2)

        # 绘制节点标签
        nx.draw_networkx_labels(G, pos, node_labels, font_size=10)

        # 绘制边标签
        nx.draw_networkx_edge_labels(G, pos, edge_labels, font_size=8)

        plt.title("Neo4j 关键路径可视化")
        plt.axis('off')
        plt.tight_layout()
        plt.show()

    def visualize_with_pyvis(self, graph_data: Dict, output_file="neo4j_path.html"):
        """使用 Pyvis 创建交互式可视化"""
        net = Network(height="750px", width="100%",
                      bgcolor="#ffffff", font_color="black",
                      directed=True)

        # 添加节点
        for node in graph_data["nodes"]:
            node_id = str(node["id"])
            node_name = node.get("name", f"Node_{node_id}")
            node_labels = node.get("labels", ["Entity"])

            # 根据标签设置颜色
            if "Person" in node_labels:
                color = "#FF9999"
            elif "Place" in node_labels:
                color = "#99FF99"
            elif "Product" in node_labels:
                color = "#9999FF"
            else:
                color = "#FFD700"

            net.add_node(node_id,
                         label=node_name,
                         title=f"ID: {node_id}\nLabels: {', '.join(node_labels)}",
                         color=color,
                         size=30)

        # 添加边
        for rel in graph_data["relationships"]:
            from_id = str(rel["from"])
            to_id = str(rel["to"])
            rel_type = rel["type"]

            net.add_edge(from_id, to_id,
                         title=rel_type,
                         label=rel_type,
                         arrows='to',
                         color='#666666',
                         width=2)

        # 配置物理布局
        net.set_options("""
        {
          "physics": {
            "forceAtlas2Based": {
              "gravitationalConstant": -50,
              "centralGravity": 0.01,
              "springLength": 200,
              "springConstant": 0.08
            },
            "maxVelocity": 50,
            "solver": "forceAtlas2Based",
            "timestep": 0.35,
            "stabilization": {"iterations": 150}
          }
        }
        """)

        # 保存并打开
        net.show(output_file)
        webbrowser.open('file://' + os.path.realpath(output_file))
        return output_file


class EnhancedPathVisualizer:
    """增强版路径可视化器，集成到问答服务"""

    def __init__(self, neo4j_visualizer):
        self.visualizer = neo4j_visualizer

    def highlight_answer_path(self, question: str, answer_entities: List[str]):
        """高亮答案路径"""
        # 获取图谱数据
        graph_data = self.visualizer.get_answer_path(question, answer_entities)

        if not graph_data["nodes"]:
            print("未找到相关路径")
            return None

        print(f"找到 {len(graph_data['nodes'])} 个节点，{len(graph_data['relationships'])} 条关系")

        # 使用 Pyvis 创建交互式可视化
        html_file = self.visualizer.visualize_with_pyvis(graph_data,
                                                         f"answer_path_{question[:10]}.html")

        # 同时使用 NetworkX 创建静态图（可选）
        self.visualizer.visualize_with_networkx(graph_data, answer_entities)

        return html_file

    def extract_answer_entities_from_result(self, solver_result: Dict) -> List[str]:
        """从求解结果中提取答案实体"""
        entities = []

        try:
            # 从方案模型中提取
            if "方案模型" in solver_result:
                for solution in solver_result["方案模型"]:
                    if "输出" in solution:
                        for output in solution["输出"]:
                            if output and output != "信息不足":
                                entities.append(output)

            # 从图谱路径中提取
            if "图谱路径" in solver_result and "答案路径" in solver_result["图谱路径"]:
                for path in solver_result["图谱路径"]["答案路径"]:
                    for node in path.get("nodes", []):
                        if node.get("label") and node["label"] not in entities:
                            entities.append(node["label"])
        except Exception as e:
            print(f"提取答案实体失败: {e}")

        return entities