from ir_config.config import Config
from neo4j import GraphDatabase
import logging


class Neo4jConnection:
    def __init__(self):
        self.config = Config()
        self.neo4j_uri = self.config.neo4j_uri
        self.neo4j_username = self.config.neo4j_username
        self.neo4j_password = self.config.neo4j_password
        self.driver = None

    def connect(self):
        """连接到 Neo4j 数据库"""
        try:
            self.driver = GraphDatabase.driver(
                self.neo4j_uri,
                auth=(self.neo4j_username, self.neo4j_password)
            )
            # 测试连接
            with self.driver.session() as session:
                session.run("RETURN 1")
            print("成功连接到 Neo4j 数据库")
            return True
        except Exception as e:
            print(f"连接 Neo4j 数据库失败: {str(e)}")
            return False

    def close(self):
        """关闭 Neo4j 连接"""
        if self.driver:
            self.driver.close()
            logging.info("Neo4j 连接已关闭")

    def run_query(self, query, parameters=None):
        """执行 Cypher 查询
        
        Args:
            query: Cypher 查询语句
            parameters: 查询参数
            
        Returns:
            查询结果
        """
        if not self.driver:
            if not self.connect():
                return None
        
        try:
            with self.driver.session() as session:
                result = session.run(query, parameters)
                return [record.data() for record in result]
        except Exception as e:
            logging.error(f"执行查询失败: {str(e)}")
            return None

    def create_node(self, label, properties):
        """创建节点
        
        Args:
            label: 节点标签（如 Intent、Domain 等）
            properties: 节点属性（字典类型，包含业务 ID 和其他属性）
            
        Returns:
            创建结果
        """
        # 确保 properties 是字典类型
        if not isinstance(properties, dict):
            logging.error(f"properties 必须是字典类型，当前类型: {type(properties)}")
            return None
        
        if label == "Domain" or label == "Intent" or label == "Plan":
            # 对于 Intent 标签，确保包含 ID
            if label == "Intent" and "id" not in properties and "intent_id" in properties:
                # 对于 Intent 标签，使用 intent_id 作为 ID
                properties["id"] = properties["intent_id"]
                del properties["intent_id"]
            
            query = f"CREATE (n:{label}) SET n = $properties RETURN n"
            return self.run_query(query, {"properties": properties})

        else:
            logging.error(f"不支持的节点标签: {label}")
            return None

    def create_relationship(self, start_label, start_id, end_label, end_id, rel_type, properties=None):
        """创建关系（只查询已存在的节点，不创建新节点）
        
        Args:
            start_label: 起始节点标签（如 Intent、Domain、Plan）
            start_id: 起始节点标识
            end_label: 结束节点标签（如 Domain、Plan）
            end_id: 结束节点标识
            rel_type: 关系类型（如 意图_域，域_计划等）
            properties: 关系属性
            
        Returns:
            创建结果，如果节点不存在则返回 None
        """
        # 构建查询语句，只匹配已存在的节点
        # 处理起始节点匹配条件
        if start_label == "Intent":
            start_match = "id: $start_id"
        elif start_label == "Domain":
            start_match = "domain_name: $start_id"
        elif start_label == "Plan":
            start_match = "plan: $start_id"
        else:
            logging.error(f"不支持的起始节点标签: {start_label}")
            return None
        
        # 处理结束节点匹配条件
        if end_label == "Intent":
            end_match = "id: $end_id"
        elif end_label == "Domain":
            end_match = "domain_name: $end_id"
        elif end_label == "Plan":
            end_match = "plan: $end_id"
        else:
            logging.error(f"不支持的结束节点标签: {end_label}")
            return None
        
        # 构建查询语句
        query = f"""
        MATCH (a:{start_label} {{{start_match}}})
        MATCH (b:{end_label} {{{end_match}}})
        CREATE (a)-[r:{rel_type}]->(b)
        {"SET r = $properties" if properties else ""}
        RETURN r
        """
        
        # 构建参数
        params = {
            "start_id": start_id,
            "end_id": end_id
        }
        if properties:
            params["properties"] = properties
        
        # 执行查询
        result = self.run_query(query, params)
        
        # 如果结果为空，说明节点不存在
        if not result:
            logging.warning(f"节点不存在: {start_label}({start_id}) 或 {end_label}({end_id})")
            return None
        
        return result

    def create_intent_node(self, intent_id, intent_name):
        """创建 Intent 标签的节点
        
        Args:
            intent_id: 意图 ID
            intent_name: 意图名称
            
        Returns:
            创建结果
        """
        properties = {
            "intent_id": intent_id,
            "intent_name": intent_name,
        }
        return self.create_node("Intent", properties)

    def create_domain_node(self, domain_name):
        """创建 Domain 标签的节点
        
        Args:
            domain_name: 域名称
            **kwargs: 其他属性
            
        Returns:
            创建结果
        """
        properties = {
            "domain_name": domain_name,
        }
        return self.create_node("Domain", properties)

    def create_plan_node(self, plan):
        """创建 Plan 标签的节点
        
        Args:
            plan: 包含计划名称以及计划内容的字典，格式为 {"plan": "计划名称", "plan_content": "计划内容"}
        
        Returns:
            创建结果
        """
        properties = {
            "plan": plan["plan"],
            "plan_content": plan["plan_content"],
        }
        return self.create_node("Plan", properties)

    def create_intent_domain_relationship(self, intent_id, domain_name, rel_type="意图_域", **kwargs):
        """创建意图和域的关系（只查询已存在的节点，不创建新节点）
        
        Args:
            intent_id: 意图 ID
            domain_name: 域名称
            rel_type: 关系类型（默认 BELONGS_TO）
            **kwargs: 关系属性
            
        Returns:
            创建结果，如果节点不存在则返回 None
        """

        properties = kwargs if kwargs else None
        return self.create_relationship(
            "Intent", intent_id, "Domain", domain_name, rel_type, properties
        )

    def create_domain_plan_relationship(self, domain_name, plan, rel_type="域_计划", **kwargs):
        """创建域和计划的关系（只查询已存在的节点，不创建新节点）
        
        Args:
            domain_name: 域名称
            plan: 包含计划名称以及计划内容的字典，格式为 {"plan": "计划名称", "plan_content": "计划内容"}
            rel_type: 关系类型
            **kwargs: 关系属性
            
        Returns:
            创建结果，如果节点不存在则返回 None
        """

        properties = kwargs if kwargs else None
        return self.create_relationship(
            "Domain", domain_name, "Plan", plan["plan"], rel_type, properties
        )

    def get_nodes(self, label, limit=100):
        """获取指定标签的节点
        
        Args:
            label: 节点标签
            limit: 返回数量限制
            
        Returns:
            节点列表
        """
        query = f"MATCH (n:{label}) RETURN n LIMIT $limit"
        return self.run_query(query, {"limit": limit})

    def delete_intent_node(self, intent_id):
        """删除指定的 Intent 节点
        
        Args:
            intent_id: 意图 ID
            
        Returns:
            删除结果
        """
        # 先删除与该节点相关的所有关系
        query1 = f"MATCH (n:Intent {{id: $intent_id}})-[r]-() DELETE r"
        self.run_query(query1, {"intent_id": intent_id})
        
        # 再删除节点
        query2 = f"MATCH (n:Intent {{id: $intent_id}}) DELETE n"
        return self.run_query(query2, {"intent_id": intent_id})

    def delete_domain_node(self, domain_name):
        """删除指定的 Domain 节点
        
        Args:
            domain_name: 域名称
            
        Returns:
            删除结果
        """
        # 先删除与该节点相关的所有关系
        query1 = f"MATCH (n:Domain {{domain_name: $domain_name}})-[r]-() DELETE r"
        self.run_query(query1, {"domain_name": domain_name})
        
        # 再删除节点
        query2 = f"MATCH (n:Domain {{domain_name: $domain_name}}) DELETE n"
        return self.run_query(query2, {"domain_name": domain_name})

    def delete_plan_node(self, plan):
        """删除指定的 Plan 节点
        
        Args:
            plan: 计划名称
            
        Returns:
            删除结果
        """
        # 先删除与该节点相关的所有关系
        query1 = f"MATCH (n:Plan {{plan: $plan}})-[r]-() DELETE r"
        self.run_query(query1, {"plan": plan["plan"]})
        
        # 再删除节点
        query2 = f"MATCH (n:Plan {{plan: $plan}}) DELETE n"
        return self.run_query(query2, {"plan": plan["plan"]})

    def delete_intent_domain_relationship(self, intent_id, domain_name):
        """删除意图和域之间的关系
        
        Args:
            intent_id: 意图 ID
            domain_name: 域名称
            
        Returns:
            删除结果
        """
        query = f"""
        MATCH (a:Intent {{id: $intent_id}})-[r]->(b:Domain {{domain_name: $domain_name}})
        DELETE r
        RETURN r
        """
        return self.run_query(query, {"intent_id": intent_id, "domain_name": domain_name})

    def delete_domain_plan_relationship(self, domain_name, plan):
        """删除域和计划之间的关系
        
        Args:
            domain_name: 域名称
            plan: 包含计划名称以及计划内容的字典，格式为 {"plan": "计划名称", "plan_content": "计划内容"}
            
        Returns:
            删除结果
        """
        query = f"""
        MATCH (a:Domain {{domain_name: $domain_name}})-[r]->(b:Plan {{plan: $plan}})
        DELETE r
        RETURN r
        """
        return self.run_query(query, {"domain_name": domain_name, "plan": plan["plan"]})

    def delete_all_intent_domain_plan_data(self):
        """删除所有 Intent、Domain 和 Plan 节点及其关系
        
        Returns:
            删除结果
        """
        # 先删除所有关系
        query1 = "MATCH (a:Intent)-[r]->(b:Domain) DELETE r"
        self.run_query(query1)

        query2 = "MATCH (a:Domain)-[r]->(b:Plan) DELETE r"
        self.run_query(query2)

        # 再删除所有 Intent节点
        query3 = "MATCH (n:Intent) DELETE n"
        self.run_query(query3)
        
        # 再删除所有 Domain 节点
        query4 = "MATCH (n:Domain) DELETE n"
        self.run_query(query4)
        
        # 最后删除所有 Plan 节点
        query5 = "MATCH (n:Plan) DELETE n"
        return self.run_query(query5)

if __name__ == "__main__":
    gc = Neo4jConnection()
    gc.connect()
    plan = """
    1.上下文获取与指标匹配：输入结构化意图，输出用指标描述的业务；
    2.SQL转换：输入指标描述的业务，输出SQL描述的业务；
    3.SQL执行：输入SQL，输出指标计算结果；
    4.图表可视化与答案生成：输入计算结果，输出图表+文字内容描述。
    """
    # gc.delete_domain_plan_relationship("看趋势_销售分析域", plan)
