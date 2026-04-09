from ir_utils.neo4j_connection import Neo4jConnection


class SearchPlan:
    def __init__(self):
        self.gc = Neo4jConnection()
        self.gc.connect()

    def search_plan(self, domain_name):
        """根据域名称查询计划
        
        Args:
            domain_name: 域名称
            
        Returns:
            计划对象
        """
        query = """
        MATCH (d:Domain {domain_name: $domain_name})
        MATCH (d)-[:域_计划]->(p:Plan)
        RETURN p.plan
        """
        results = self.gc.run_query(query, {"domain_name": domain_name})
        if results:
            return results
        else:
            return None

if __name__ == "__main__":
    search_plan = SearchPlan()
    plans = search_plan.search_plan("看构成_采购管理域")
    print(plans[0].get("plan"))
    search_plan.gc.close()
