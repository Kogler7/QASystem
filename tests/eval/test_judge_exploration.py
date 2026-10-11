import pytest

from eval.tools.judge_exploration import build_jobs, summarize, validate_answers, window_bounds


def test_real_large_window_keeps_surrounding_items():
    dataset = {"cases": {"a": {"entities": {"golden": list(range(55)), "predicted": list(range(31))}}},
               "pairs": [{"id": "x", "case": "a", "collection": "entities", "g": 5, "p": 22}]}
    job = build_jobs(dataset, ["api_large"], 1)[0]
    assert job["g_bounds"] == [0, 40]
    assert job["p_bounds"] == [20, 31]
    assert window_bounds(54, 40, 55) == (40, 55)


@pytest.mark.parametrize("value", [{}, {"answers": {}}, {"answers": {"g0_p0": {"choice": "maybe"}}}])
def test_missing_or_invalid_choice_fails(value):
    with pytest.raises(ValueError):
        validate_answers(value)


def test_granularity_excluded_and_missing_is_not_negative():
    dataset = {"repetitions": 3, "pairs": [{"id": "p", "label": "yes"}, {"id": "g", "label": "granularity"}]}
    row = {"condition": "api_choice", "status": "succeeded", "elapsed_seconds": 1,
           "decisions": {"p": True, "g": False}}
    stats = summarize(dataset, [row], 1)["conditions"]["api_choice"]
    assert stats["positive_answers"] == stats["positive_correct"] == 1
    assert stats["negative_answers"] == 0
    assert stats["missing_answers"] == 4
    assert stats["stable_pairs"] == 0
