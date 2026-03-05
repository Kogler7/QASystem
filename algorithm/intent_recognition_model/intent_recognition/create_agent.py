"""
兼容层：LangChain 无 create_agent，用 with_structured_output 实现相同调用方式。
"""
from typing import Any


def create_agent(model, response_format, system_prompt: str):
    """返回可 invoke 的 agent，invoke(messages) -> {"structured_response": 结构化结果}。"""
    structured_llm = model.with_structured_output(response_format)

    class Agent:
        def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
            messages = payload.get("messages") or []
            content = ""
            for m in messages:
                if m.get("role") == "user" and "content" in m:
                    content = m["content"]
                    break
            out = structured_llm.invoke(
                [("system", system_prompt), ("human", content)]
            )
            if hasattr(out, "model_dump"):
                out = out.model_dump()
            elif not isinstance(out, dict):
                out = dict(out) if hasattr(out, "__iter__") else {"raw": str(out)}
            return {"structured_response": out}

    return Agent()
