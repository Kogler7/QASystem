from ir_utils.neo4j_connection import Neo4jConnection
import json
import os

class InitGraph:
    def __init__(self):
        self.gc = Neo4jConnection()
        self.gc.connect()

    def create_intent_struct(self, intent_id, intent_name, domain_name, rel_type):
        """创建 Intent 结构"""
        self.gc.create_intent_node(intent_id, intent_name)
        self.gc.create_domain_node(domain_name)
        self.gc.create_intent_domain_relationship(intent_id, domain_name, rel_type)

    def create_domain_plan_struct(self, domain_name, plan, rel_type):
        """先创建计划节点，再创建关系节点"""
        self.gc.create_plan_node(plan)
        self.gc.create_domain_plan_relationship(domain_name, plan, rel_type)

    def get_intent_struct_from_json(self):
        """获取 Intent 图谱结构
        
        Returns:
            list: 包含每个意图的 intent_id, domain, intent_name 的列表
        """
        # 构建 intent.json 文件路径
        json_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'data', 'intent.json'
        )
        
        try:
            # 读取 intent.json 文件
            with open(json_path, 'r', encoding='utf-8') as f:
                intents = json.load(f)
            
            # 提取每个意图的 intent_id, domain, intent_name
            intent_structs = []
            for intent in intents:
                if isinstance(intent, dict):
                    intent_id = intent.get('intent_id')
                    domain = intent.get('domain')
                    intent_name = intent.get('intent_name')
                    
                    if intent_id and domain and intent_name:
                        intent_structs.append({
                            'intent_id': intent_id,
                            'domain': domain,
                            'intent_name': intent_name
                        })
            
            return intent_structs
        except Exception as e:
            print(f"读取 intent.json 文件失败: {str(e)}")
            return []

    def batch_create_intent_structs(self):
        """批量创建 Intent 结构
        
        从 intent.json 文件中提取意图信息，并批量插入到 Neo4j 图谱中
        """
        # 获取意图结构
        intent_structs = self.get_intent_struct_from_json()
        
        if not intent_structs:
            print("没有找到意图信息")
            return
        
        # 批量创建意图结构
        for i, intent in enumerate(intent_structs):
            try:
                unique_domain_name = f"{intent['intent_name']}_{intent['domain']}"
                self.create_intent_struct(
                    intent_id=intent['intent_id'],
                    intent_name=intent['intent_name'],
                    domain_name=unique_domain_name,
                    rel_type="意图_域"
                )
                print(f"创建意图结构 {i+1}/{len(intent_structs)}: {intent['intent_name']}")
            except Exception as e:
                print(f"创建意图结构失败: {str(e)}")
                continue
        
        print(f"批量创建完成，共处理 {len(intent_structs)} 个意图")

    def create_domain_plan_relationship(self, domain_name, plan):
        """创建 Domain 和 Plan 之间的关系"""
        self.gc.create_domain_plan_relationship(domain_name, plan)

    def delete_all_intent_domain_data(self):
        """删除所有 Intent 和 Domain 数据"""
        self.gc.delete_all_intent_domain_data()

if __name__ == "__main__":
    gc = Neo4jConnection()
    init_graph = InitGraph()
    # init_graph.batch_create_intent_structs()
    # gc.delete_all_intent_domain_data()
    plan = """
    1.上下文获取与指标匹配：输入结构化意图，输出用指标描述的业务；
    2.SQL转换：输入指标描述的业务，输出SQL描述的业务；
    3.SQL执行：输入SQL，输出指标计算结果；
    4.图表可视化与答案生成：输入计算结果，输出图表+文字内容描述。
    """
    init_graph.create_domain_plan_struct("看构成_采购管理域", plan, rel_type="域_计划")

    gc.close()