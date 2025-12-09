from answer_Interface import Neo4jQAService
service = Neo4jQAService()

# 回答问题
result = service.answer_question("吴敏经常访问哪里？")
print(f"答案: {result['answer']}")