#!/usr/bin/env python3
"""Keyboard editor for eval/run.py. Saving or opening never starts an evaluation."""
from __future__ import annotations

import argparse
import ast
from copy import deepcopy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from docker import terminal_ui as screen
from docker.configure import read_key

SECTIONS = ("数据集", "抽取", "裁判", "门禁", "执行", "全部")


def definitions(root: Path = ROOT) -> tuple[argparse.ArgumentParser, dict]:
    """Read the real CLI definition without importing run.py or loading credentials."""
    tree = ast.parse((root / "eval/run.py").read_text(encoding="utf-8"))
    function = deepcopy(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "parse_args"))
    function.body[-1] = ast.Return(value=ast.Name(id="parser", ctx=ast.Load()))
    helper = deepcopy(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "add_runtime_arguments"))
    retention = next(n.value for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DEFAULT_RUNS_RETENTION_DAYS" for t in n.targets))
    space = {"argparse": argparse, "DEFAULT_WORKSPACE": root / "test_data/eval",
             "DEFAULT_RUNS_RETENTION_DAYS": ast.literal_eval(retention)}
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), helper, function], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "eval/run.py:parse_args", "exec"), space)
    def invalid(message):
        raise ValueError("CLI 参数值不合法，请检查类型和可选值")
    node = next(n.value for n in tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "DEFAULT_PARAMS")
    defaults = eval(compile(ast.Expression(node), "eval/run.py:DEFAULT_PARAMS", "eval"), space)
    space["DEFAULT_PARAMS"] = defaults
    parser = space["parse_args"]([])
    parser.error = invalid
    return parser, defaults


def category(name: str) -> str:
    if name in {"targets", "workspace", "groups", "cases"}: return "数据集"
    if name.startswith(("extract_", "chunk_")): return "抽取"
    if name.startswith("judge_") or name == "match_mode": return "裁判"
    if name.startswith(("min_", "max_")) or name in {"baseline", "require_scored", "fail_on_empty"}: return "门禁"
    return "执行"


def get_path(data: dict, key: str):
    value = data
    for part in key.split("."):
        if not isinstance(value, dict): return None
        value = value.get(part)
    return value


def put_path(data: dict, key: str, value) -> None:
    parts = key.split(".")
    for part in parts[:-1]: data = data.setdefault(part, {})
    data[parts[-1]] = value


def merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in override.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else deepcopy(value)
    return result


def no_credentials(data) -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            if re.search(r"(^|_)(api_key|password|secret|authorization|access_token)$", str(key), re.I):
                raise ValueError("配置不能携带密钥；请改用环境变量，原参数文件未修改")
            no_credentials(value)
    elif isinstance(data, list):
        for value in data: no_credentials(value)
    elif isinstance(data, str) and "://" in data:
        url = urlsplit(data)
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("服务 URL 不允许用户名、密码、查询参数或片段")


def display(value) -> str:
    if value is None: return "继承默认"
    if isinstance(value, bool): return "[开]" if value else "[关]"
    if isinstance(value, list): return " ".join(value) or "全部"
    return str(value) or "全部 / 未覆盖"


