from pathlib import Path

from agentcode.skillsets.loader import SkillLoader


ROOT = Path(__file__).resolve().parents[2]
SKILLSET_DIR = ROOT / "agent" / "skills"
EXPECTED_SKILLS = {"problem-solve", "semantic-align", "plan-execute"}


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
        "link_entities",
        "query_entity_properties",
        "search_graph",
    ):
        assert tool_name in combined

    for removed_tool in (
        "recognize_intent",
        "batch_recognize_intent",
        "get_plan_from_intent",
    ):
        assert removed_tool not in combined
    assert "problem-resolve-path-display" in combined


def test_skills_require_graph_sourced_business_knowledge_and_solutions():
    problem = (SKILLSET_DIR / "problem-solve" / "SKILL.md").read_text(encoding="utf-8")
    align = (SKILLSET_DIR / "semantic-align" / "SKILL.md").read_text(encoding="utf-8")
    execute = (SKILLSET_DIR / "plan-execute" / "SKILL.md").read_text(encoding="utf-8")

    assert "禁止从模型记忆补充" in problem
    assert "证据门" in problem
    assert "evidence_ledger" in align
    assert "solution_class" in align
    assert "禁止自行创造一个替代方案" in problem
    assert "不得自行设计一个业务方案" in execute
    assert "禁止自行设计业务方案冒充图谱方案" in execute
    assert "answer-v1" in problem
    assert '"conclusion"' in problem
    assert '"confidence"' in problem
    assert "【结论】" in problem and "不要写【结论】" in problem
