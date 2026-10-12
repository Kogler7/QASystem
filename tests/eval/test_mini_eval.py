from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from eval import mini_eval


def _case(workspace: Path, group: str, name: str) -> Path:
    case = workspace / group / name
    (case / "docs").mkdir(parents=True)
    (case / "docs" / "document.md").write_text("评测文本", encoding="utf-8")
    return case


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "test_data"
    for group, count in (("official", 4), ("standard", 3), ("rule", 1), ("simple", 1)):
        for index in range(1, count + 1):
            _case(root, group, f"{group}_{index:02d}")
    return root


def _params(tmp_path: Path, config=None) -> Path:
    path = tmp_path / "params.yaml"
    path.write_text(json.dumps({"mini_eval": config or {}, "runs_retention_days": 0}), encoding="utf-8")
    return path


def test_default_first_selection_is_bounded_and_retains_type_order(workspace):
    chosen, strategy, seed = mini_eval.select_cases(
        workspace, {"cases_per_type": 1, "per_type": {"official": 2}, "selection": "first", "seed": 42},
    )
    assert [path.relative_to(workspace).as_posix() for path in chosen] == [
        "official/official_01", "official/official_02", "rule/rule_01", "simple/simple_01", "standard/standard_01",
    ]
    assert strategy == "first"
    assert seed == 42
    assert all(path.is_absolute() for path in chosen)


def test_short_types_take_all_without_duplicates(workspace):
    chosen, _, _ = mini_eval.select_cases(workspace, {"cases_per_type": 2})
    counts = {name: sum(path.parent.name == name for path in chosen) for name in ("official", "rule", "simple", "standard")}
    assert counts == {"official": 2, "standard": 2, "rule": 1, "simple": 1}
    assert len(chosen) == len(set(chosen))


def test_discovery_uses_shared_hidden_reserved_and_docs_rules(workspace):
    _case(workspace, ".hidden", "hidden_01")
    _case(workspace, "_internal", "hidden_02")
    _case(workspace, "中车RAG验证数据集", "hidden_03")
    _case(workspace, "official", "_hidden")
    _case(workspace, "official", "ontology")
    (workspace / "official" / "000_no_docs").mkdir()
    (workspace / "notes").mkdir()
    chosen, _, _ = mini_eval.select_cases(workspace, {})
    assert [path.name for path in chosen] == ["official_01", "rule_01", "simple_01", "standard_01"]


def test_seeded_random_is_repeatable_and_varies_with_seed(workspace):
    first, _, seed = mini_eval.select_cases(workspace, {"selection": "random", "seed": 1, "cases_per_type": 2})
    repeated, _, _ = mini_eval.select_cases(workspace, {"selection": "random", "seed": 1, "cases_per_type": 2})
    other, _, _ = mini_eval.select_cases(workspace, {"selection": "random", "seed": 2, "cases_per_type": 2})
    assert first == repeated
    assert first != other
    assert seed == 1
    assert len(first) == 6


def test_null_seed_generates_a_printable_reproducible_seed(workspace, monkeypatch):
    monkeypatch.setattr(mini_eval.secrets, "randbits", lambda bits: 123456)
    chosen, _, seed = mini_eval.select_cases(workspace, {"selection": "random", "seed": None})
    repeated, _, _ = mini_eval.select_cases(workspace, {"selection": "random", "seed": seed})
    assert seed == 123456
    assert chosen == repeated


@pytest.mark.parametrize("value", [0, 3, -1, True, False, 1.0, "2", None])
@pytest.mark.parametrize("field", ["default", "override"])
def test_counts_must_be_integer_one_or_two(workspace, value, field):
    config = {"cases_per_type": value} if field == "default" else {"per_type": {"official": value}}
    with pytest.raises(ValueError, match="只允许整数 1 或 2"):
        mini_eval.select_cases(workspace, config)


@pytest.mark.parametrize("config,match", [
    (None, "必须是 YAML 对象"),
    ({"case_per_type": 2}, "未知配置项"),
    ({"per_type": []}, "必须是 YAML 对象"),
    ({"per_type": {"typo": 1}}, "不存在或没有有效 case"),
    ({"per_type": {" official": 1}}, "类型名称"),
    ({"per_type": {1: 1}}, "类型名称"),
    ({"selection": "all"}, "只允许 first 或 random"),
    ({"seed": True}, "必须是整数或 null"),
    ({"seed": 1.2}, "必须是整数或 null"),
    ({"seed": "42"}, "必须是整数或 null"),
])
def test_invalid_configuration_fails_explicitly(workspace, config, match):
    with pytest.raises(ValueError, match=match):
        mini_eval.select_cases(workspace, config)


def test_no_cases_and_missing_workspace_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="未发现任何有效 case"):
        mini_eval.select_cases(tmp_path, {})
    with pytest.raises(ValueError, match="工作区不存在"):
        mini_eval.select_cases(tmp_path / "missing", {})