class Configuration:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()
        self.parser, self.default_params = definitions(self.root)
        self.actions = {a.dest: a for a in self.parser._actions if a.dest != "help"}
        self.cli = {}

    def rows(self) -> list[dict]:
        rows = []
        for key, action in self.actions.items():
            value = self.cli.get(key, action.default)
            effective = value
            if value is None:
                if key == "mode": effective = ["pipeline"]
                elif key == "judge_model": effective = "deepseek-flash / 沿用应用裁判设置"
                elif key == "match_mode": effective = "llm"
            rows.append(dict(key=key, kind="cli", name=action.option_strings[0] if action.option_strings else "评测目录 targets",
                             value=value, effective=effective, default=action.default, description=action.help or "", section=category(key)))
        return rows

    def edit(self, row: dict, text: str) -> None:
        key = row["key"]
        if not text.strip(): self.cli.pop(key, None); return
        action = self.actions[key]
        if isinstance(action, (argparse._StoreTrueAction, argparse.BooleanOptionalAction)):
            if text.lower() not in {"true", "false"}: raise ValueError("开关请输入 true 或 false")
            value = text.lower() == "true"
        elif action.nargs in {"+", "*"}:
            value = shlex.split(text, posix=True)
            if action.choices and any(v not in action.choices for v in value): raise ValueError("不支持的多选值")
        else:
            value = action.type(text) if action.type else text
            if action.choices and value not in action.choices: raise ValueError("不支持的可选值")
        no_credentials(value)
        self.cli[key] = value

    def arguments(self) -> list[str]:
        args = []
        for key, value in self.cli.items():
            if value is None or key == "targets": continue
            action = self.actions[key]
            if isinstance(action, argparse.BooleanOptionalAction): args.append(action.option_strings[0 if value else 1])
            elif isinstance(action, argparse._StoreTrueAction):
                if value: args.append(action.option_strings[0])
            else:
                args.append(action.option_strings[0])
                args.extend(str(v) for v in value) if isinstance(value, list) else args.append(str(value))
        if self.cli.get("targets"): args += ["--", *self.cli["targets"]]
        return args

    def validate(self) -> None:
        if set(self.cli) - self.actions.keys(): raise ValueError("当前版本未知参数")
        no_credentials(self.cli)
        for key, value in self.cli.items():
            action = self.actions[key]
            if isinstance(action, (argparse.BooleanOptionalAction, argparse._StoreTrueAction)) and type(value) is not bool: raise ValueError("开关必须为布尔值")
            if action.nargs in {"+", "*"} and (not isinstance(value, list) or any(not isinstance(v, str) for v in value)): raise ValueError("多选必须为列表")
        args = self.parser.parse_args(self.arguments())
        if args.targets and (args.groups or args.cases): raise ValueError("目录与 groups/cases 不能同时设置")
        modes = args.mode or ["pipeline"]
        if args.system and "pipeline" not in modes: modes = [*modes, "pipeline"]
        if args.from_run and not set(modes) & {"extract", "chunk-extract"}: raise ValueError("复用切片需 extract/chunk-extract")
        if "extract" in modes and not args.from_run: raise ValueError("extract 必须指定 from-run")
        if args.reuse_extract_from and (args.stage != "backend" or modes != ["extract"] or not args.from_run): raise ValueError("复用抽取需 backend+extract+from-run")
        for key in ("min_f1", "min_relation_f1", "max_f1_drop"):
            value = getattr(args,key)
            if not math.isfinite(value) or not 0 <= value <= 1: raise ValueError("F1 阈值必须在 0–1")
        for key in ("poll_interval_seconds", "judge_timeout_seconds", "judge_max_tokens", "judge_batch_size", "judge_golden_batch_size", "judge_max_concurrency", "max_case_seconds"):
            value = getattr(args,key)
            if value is not None and (not math.isfinite(value) or value <= 0): raise ValueError("预算与并发必须为有限正数")
        if not math.isfinite(args.poll_timeout_seconds) or args.poll_timeout_seconds < 0: raise ValueError("轮询超时必须为有限非负数")
        if args.chunk_size is not None and args.chunk_overlap is not None and args.chunk_size <= args.chunk_overlap: raise ValueError("size 必须大于 overlap")
        if args.judge_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",args.judge_key_env): raise ValueError("密钥只接受环境变量名")

    def command(self) -> list[str]: return [sys.executable, str(self.root / "eval/run.py"), *self.arguments()]

    def preview(self) -> str:
        self.validate()
        return subprocess.list2cmdline(self.command()) if os.name == "nt" else shlex.join(self.command())

    def execute(self, confirmation: str) -> int | None:
        if confirmation != "run": return None
        self.validate()
        return subprocess.run(self.command(),cwd=self.root,check=False).returncode


def render(config: Configuration, rows: list[dict], index: int, section: str, notice: str, columns: int, lines: int, dirty=False) -> str:
    cells = max(1, columns - 1)
    color = "NO_COLOR" not in os.environ
    def line(text, style="normal"):
        return (screen.STYLES[style] if color else "") + screen.fit(text, cells) + (screen.RESET if color else "")
    if columns < 64 or lines < 18: return line("请扩大终端至 64 列 × 18 行；q 退出")
    output = [line(" QASystem / 评测配置", "title"), line(" [评测参数] " + ("● 临时修改" if dirty else "○ 本轮默认") + "   仅本轮临时配置，不加载/保存文件", "muted"),
              line(" " + "  ".join(f"[{s}]" if s == section else s for s in SECTIONS), "title")]
    height = max(1, lines - 11)
    start = max(0, min(index - height + 1, len(rows) - height))
    inner = cells - 2
    output += [line("┌" + "─" * inner + "┐", "muted"), line("│" + screen.pair(" 参数 / 键", "编辑值（未设置时继承默认） ", inner) + "│", "title")]
    for i in range(height):
        pos = start + i
        row = rows[pos] if pos < len(rows) else None
        body = screen.pair((" › " if pos == index else "   ") + row["name"], display(row["effective"]) + " " + ("[覆写]" if row["key"] in config.cli else "[继承]"), inner) if row else " " * inner
        output.append(line("│" + body + "│", "selected" if row and pos == index else "normal"))
    output.append(line("└" + "─" * inner + "┘", "muted"))
    row = rows[index] if rows else None
    output += [line(" " + (row["description"] if row else "无配置项"), "muted"),
               line(" " + (f"CLI 默认: {display(row['default'])}；参数来自当前命令，不读取评测配置文件" if row else ""), "muted"),
               line(" " + notice, "warning"), line(" ↑↓/PgUp/PgDn 移动  ←→/Tab 分区  Enter 编辑  Space 开关  d 继承默认", "muted"),
               line(" p 命令预览  v 只扫描数据集  r 明确执行评测  q 退出", "title")]
    return "\n".join(output[:lines])


