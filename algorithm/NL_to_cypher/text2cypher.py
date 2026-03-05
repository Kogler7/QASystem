import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
from common.llm_client import complete as call_llm

import json


class Text2CypherGenerator:
    def __init__(self):
        self.prompt_template = """你是一个Neo4j Cypher查询生成专家。请根据提供的图谱Schema，将用户的自然语言问题转换为准确、可执行的Cypher查询语句。

        图谱Schema如下：
        {graph_schema}
        
        请严格遵循以下要求：
        1. 只返回Cypher查询语句本身，不要包含任何额外的解释、Markdown代码块标记或说明文字。
        2. 确保生成的Cypher语法正确，并符合提供的Schema。
        3.- 路径查询：MATCH p=(b:Label)-[r*]->(n) RETURN p UNION MATCH p=(n)-[r*]->(b:Label) RETURN p LIMIT N
        - 计数查询：MATCH (n:Label) RETURN count(n)
        
        4. **返回值**：
           - 当问题询问"路径"时，返回整个路径：RETURN p
           - 当问题询问"节点"时，返回特定节点：RETURN a, b, c
        
        5. **语法规则**：
           - 路径变量用 p= 开头
           - 节点变量用小写字母：a, b, c, n, m
           - 关系变量用 r
           - 使用 UNION 合并查询，不要用 OR
           - 正确使用 LIMIT
        
        示例：
        问题：找出图中所有Article节点的总数。
        Schema：Graph schema: Relevant node labels and their properties are:\nArticle
        Cypher：MATCH (n:Article) RETURN count(n)
        
        问题：Identify three paths where Article is a start or end node!
        Schema：Graph schema: Relevant node labels and their properties are:\nArticle
        Cypher：MATCH p=(b:Article)-[r*]->(n) RETURN p UNION MATCH p=(n)-[r*]->(b:Article) RETURN p LIMIT 3
        
        现在，请为以下问题生成Cypher查询：
        问题：{user_question}
        Schema：{graph_schema}
        Cypher："""

    def generate(self, question, schema):
        prompt = self.prompt_template.format(user_question=question, graph_schema=schema)
        response = call_llm(prompt)
        return response



def load_dataset(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            # 读取整个文件（单行）
            data = json.load(f)  # 直接用json.load读取文件对象

        print(f"✓ 成功加载 {len(data)} 条记录")
        return data

    except FileNotFoundError:
        print(f"错误: 文件 '{file_path}' 不存在")
        return []
    except json.JSONDecodeError as e:
        print(f"JSON解析错误: {e}")

def evaluate(generator, dataset):
    results = []
    for i, item in enumerate(dataset):
        question = item["Question"]
        schema = item["Schema"]
        gold_cypher = item["Cypher"].strip()

        # 步骤1: 生成Cypher
        try:
            predicted_cypher = generator.generate(question, schema)
        except Exception as e:
            predicted_cypher = ""
            print(f"生成第{i}条数据时出错: {e}")

        # 步骤2: 基础评估（这里以字符串精确匹配和包含关键部分为例）
        print(predicted_cypher)
        print(gold_cypher)
        print("\n")
        exact_match = (predicted_cypher == gold_cypher)
        # 更复杂的评估可以在这里加入：连接真实数据库执行并对比结果

        results.append({
            "id": i,
            "question": question,
            "gold_cypher": gold_cypher,
            "predicted_cypher": predicted_cypher,
            "exact_match": exact_match
        })
    return results

def calculate_metrics(results):
    total = len(results)
    exact_matches = sum([r["exact_match"] for r in results])
    exact_match_rate = exact_matches / total * 100
    print(f"总计 {total} 条数据")
    print(f"匹配率: {exact_match_rate:.2f}%")
    # 可以打印错误案例进行分析
    for r in results:
        if r["exact_match"]<0.5:
            print(f"\n--- 不匹配案例 ID: {r['id']} ---")
            print(f"问题: {r['question']}")
            print(f"标准答案: {r['gold_cypher']}")
            print(f"模型生成: {r['predicted_cypher']}")


def get_user_input():
    """获取用户输入"""
    print("=" * 60)
    print("          Text2Cypher 查询生成工具")
    print("=" * 60)

    # 获取问题
    question = input("\n请输入要查询的问题: ").strip()
    if not question:
        print("问题不能为空！")
        return None, None

    # 获取Schema
    print("\n请选择Schema来源：")
    print("1. 使用默认Schema")
    print("2. 手动输入Schema")

    choice = input("请选择 (1-2): ").strip()

    if choice == '1':
        # 默认Schema
        schema = """Graph schema: Relevant node labels and their properties are:
        Article
        Keyword
        Author
        Journal
        Categories
        DOI
        Report
        UpdateDate
        Topic"""
        print("使用默认Schema")
    elif choice == '2':
        # 手动输入
        print("\n请输入Schema（输入空行结束）：")
        lines = []
        while True:
            line = input()
            if not line:
                break
            lines.append(line)
        schema = '\n'.join(lines)
        print("Schema输入完成")

    else:
        print("无效选择，使用默认Schema")
        schema = """Graph schema: Relevant node labels and their properties are:
        Article
        Keyword
        Author
        Journal
        Categories
        DOI
        Report
        UpdateDate
        Topic"""

    return question, schema
# 主程序
if __name__ == "__main__":
    # 1. 初始化你的生成器
    generator = Text2CypherGenerator()
    # # 2. 加载你的数据集
    # dataset = load_dataset("parametric_trainer_with_repeats.json")
    # # 3. 进行评估
    # eval_results = evaluate(generator, dataset)
    # # 4. 计算并输出指标
    # calculate_metrics(eval_results)

    # 获取用户输入
    question, schema = get_user_input()

    # 生成Cypher查询
    print("\n" + "-" * 60)
    print("正在生成Cypher查询...")
    print("-" * 60)

    try:
        cypher = generator.generate(question, schema)
        print(f"问题: {question}")
        print(f"生成的Cypher查询:\n")
        print(cypher)
        print("=" * 60)
    except Exception as e:
        print(f"生成失败: {e}")