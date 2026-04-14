from ir_utils.neo4j_connection import Neo4jConnection


class SearchPlan:
    def __init__(self):
        self.gc = Neo4jConnection()
        self.gc.connect()

    def search_plan(self, intent_id, domain):
        """根据意图和域名查询计划
        
        Args:
            intent_id: 意图 ID
            domain: 域名称，作为关系类型
            
        Returns:
            包含 description（字符串）和 steps（列表）的字典
        """
        # 构建查询，查找 Intent 节点通过 domain 关系连接到 Plan 节点的路径
        query = f"""
        MATCH (i:Intent {{id: $intent_id}})-[:{domain}]->(p:Plan)
        RETURN p
        """
        
        results = self.gc.run_query(query, {"intent_id": intent_id})
        if results:
            # 提取第一个结果中的 p 数据
            first_result = results[0]
            if "p" in first_result:
                plan_data = first_result["p"]
                # 将 plan_content 从字符串转换为 JSON 对象
                if "plan_content" in plan_data:
                    import json
                    try:
                        plan_data["description"] = plan_data["description"]
                        plan_data["steps"] = json.loads(plan_data["steps"])
                    except json.JSONDecodeError:
                        # 如果解析失败，保持原始字符串
                        pass
                return plan_data
        return None

if __name__ == "__main__":
    search_plan = SearchPlan()
    # 示例：传递意图ID和域作为关系类型
    plans = search_plan.search_plan("INT20260211001", "采购管理域")
    if plans:
        print(type(plans))  #<class 'dict'>
        print(type(plans.get("description")))  #<class 'str'>
        print(type(plans.get("steps")))  #<class 'list'>
        print(plans)
    search_plan.gc.close()