def ui(config: Configuration) -> None:
    index, selected, notice = 0, 0, "本轮临时计划；退出后丢弃，不读取或保存评测配置。"
    original = json.dumps(config.cli, sort_keys=True)
    print("\x1b[?1049h", end="", flush=True)
    try:
        while True:
            rows = [r for r in config.rows() if SECTIONS[selected] == "全部" or r["section"] == SECTIONS[selected]]
            index = max(0, min(index, len(rows) - 1))
            columns, lines = shutil.get_terminal_size((100, 28))
            dirty = original != json.dumps(config.cli, sort_keys=True)
            print("\x1b[H" + render(config, rows, index, SECTIONS[selected], notice, columns, lines, dirty) + "\x1b[J", end="", flush=True)
            key = read_key(); row = rows[index] if rows else None
            try:
                if key in {"q", "escape", "\x03"}: return
                if key in {"down", "j"}: index = min(index + 1, len(rows) - 1)
                elif key in {"up", "k"}: index = max(0, index - 1)
                elif key in {"left", "right", "\t"}: selected = (selected + (-1 if key == "left" else 1)) % len(SECTIONS); index = 0
                elif key in {"pageup", "pagedown"}: index = max(0, min(len(rows) - 1, index + (1 if key == "pagedown" else -1) * max(1, lines - 11)))
                elif key == "d" and row: config.cli.pop(row["key"], None)
                elif key == " " and row and (isinstance(row["effective"], bool) or isinstance(config.actions.get(row["key"]), (argparse.BooleanOptionalAction, argparse._StoreTrueAction))):
                    config.cli[row["key"]] = not bool(row["effective"])
                elif key in {"\r", "\n"} and row:
                    choices = config.actions[row["key"]].choices if row["kind"] == "cli" else None
                    print("\n" + row["description"] + ("\n可选: " + ", ".join(map(str, choices)) if choices else ""))
                    config.edit(row, input("新值（空值恢复继承；多项用空格分隔、路径有空格用引号）: "))
                elif key == "p": print("\n" + config.preview()); input("这是命令预览，不会执行。回车返回。")
                elif key == "v":
                    config.validate()
                    command = config.command(); command.insert(2, "--dry-run")
                    subprocess.run(command, cwd=config.root, check=False)
                    input("只扫描数据集，不调 Backend / judge。回车返回。")
                elif key == "r":
                    print("\n" + config.preview() + "\n将真实调用 Backend 和 judge，可能产生 API 费用。")
                    code = config.execute(input("输入 run 执行，其他输入取消: "))
                    if code is not None: notice = f"评测退出码 {code}；报告位于 tmp/eval/runs/"; input("回车返回配置界面。")
            except (ValueError, OSError, RuntimeError): notice = "配置值或模式组合不合法；请检查当前项、模式组合与路径。未显示原始异常中的配置值。"
    finally: print("\x1b[0m\x1b[?25h\x1b[?1049l", end="", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true", help="校验并显示最终命令，不执行、不保存")
    args = parser.parse_args(argv)
    try:
        config = Configuration()
        if args.preview: print(config.preview()); return 0
        if not sys.stdin.isatty(): raise ValueError("需要交互终端；SSH 请使用 -t")
        ui(config); return 0
    except (ValueError, OSError, KeyboardInterrupt, EOFError):
        print("评测配置入口未完成；请检查临时参数与交互终端。未启动评测。", file=sys.stderr)
        return 1


if __name__ == "__main__": raise SystemExit(main())
