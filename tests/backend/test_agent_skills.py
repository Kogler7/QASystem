from pathlib import Path

from agentcode.skillsets.loader import SkillLoader


ROOT = Path(__file__).resolve().parents[2]
SKILLSET_DIR = ROOT / "agent" / "skills"
EXPECTED_SKILLS = {"graph-qa"}


def test_graph_grounded_skills_are_discoverable():
    loader = SkillLoader(skillset_dir=SKILLSET_DIR)
    refs = loader.discover_skills()
    assert {ref.id for ref in refs} == EXPECTED_SKILLS
    assert all(ref.inject_layer == "developer" for ref in refs)
    assert set(loader._path_by_id) == EXPECTED_SKILLS


def test_skills_use_atomic_graph_tools_without_removed_planning_tools():
    texts = {
        skill_id: (SKILLSET_DIR / skill_id / "SKILL.md").read_text(encoding="utf-8")
        for skill_id in EXPECTED_SKILLS
    }
    combined = "\n".join(texts.values())

    for tool_name in (
        "locate_knowledge_space",
        "search_entities",
        "get_entity_details",
        "search_graph",
    ):
        assert tool_name in combined

    for removed_tool in (
        "recognize_intent",
        "batch_recognize_intent",
        "get_plan_from_intent",
    ):
        assert removed_tool not in combined
    assert "problem-resolve-path-display" not in combined


def test_skills_require_graph_sourced_business_knowledge_and_solutions():
    graph = (SKILLSET_DIR / "graph-qa" / "SKILL.md").read_text(encoding="utf-8")
    assert "禁止用你自己的记忆、常识或训练知识补齐" in graph
    assert "完整给出" in graph
    assert "缺口报告" in graph
    assert "禁止自行编造" in graph
    assert "answer-v1" in graph
    for field in ("conclusion", "confidence", "evidence", "gaps"):
        assert f"`{field}`" in graph
    assert "`solution_path`：预留字段，固定 `[]`" in graph
