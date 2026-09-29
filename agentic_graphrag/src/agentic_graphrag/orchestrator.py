from __future__ import annotations

import re
import uuid

from .schemas import EvidencePack, FinalGraphAnswer, GraphAnswerState, ProblemFrame, QuestionType
from .settings import settings
from .tool_client import ToolClient, build_tool_client


class AgenticGraphRAGOrchestrator:
    def __init__(self, tool_client: ToolClient | None = None) -> None:
        self.tool_client = tool_client or build_tool_client()

    def answer(self, question: str, session_id: str | None = None) -> FinalGraphAnswer:
        normalized = self._normalize_question(question)
        frame = ProblemFrame(
            question=question,
            normalized_question=normalized,
            question_type=self._route_question(normalized),
        )
        state = GraphAnswerState(
            session_id=session_id or str(uuid.uuid4()),
            problem_frame=frame,
        )

        space, trace = self.tool_client.locate_knowledge_space(question)
        state.tool_trace.append(trace)
        state.problem_frame.knowledge_space = space

        entities, trace = self.tool_client.link_entities(question)
        state.tool_trace.append(trace)
        state.linked_entities = entities
        if not entities:
            state.missing_info.append("未能在知识图谱中链接到明确实体。")

        for entity in entities[:3]:
            paths, trace = self.tool_client.search_graph(entity, question, depth=1)
            state.tool_trace.append(trace)
            state.graph_paths.extend(paths)

        evidence, trace = self.tool_client.retrieve_supporting_evidence(
            question=question,
            entities=state.linked_entities,
            paths=state.graph_paths,
        )
        state.tool_trace.append(trace)
        state.evidence_pack = EvidencePack(items=evidence[: settings.max_evidence])

        confidence = self._score_confidence(state)
        answer = self._compose_answer(state, confidence)

        return FinalGraphAnswer(
            answer=answer,
            confidence=confidence,
            problem_frame=state.problem_frame,
            evidence_pack=state.evidence_pack,
            tool_trace=state.tool_trace,
            missing_info=state.missing_info,
            conflicts=state.conflicts,
        )

    @staticmethod
    def _normalize_question(question: str) -> str:
        return re.sub(r"\s+", " ", question).strip()

    @staticmethod
    def _route_question(question: str) -> QuestionType:
        if any(word in question for word in ("多少", "金额", "数量", "比例", "构成")):
            return "metric"
        if any(word in question for word in ("趋势", "同比", "环比", "月度")):
            return "trend"
        if any(word in question for word in ("为什么", "导致", "影响", "关系")):
            return "relation"
        if any(word in question for word in ("如何", "流程", "步骤", "处置")):
            return "procedure"
        if any(word in question for word in ("分析", "综合", "对比")):
            return "synthesis"
        if any(word in question for word in ("是什么", "属性", "定义", "口径")):
            return "fact"
        return "unknown"

    @staticmethod
    def _score_confidence(state: GraphAnswerState) -> str:
        if state.conflicts:
            return "low"
        if len(state.evidence_pack.items) >= 3 and state.linked_entities:
            return "high"
        if state.evidence_pack.items:
            return "medium"
        return "low"

    @staticmethod
    def _compose_answer(state: GraphAnswerState, confidence: str) -> str:
        top_items = state.evidence_pack.top(5)
        if not top_items:
            return "当前没有检索到足够证据，无法可靠回答该问题。"

        lines = [
            f"已定位知识空间：{state.problem_frame.knowledge_space or '未确定'}。",
            f"问题类型判断为：{state.problem_frame.question_type}。",
            "基于当前证据，结论如下：",
        ]

        entity_names = [entity.name for entity in state.linked_entities]
        if entity_names:
            lines.append("关联到的核心实体包括：" + "、".join(entity_names) + "。")

        if state.problem_frame.question_type in {"metric", "trend"}:
            lines.append("涉及数值或趋势的问题需要进一步接入真实 SQL/Cypher 查询工具后给出计算结果。")
        else:
            lines.append("已根据图谱实体和路径证据生成初步回答。")

        cited = "、".join(item.id for item in top_items)
        lines.append(f"证据引用：{cited}。")
        lines.append(f"置信度：{confidence}。")
        return "\n".join(lines)
