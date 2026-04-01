from ir_utils.search_plan import SearchPlan
from intent_recognition.intent_app import IntentRecognition
from intent_recognition.agent_intent import run_tool_loop
import json

class PlanApp:
    def __init__(self):
        self.search_plan = SearchPlan()
        self.intent_app = IntentRecognition()

    def get_plan_from_domain(self, domain_name):
        """根据域名称获取计划"""
        return self.search_plan.search_plan(domain_name)

    def get_plan_from_query(self, query):
        """根据询问获取计划"""
        # results = self.intent_app.generate_result(query)
        results = run_tool_loop(query)

        if results:
            # intent = results.get("最终筛选的意图")
            intent = json.loads(results.get("json_content"))
            intent_name = intent.get("intent_name")
            domain = intent.get("domain")
            intent_domain = f"{intent_name}_{domain}"
            plan = self.search_plan.search_plan(intent_domain)[0]
            # 去掉计划内容中的换行符
            if "p.plan" in plan:
                plan["p.plan"] = plan["p.plan"].replace('\n', '')
                plan["p.plan"] = plan["p.plan"].replace(' ', '')
            return json.dumps(plan, ensure_ascii=False, indent=2)
        return json.dumps({"message": "未识别到意图,所以无法获取计划信息"}, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    plan_app = PlanApp()
    plan = plan_app.get_plan_from_query("公开采购与非公开采购金额构成如何？")
    print("公开采购与非公开采购金额构成如何？")
    print(plan)
    print(type(plan))