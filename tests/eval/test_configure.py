from pathlib import Path
import json
from unittest.mock import patch

import pytest

from eval.configure import Configuration, ROOT, definitions, render
from docker.terminal_ui import width, ANSI


@pytest.fixture
def config(tmp_path):
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval/run.py").write_text((ROOT / "eval/run.py").read_text(encoding="utf-8"), encoding="utf-8")
    return Configuration(root=tmp_path)


def row(config, key):
    return next(r for r in config.rows() if r["key"] == key)


def test_actual_parser_defaults_and_no_start(config):
    from eval.run import parse_args
    real = vars(parse_args([]))
    actual = vars(config.parser.parse_args([]))
    real.pop("workspace"); actual.pop("workspace")
    assert actual == real
    assert config.arguments() == []
    assert row(config, "mode")["effective"] == ["pipeline"]
    assert row(config, "match_mode")["effective"] == "llm"
    assert not hasattr(config,"profile")


def test_choices_types_and_boolean(config):
    config.edit(row(config, "extract_version"), "v3.1")
    config.edit(row(config, "mode"), "chunk-extract pipeline")
    config.edit(row(config, "fail_on_empty"), "false")
    assert "--no-fail-on-empty" in config.arguments()
    assert config.cli["mode"] == ["chunk-extract", "pipeline"]
    with pytest.raises(ValueError): config.edit(row(config, "extract_version"), "v9")
    with pytest.raises(ValueError): config.edit(row(config, "case_attempts"), "5")
    with pytest.raises(ValueError): config.edit(row(config, "dry_run"), "maybe")
    config.edit(row(config, "extract_version"), "")
    assert "extract_version" not in config.cli


def test_open_and_preview_never_read_or_write_profiles(config):
    (config.root / 'eval/params.yaml').write_text('eval: {judge_model: ignored}',encoding='utf-8')
    (config.root / 'eval/.env').write_text('QASYSTEM_JUDGE_MODEL=ignored',encoding='utf-8')
    with patch('pathlib.Path.write_text') as write, patch('eval.configure.subprocess.run') as execute:
        config.preview()
        assert config.cli == {}
        write.assert_not_called(); execute.assert_not_called()


def test_explicit_execute_list_no_shell(config):
    with patch('eval.configure.subprocess.run') as execute:
        assert config.execute('yes') is None
        execute.assert_not_called()
        execute.return_value.returncode=4
        assert config.execute('run')==4
        execute.assert_called_once_with(config.command(),cwd=config.root,check=False)


@pytest.mark.parametrize("cli", [
    {"targets": ["case"], "groups": "official"},
    {"mode": ["extract"]},
    {"mode": ["pipeline"], "from_run": "run"},
    {"reuse_extract_from": "run"},
    {"min_f1": -0.1}, {"min_f1": float("nan")},
    {"max_case_seconds": float("inf")},
    {"chunk_size": 50, "chunk_overlap": 100},
    {"judge_key_env": "literal-key-is-not-an-env-name!"},
    {"judge_base_url": "https://user:secret@example.org/v1"},
    {"judge_base_url": "https://example.org/v1?api_key=secret"},
    {"dry_run": "false"}, {"mode": "pipeline"},
    {"extract_version": "missing"},
])
def test_conflicts_rejected(config, cli):
    config.cli.update(cli)
    with pytest.raises(ValueError): config.validate()
    assert not hasattr(config,"profile")


def test_preview_shell_quotes_and_never_executes(config):
    config.cli["targets"] = ["test_data/含 空格/one; echo hello"]
    with patch("eval.configure.subprocess.run") as execute:
        text = config.preview()
        assert "含 空格" in text
        assert config.command()[-1] == config.cli["targets"][0]
        execute.assert_not_called()


@pytest.mark.parametrize("columns,lines", [(80, 24), (120, 30), (180, 40)])
def test_terminal_frame_fits_cjk_and_has_flags(config, columns, lines):
    rows = config.rows()
    frame = render(config, rows, 0, "数据集", "已保存，未启动评测", columns, lines)
    assert "[评测参数]" in frame
    assert "[继承]" in frame
    assert len(frame.splitlines()) <= lines
    assert all(width(ANSI.sub("", line)) <= columns - 1 for line in frame.splitlines())


def test_targets_after_separator(config):
    config.cli.update(targets=["-odd-path"], dry_run=True)
    assert config.arguments() == ["--dry-run", "--", "-odd-path"]
