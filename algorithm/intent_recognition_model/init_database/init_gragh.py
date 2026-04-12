from ir_utils.neo4j_connection import Neo4jConnection
import json
import os

class InitGraph:
    def __init__(self):
        self.gc = Neo4jConnection()
        self.gc.connect()

    def create_intent_struct(self, intent_id, intent_name, domain, plan):
        """创建 Intent 结构"""
        self.gc.create_intent_node(intent_id, intent_name)
        self.gc.create_intent_plan_relationship(intent_id, plan, domain)

    def create_intent_plan_struct(self, intent_id, plan, domain):
        """先创建计划节点，再创建关系节点"""
        self.gc.create_plan_node(plan)
        self.gc.create_intent_plan_relationship(intent_id, plan, domain)

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

    def delete_all_intent_domain_plan_data(self):
        """删除所有 Intent、Domain 和 Plan 数据"""
        self.gc.delete_all_intent_domain_plan_data()

if __name__ == "__main__":
    gc = Neo4jConnection()
    init_graph = InitGraph()
    # init_graph.batch_create_intent_structs()
    # gc.delete_all_intent_domain_plan_data()
    
    # 方案模型数据
    plan_models = [
        {
            "方案ID": "SOL-001",
            "方案类别": "分析类问题解决方案",
            "方案目标": "将结构化意图转换为指标描述的业务逻辑",
            "输入": ["结构化意图"],
            "输出": ["指标描述的业务逻辑"],
            "约束": ["基于业务指标体系匹配", "意图语义准确解析", "指标定义无歧义"],
            "控制逻辑": None,
            "置信度": 0.9
        },
        {
            "方案ID": "SOL-002",
            "方案类别": "分析类问题解决方案",
            "方案目标": "将指标描述的业务逻辑转换为可执行SQL语句",
            "输入": ["指标描述的业务逻辑"],
            "输出": ["SQL描述的业务查询语句"],
            "约束": ["SQL语法规范合法", "表字段与指标一一对应", "避免逻辑错误与性能风险"],
            "控制逻辑": None,
            "置信度": 0.9
        },
        {
            "方案ID": "SOL-003",
            "方案类别": "分析类问题解决方案",
            "方案目标": "执行SQL语句并完成指标数值计算",
            "输入": ["SQL查询语句"],
            "输出": ["指标计算结果数据集"],
            "约束": ["数据源权限合法", "计算结果准确无误", "执行超时控制"],
            "控制逻辑": None,
            "置信度": 0.9
        },
        {
            "方案ID": "SOL-004",
            "方案类别": "分析类问题解决方案",
            "方案目标": "基于指标计算结果生成可视化图表与自然语言答案",
            "输入": ["指标计算结果数据集"],
            "输出": ["可视化图表", "自然语言文字描述内容"],
            "约束": ["图表类型与数据匹配", "描述语言简洁易懂", "结果与计算数据一致"],
            "控制逻辑": None,
            "置信度": 0.9
        }
    ]
    
    # 创建计划字典
    plan = {
        "plan": "销售分析域的看趋势计划方案",
        "plan_content": json.dumps(plan_models, ensure_ascii=False, indent=2)
    }
    
    # 添加到图谱
    init_graph.create_intent_plan_struct("INT20260211002", plan, "销售分析域")
    
    gc.close()