def test_main_delegates_selected_paths_and_preserves_extract_options(workspace, tmp_path, monkeypatch):
    params = _params(tmp_path, {"per_type": {"official": 2}})
    child = Mock(return_value=subprocess.CompletedProcess([], 4))
    monkeypatch.setattr(mini_eval.subprocess, "run", child)
    result = mini_eval.main([
        "--workspace", str(workspace), "--mini-per-type", "official=2", "--match-mode", "llm",
        "--from-run", "frozen", "--reuse-extract-from", "old", "--max-case-seconds", "1800", "--mode", "extract",
    ])
    assert result == 4
    command = child.call_args.args[0]
    assert command[:2] == [sys.executable, str(Path(mini_eval.run.__file__).resolve())]
    parsed = mini_eval.run.parse_args(command[2:])
    assert len(parsed.targets) == 5
    assert all(Path(path).is_absolute() for path in parsed.targets)
    assert parsed.mode == ["extract"]
    assert parsed.from_run == "frozen"
    assert parsed.reuse_extract_from == "old"
    assert parsed.match_mode == "llm"
    assert parsed.max_case_seconds == 1800
    assert parsed.groups == parsed.cases == ""
    assert child.call_args.kwargs == {"check": False}


def test_default_params_does_not_switch_to_workspace_params(workspace, tmp_path, monkeypatch):
    params = _params(tmp_path, {"per_type": {"official": 2}})
    (workspace / "params.yaml").write_text("mini_eval: {cases_per_type: 999}", encoding="utf-8")
    child = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(mini_eval.subprocess, "run", child)
    assert mini_eval.main(["--workspace", str(workspace), "--dry-run", "--"]) == 0
    parsed = mini_eval.run.parse_args(child.call_args.args[0][2:])
    assert not hasattr(parsed,"params")
    assert parsed.dry_run
    assert len(parsed.targets) == 5


@pytest.mark.parametrize("args", [
    ["official"], ["--groups", "official"], ["--cases", "official_01"], ["--groups="], ["--cases="],
])
def test_target_overrides_never_launch_child(monkeypatch, args):
    child = Mock()
    monkeypatch.setattr(mini_eval.subprocess, "run", child)
    assert mini_eval.main(args) == 1
    child.assert_not_called()


def test_unknown_arguments_never_launch_child(monkeypatch):
    child = Mock()
    monkeypatch.setattr(mini_eval.subprocess, "run", child)
    with pytest.raises(SystemExit) as exc:
        mini_eval.main(["--all-cases"])
    assert exc.value.code == 2
    child.assert_not_called()


@pytest.mark.parametrize("config", [{"cases_per_type": True}, {"per_type": {"missing": 1}}])
def test_invalid_config_never_launches_child(workspace, tmp_path, monkeypatch, config):
    child = Mock()
    monkeypatch.setattr(mini_eval.subprocess, "run", child)
    params = _params(tmp_path, config)
    assert mini_eval.main(["--workspace", str(workspace), "--mini-per-type", "missing=1"]) == 1
    child.assert_not_called()


def test_missing_params_never_launches_child(workspace, tmp_path, monkeypatch):
    child = Mock()
    monkeypatch.setattr(mini_eval.subprocess, "run", child)
    with pytest.raises(SystemExit):
        mini_eval.main(["--workspace",str(workspace),"--params",str(tmp_path / "missing.yaml")])
    child.assert_not_called()


@pytest.mark.parametrize("entry", [["eval/mini_eval.py"], ["-m", "eval.mini_eval"]])
def test_entrypoints_dry_run_only_selected_cases(workspace, tmp_path, entry):
    params = _params(tmp_path, {"per_type": {"official": 2}})
    result = subprocess.run(
        [sys.executable, *entry, "--workspace", str(workspace), "--mini-per-type", "official=2", "--dry-run"],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "共选择 5 个 case" in result.stdout
    assert "类型 official: 2 个 case" in result.stdout
    assert "类型 standard: 1 个 case" in result.stdout
    assert "official_03" not in result.stdout
    assert "standard_02" not in result.stdout


def test_mini_forwards_extraction_overrides(workspace, tmp_path, monkeypatch):
    params = _params(tmp_path)
    child = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(mini_eval.subprocess, "run", child)
    assert mini_eval.main(["--workspace", str(workspace), "--mini-per-type", "official=2",
                          "--extract-version", "v3.1", "--extract-provider", "siliconflow",
                          "--extract-request-timeout", "300", "--chunk-overlap", "80"]) == 0
    command = child.call_args.args[0]
    for flag, value in [("--extract-version", "v3.1"), ("--extract-provider", "siliconflow"),
                        ("--extract-request-timeout", "300"), ("--chunk-overlap", "80")]:
        assert command[command.index(flag) + 1] == value
