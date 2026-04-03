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
        # 使用大模型生成的答案
        if outputs:
            main_answer = outputs[0]
        else:
            main_answer = "抱歉，无法生成答案"
        
        # 提取数据来源信息
        data_sources = []
        if answer_path:
            # 从大模型生成的答案中提取关键实体名称
            # 通过检查答案中提到的实体来判断哪些是相关的
            for path in answer_path:
                nodes = path.get("nodes", [])
                # 提取所有节点的数据来源（不仅仅是最后一个节点）
                for node in nodes:
                    node_label = node.get('label', node.get('id', ''))
                    # 检查节点标签的任何部分是否在答案中被提到
                    # 例如："CG_003 公开采购中标金额" -> 检查"CG_003"或"公开采购中标金额"
                    label_parts = node_label.replace('_', ' ').split()
                    is_mentioned = any(part in main_answer for part in label_parts if len(part) > 2)
                    
                    if is_mentioned or node_label in main_answer:
                        properties = node.get('properties', {})
                        if properties:
                            data_source = properties.get('数据来源表')
                            if data_source and data_source not in data_sources:
                                data_sources.append(data_source)
        
        # 如果有数据来源，添加到答案中
        if data_sources:
            data_source_str = "、".join(data_sources)
            main_answer += f" 数据来源于：{data_source_str}。"
        
        # 只返回答案部分，不包含其他信息
        return main_answer
