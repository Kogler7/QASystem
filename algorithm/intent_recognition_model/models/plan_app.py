from ir_utils.search_plan import SearchPlan
from models.agent_intent import intent_recognition
import json

class PlanApp:
    def __init__(self):
        self.search_plan = SearchPlan()

    def get_plan_from_intent(self, intent_id="", domain=""):
        """根据意图名称和域名称获取计划"""
        if intent_id and domain:
            # 传递意图ID和域作为关系类型
            plan = self.search_plan.search_plan(intent_id, domain)
            # 没有找到计划
            if not plan:
                return {
                            "understanding": "未找到计划",
                            "pass": False
                        }
                        # 获取最终计划内容
            if "description" in plan and "steps" in plan:
                final_description = plan.get("description")
                final_steps = json.loads(plan.get("steps"))

            # 评估计划是否存在
            if not self.is_plan_exist(final_description):
                return {
                            "understanding": "计划内容不存在",
                            "pass": False
                        }
            
            final_results = {
                    "understanding": "计划存在",
                    "pass": True,
                    "plan": {
                        "description": final_description,
                        "steps": final_steps
                    }
                }

            return final_results
        # 没有找到意图和域
        return {"understanding": "未找到意图或域", "pass": False}

    def get_plan_from_model(self, problem_model):
        print(f"problem_model: {problem_model}")
        """根据用户查询获取计划（先调用意图识别，再生成计划）"""
        try:
            # 第一步：调用意图识别
            intent_result = intent_recognition(problem_model)
            intent_data = intent_result.get('intent', {})
            
            if not intent_data:
                return {
                            "understanding": "意图识别失败",
                            "pass": False
                            }
            
            # 解析意图识别结果
            intent_id = intent_data.get('intent_id')
            domain = intent_data.get('domain')
            
            if not intent_id or not domain:
                return {
                            "understanding": "意图识别结果中缺少必要字段",
                            "pass": False
                        }
            
            # 第二步：根据意图和域生成计划
            final_results = self.get_plan_from_intent(intent_id, domain)
            final_results["problem_model"] = problem_model
            final_results["details"] = {}
            return final_results

        except Exception as e:
            return {
                        "understanding": f"生成计划失败: {str(e)}",
                        "pass": False
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
    problem_model = {
            "problem_model":     
                {
                    "problem_id": "QKB-001",
                    "description": "公开采购与非公开采购金额构成如何？",
                    "problem_type": "描述型问题",
                    "constraints": ["需基于公开数据源"],
                    "target_object": "采购金额构成",
                    "stakeholders": ["采购部门", "财务部门"],
                    "goals": ["明确金额分配比例"],
                    "status": "分析阶段",
                    "created_at": "2026-04-08T00:54:30.393365",
                    "related_entities": ["公开采购", "非公开采购", "金额构成"],
                    "children": []
                }
        }
    plan = plan_app.get_plan_from_model(problem_model)
    print("采购管理域的计划")
    print(plan)
    print(type(plan))