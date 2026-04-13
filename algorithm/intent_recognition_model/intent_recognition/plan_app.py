from ir_utils.search_plan import SearchPlan
from intent_recognition.intent_app import IntentRecognition
from intent_recognition.agent_intent import intent_recognition
import json

class PlanApp:
    def __init__(self):
        self.search_plan = SearchPlan()
        self.intent_app = IntentRecognition()

    def get_plan_from_intent(self, intent_id="", domain=""):
        """根据意图名称和域名称获取计划"""
        if intent_id and domain:
            # 传递意图ID和域作为关系类型
            plan = self.search_plan.search_plan(intent_id, domain)
            # 没有找到计划
            if not plan:
                return {
                        "success": False,
                        "data": {
                            "understanding": "未找到计划",
                            "pass": False
                            }
                    }
            # 获取最终计划内容
            if "plan_content" in plan:
                final_plan = plan.get("plan_content")

            # 评估计划是否存在
            if not self.is_plan_exist(final_plan):
                return {
                        "success": False,
                        "data": {
                            "understanding": "计划不存在",
                            "pass": False
                            }
                    }
            
            final_results = {
                "success": True,
                "data": {
                    "understanding": "计划存在",
                    "pass": True,
                    "plan": final_plan
                }
            }
            # return json.dumps(final_results, ensure_ascii=False, indent=2)
            return final_results
        # 没有找到意图和域
        return {"success": False, "data": {"understanding": "未找到意图或域", "pass": False}}

    def get_plan_from_model(self, problem_model):
        """根据用户查询获取计划（先调用意图识别，再生成计划）"""
        try:
            # 第一步：调用意图识别
            intent_result = intent_recognition(problem_model)
            intent_data = intent_result.get('data', {}).get('intent')
            
            if not intent_data:
                return {
                        "success": False,
                        "data": {
                            "understanding": "意图识别失败",
                            "pass": False
                            }
                    }
            
            # 解析意图识别结果
            intent_id = intent_data.get('intent_id')
            domain = intent_data.get('domain')
            
            if not intent_id or not domain:
                return {
                        "success": False,
                        "data": {
                            "understanding": "意图识别结果中缺少必要字段",
                            "pass": False
                            }
                    }
            
            # 第二步：根据意图和域生成计划
            return self.get_plan_from_intent(intent_id, domain)
        except Exception as e:
            return {
                    "success": False,
                    "data": {
                        "understanding": f"生成计划失败: {str(e)}",
                        "pass": False
                        }
                }

    def is_plan_exist(self, plan):
        """评估计划是否存在"""
        if plan is None:
            return False
        if isinstance(plan, str):
            # 检查是否为空字符串或仅包含空白字符
            return bool(plan.strip())
        return True

if __name__ == "__main__":
    plan_app = PlanApp()
    json_input = {
            "problem_model":     
                {
                    "问题ID": "QKB-001",
                    "问题描述": "公开采购与非公开采购金额构成如何？",
                    "问题类型": "描述型问题",
                    "约束": ["需基于公开数据源"],
                    "目标对象": "采购金额构成",
                    "干系人": ["采购部门", "财务部门"],
                    "目标": ["明确金额分配比例"],
                    "当前状态": "分析阶段",
                    "创建时间": "2026-04-08T00:54:30.393365",
                    "相关实体": ["公开采购", "非公开采购", "金额构成"]
                }
        }
    problem_model = json.loads(json_input)["problem_model"]
    plan = plan_app.get_plan_from_model(problem_model)
    print("采购管理域的计划")
    print(plan)
    print(type(plan))