import sys
import os

# 添加项目根目录到Python路径
current_file = os.path.abspath(__file__)
tools_dir = os.path.dirname(current_file)
qa_dir = os.path.dirname(tools_dir)
ans_dir = os.path.dirname(qa_dir)
algorithm_dir = os.path.dirname(ans_dir)
project_root = os.path.dirname(algorithm_dir)
sys.path.insert(0, project_root)

from typing import Dict, List
from datetime import datetime


class AnswerGenerationTool:
    """
    答案生成工具：整合所有信息生成最终答案
    """
    
    def __init__(self, neo4j_client, args):
        self.client = neo4j_client
        self.args = args
    
    def generate_answer(self, solution: Dict, graph_paths: Dict, question: str) -> Dict:
        """
        生成最终答案
        输入：解决方案 + 图谱路径 + 原始问题
        输出：最终答案
        """
        try:
            # 1. 从解决方案中提取信息
            solution_description = solution.get("方案描述", "")
            outputs = solution.get("输出", [])
            steps = solution.get("步骤", [])
            
            # 2. 从图谱路径中提取信息
            answer_path = graph_paths.get("answer_path", [])
            reasoning_chains = graph_paths.get("reasoning_chains", [])
            
            # 3. 生成最终答案
            final_answer = self._generate_final_answer(
                question=question,
                solution_description=solution_description,
                outputs=outputs,
                steps=steps,
                answer_path=answer_path
            )
            
            # 4. 生成详细信息
            detailed_info = {
                "solution": solution,
                "graph_paths": graph_paths,
                "steps": steps,
                "generated_at": datetime.now().isoformat()
            }
            
            return {
                "success": True,
                "answer": final_answer,
                "detailed_info": detailed_info
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "message": "答案生成失败"
            }
    
    def _generate_final_answer(self, question: str, solution_description: str, 
                              outputs: List, steps: List, answer_path: List) -> str:
        """
        生成最终答案
        """
        if outputs:
            # 如果有输出，使用第一个输出作为答案
            main_answer = outputs[0]
        else:
            main_answer = "抱歉，无法生成答案"
        
        # 构建最终答案
        final_answer = f"针对问题：{question}\n\n"
        final_answer += f"答案：{main_answer}\n\n"
        
        if steps:
            final_answer += "解决步骤：\n"
            for i, step in enumerate(steps, 1):
                final_answer += f"{i}. {step}\n"
        
        if answer_path:
            final_answer += "\n知识图谱路径：\n"
            for path in answer_path:
                depth = path.get("depth", 1)
                nodes = path.get("nodes", [])
                relationships = path.get("relationships", [])
                
                final_answer += f"深度 {depth}：\n"
                for node in nodes:
                    final_answer += f"  - {node.get('label', node.get('id', ''))}\n"
                for rel in relationships:
                    final_answer += f"  - {rel.get('from')} → {rel.get('type')} → {rel.get('to')}\n"
        
        return final_answer
