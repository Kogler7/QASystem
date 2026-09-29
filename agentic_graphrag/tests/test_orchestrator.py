from agentic_graphrag import AgenticGraphRAGOrchestrator


def test_orchestrator_returns_evidence():
    result = AgenticGraphRAGOrchestrator().answer("公开采购与非公开采购金额构成如何？")

    assert result.answer
    assert result.problem_frame.question_type == "metric"
    assert result.problem_frame.knowledge_space == "采购管理域-信息"
    assert result.evidence_pack.items
    assert result.tool_trace
