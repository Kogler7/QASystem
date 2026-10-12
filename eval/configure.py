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
PARAM_FIELDS = {
    "backend_url": ("执行", "Backend 地址；QASYSTEM_BACKEND_URL 环境变量优先。"),
    "mcp_url": ("执行", "插件 MCP 地址；空值使用 Backend 的 /mcp。"),
    "poll_interval_seconds": ("执行", "异步任务轮询间隔，秒。"),
    "poll_timeout_seconds": ("执行", "异步任务最长等待时间，秒。"),
    "eval.judge_timeout_seconds": ("裁判", "单次裁判请求超时，秒。"),
    "eval.judge_max_tokens": ("裁判", "裁判单次输出 token 上限。"),
    "eval.judge_batch_size": ("裁判", "每次裁判输入的预测条数。"),
    "eval.judge_golden_batch_size": ("裁判", "每次裁判输入的 golden 条数。"),
    "eval.judge_max_concurrency": ("裁判", "裁判并发 worker 数。"),
    "eval.judge_response_format": ("裁判", "auto / json_object / json_schema，沿用原评测逻辑。"),
    "eval.judge_disable_thinking": ("裁判", "是否关闭裁判思考输出，沿用原评测逻辑。"),
}


def definitions(root: Path = ROOT) -> tuple[argparse.ArgumentParser, dict]:
    """Read the real CLI definition without importing run.py or loading credentials."""
    tree = ast.parse((root / "eval/run.py").read_text(encoding="utf-8"))
    function = deepcopy(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "parse_args"))
    function.body[-1] = ast.Return(value=ast.Name(id="parser", ctx=ast.Load()))
    retention = next(n.value for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DEFAULT_RUNS_RETENTION_DAYS" for t in n.targets))
    space = {"argparse": argparse, "DEFAULT_WORKSPACE": root / "test_data/eval",
             "DEFAULT_RUNS_RETENTION_DAYS": ast.literal_eval(retention)}
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "eval/run.py:parse_args", "exec"), space)
    parser = space["parse_args"]([])
    def invalid(message):
        raise ValueError("CLI 参数值不合法，请检查类型和可选值")
    parser.error = invalid
    node = next(n.value for n in tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "DEFAULT_PARAMS")
    defaults = eval(compile(ast.Expression(node), "eval/run.py:DEFAULT_PARAMS", "eval"), space)
    return parser, defaults


def category(name: str) -> str:
    if name in {"targets", "workspace", "params", "groups", "cases"}: return "数据集"
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
    def __init__(self, profile: Path, root: Path = ROOT):
        self.root, self.profile = root.resolve(), profile.resolve()
        if not self.profile.is_relative_to(self.root / "tmp"):
            raise ValueError("配置入口的输出必须放在仓库 tmp/ 下")
        self.parser, self.default_params = definitions(self.root)
        self.actions = {a.dest: a for a in self.parser._actions if a.dest != "help"}
        self.cli, self.params = {}, {}
        self.original = self.profile.read_bytes() if self.profile.exists() else None
        if self.original:
            payload = json.loads(self.original)
            if not isinstance(payload, dict) or set(payload) - {"version", "cli", "params"} or payload.get("version") != 1:
                raise ValueError("不支持的评测配置文件格式")
            self.cli, self.params = payload.get("cli", {}), payload.get("params", {})
            if not isinstance(self.cli, dict) or not isinstance(self.params, dict): raise ValueError("配置字段必须为对象")
        self.generated = self.profile.with_name(self.profile.stem + ".params.yaml")
        self.backup = None
        if self.original:
            self.backup = self.profile.with_name(self.profile.name + ".backup-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            self.backup.write_bytes(self.original)
        self.validate()

    def source_params(self) -> tuple[Path, dict]:
        workspace = Path(self.cli.get("workspace", self.actions["workspace"].default)).expanduser()
        if not workspace.is_absolute(): workspace = self.root / workspace
        explicit = self.cli.get("params")
        path = Path(explicit).expanduser() if explicit else next((p for p in (workspace / "params.yaml", self.root / "eval/params.yaml") if p.is_file()), self.root / "eval/params.yaml")
        if not path.is_absolute(): path = self.root / path
        if path.exists():
            try: import yaml
            except ImportError as exc: raise ValueError("需要现有评测依赖 PyYAML") from exc
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict): raise ValueError("参数 YAML 必须为对象")
            no_credentials(raw)
        else:
            if explicit: raise FileNotFoundError("指定的参数文件不存在")
            raw = {}
        return path, merge(self.default_params, raw)

    def rows(self) -> list[dict]:
        try: _, inherited = self.source_params()
        except FileNotFoundError: inherited = deepcopy(self.default_params)
        public_env = self.public_environment()
        rows = []
        for key, action in self.actions.items():
            value = self.cli.get(key, action.default)
            effective = value
            if value is None:
                if key == "mode": effective = ["pipeline"]
                elif key.startswith("judge_"): effective = public_env.get("QASYSTEM_" + key.upper()) or inherited.get("eval", {}).get(key)
                elif key == "match_mode": effective = inherited["eval"].get(key)
                elif key == "runs_retention_days": effective = inherited.get(key)
            rows.append(dict(key=key, kind="cli", name=action.option_strings[0] if action.option_strings else "评测目录 targets",
                             value=value, effective=effective, default=action.default, description=action.help or "", section=category(key)))
        for key, (section, description) in PARAM_FIELDS.items():
            value = self.params.get(key, get_path(inherited, key))
            if key in {"backend_url", "mcp_url"} and public_env.get("QASYSTEM_" + key.upper()):
                description += " 当前进程环境变量优先，填写本项不会覆盖它。"
            rows.append(dict(key=key, kind="params", name=key, value=value, effective=value,
                             default=get_path(inherited, key), description=description, section=section))
        return rows

    def public_environment(self) -> dict:
        names = {"QASYSTEM_BACKEND_URL", "QASYSTEM_MCP_URL", "QASYSTEM_JUDGE_MODEL", "QASYSTEM_JUDGE_BASE_URL", "QASYSTEM_JUDGE_ADAPTER"}
        location_names = {"QASYSTEM_RUNTIME_SETTINGS_FILE", "QASYSTEM_RUNTIME_CONFIG_DIR"}
        values_env = {k: os.environ[k] for k in names | location_names if os.environ.get(k)}
        from docker.setup_llm import read_env
        for path in (self.root / "eval/.env", self.root / ".env"):
            if not path.exists(): continue
            values = read_env(path.read_text(encoding="utf-8"))
            for key in names | location_names:
                if key not in values_env and values.get(key): values_env[key] = values[key]
        result = {k: v for k, v in values_env.items() if k in names}
        settings_path = Path(values_env.get("QASYSTEM_RUNTIME_SETTINGS_FILE") or (
            "/app/runtime-config/settings.json" if Path("/app/runtime-config").is_dir() else
            str(Path(values_env.get("QASYSTEM_RUNTIME_CONFIG_DIR", "./runtime-config")) / "settings.json")))
        if not settings_path.is_absolute(): settings_path = self.root / settings_path
        if settings_path.exists():
            hot = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(hot, dict) or any(not isinstance(v, str) for v in hot.values()): raise ValueError("运行时裁判配置无效")
            result.update({k: v for k, v in hot.items() if k.startswith("QASYSTEM_JUDGE_") and k in names})
        no_credentials(result)
        return result

    def edit(self, row: dict, text: str) -> None:
        key = row["key"]
        target = self.cli if row["kind"] == "cli" else self.params
        if not text.strip(): target.pop(key, None); return
        if row["kind"] == "cli":
            action = self.actions[key]
            if isinstance(action, (argparse._StoreTrueAction, argparse.BooleanOptionalAction)):
                if text.lower() not in {"true", "false"}: raise ValueError("开关请输入 true 或 false；留空继承默认")
                value = text.lower() == "true"
            elif action.nargs in {"+", "*"}:
                value = shlex.split(text, posix=True)
                if not value: raise ValueError("至少输入一项")
                if action.choices and any(v not in action.choices for v in value): raise ValueError("可选值: " + ", ".join(map(str, action.choices)))
            else:
                value = action.type(text) if action.type else text
                if action.choices and value not in action.choices: raise ValueError("可选值: " + ", ".join(map(str, action.choices)))
        else:
            inherited = row["default"]
            if isinstance(inherited, bool):
                if text.lower() not in {"true", "false"}: raise ValueError("开关请输入 true 或 false")
                value = text.lower() == "true"
            elif isinstance(inherited, int): value = int(text)
            elif isinstance(inherited, float): value = float(text)
            else: value = text
        no_credentials(value)
        target[key] = value

    def arguments(self) -> list[str]:
        args = []
        for key, value in self.cli.items():
            if value is None: continue
            action = self.actions[key]
            if key == "targets": continue
            if isinstance(action, argparse.BooleanOptionalAction): args.append(action.option_strings[0 if value else 1])
            elif isinstance(action, argparse._StoreTrueAction):
                if value: args.append(action.option_strings[0])
            else:
                args.append(action.option_strings[0])
                args.extend(str(v) for v in value) if isinstance(value, list) else args.append(str(value))
        if self.params:
            # Replace the source params switch; emitted file retains source options unchanged.
            if "--params" in args:
                pos = args.index("--params"); del args[pos:pos + 2]
            args += ["--params", str(self.generated)]
        if self.cli.get("targets"): args += ["--", *self.cli["targets"]]
        return args

    def validate(self) -> None:
        if set(self.cli) - self.actions.keys() or set(self.params) - PARAM_FIELDS.keys(): raise ValueError("配置包含当前版本未知参数")
        no_credentials([self.cli, self.params])
        for key, value in self.cli.items():
            action = self.actions[key]
            if isinstance(action, (argparse.BooleanOptionalAction, argparse._StoreTrueAction)) and type(value) is not bool: raise ValueError("开关必须为布尔值")
            if action.nargs in {"+", "*"} and (not isinstance(value, list) or any(not isinstance(v, str) for v in value)): raise ValueError("多选参数必须为字符串列表")
        args = self.parser.parse_args(self.arguments())
        modes = args.mode or ["pipeline"]
        if args.system and "pipeline" not in modes: modes = [*modes, "pipeline"]
        if args.targets and (args.groups or args.cases): raise ValueError("评测目录与 groups/cases 不能同时设置")
        if args.from_run and not set(modes) & {"extract", "chunk-extract"}: raise ValueError("复用切片需选择 extract 或 chunk-extract")
        if "extract" in modes and not args.from_run: raise ValueError("extract 模式必须指定 from-run 固定切片")
        if args.reuse_extract_from and (args.stage != "backend" or modes != ["extract"] or not args.from_run): raise ValueError("复用抽取需 backend + extract + from-run")
        extract_changes = any(self.cli.get(key) is not None for key in ("extract_version", "extract_provider", "extract_request_timeout", "extract_document_timeout"))
        if extract_changes and args.stage != "backend": raise ValueError("抽取配置覆盖仅支持 backend 阶段")
        if args.reuse_extract_from and (extract_changes or args.extract_model is not None): raise ValueError("覆盖抽取配置时不能复用旧预测")
        for key in ("min_f1", "min_relation_f1", "max_f1_drop"):
            value = getattr(args, key)
            if not math.isfinite(value) or not 0 <= value <= 1: raise ValueError(f"{key} 必须在 0–1 之间")
        if not math.isfinite(args.max_latency_increase) or args.max_latency_increase < 0: raise ValueError("耗时增幅必须为有限非负数")
        for key in ("max_case_seconds", "extract_request_timeout", "extract_document_timeout", "chunk_size"):
            value = getattr(args, key)
            if value is not None and (not math.isfinite(value) or value <= 0): raise ValueError(f"{key} 必须为有限正数")
        if args.chunk_overlap is not None and args.chunk_overlap < 0: raise ValueError("overlap 不能为负")
        if args.chunk_size is not None and args.chunk_overlap is not None and args.chunk_size <= args.chunk_overlap: raise ValueError("chunk-size 必须大于 overlap")
        if args.runs_retention_days is not None and not math.isfinite(args.runs_retention_days): raise ValueError("保留天数必须为有限数")
        if args.max_stage_seconds:
            for entry in args.max_stage_seconds.split(","):
                parts = entry.strip().split("=")
                if not entry.strip(): continue
                if len(parts) != 2 or parts[0].strip() not in {"parse", "chunk", "extract"}: raise ValueError("阶段预算格式为 parse=300,chunk=120,extract=900")
                number = float(parts[1])
                if not math.isfinite(number) or number <= 0: raise ValueError("阶段预算必须为有限正数")
        for key, value in self.params.items():
            if key.endswith(("seconds", "tokens", "size", "concurrency")) and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0): raise ValueError(f"{key} 必须为有限正数")
            if key == "eval.judge_response_format" and value not in {"auto", "json_object", "json_schema"}: raise ValueError("不支持的裁判输出格式")
            if key == "eval.judge_disable_thinking" and type(value) is not bool: raise ValueError("关闭思考必须为布尔值")
        for key, value in [(k, v) for k, v in self.params.items() if k.endswith("url") and v] + [(k, v) for k, v in self.cli.items() if k.endswith("url") and v]:
            if urlsplit(value).scheme not in {"http", "https"} or not urlsplit(value).hostname: raise ValueError(f"{key} 必须为 HTTP(S) 地址")
        if args.judge_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.judge_key_env): raise ValueError("密钥字段只接受环境变量名称")
        self.source_params()

    def save(self) -> None:
        self.validate()
        if (self.profile.read_bytes() if self.profile.exists() else None) != self.original: raise ValueError("配置被其他进程修改，请重新打开")
        self.profile.parent.mkdir(parents=True, exist_ok=True)
        if self.params:
            _, payload = self.source_params()
            for key, value in self.params.items(): put_path(payload, key, value)
            self._write(self.generated, payload)
        self._write(self.profile, dict(version=1, cli=self.cli, params=self.params))
        self.original = self.profile.read_bytes()

    @staticmethod
    def _write(path: Path, payload: dict) -> None:
        temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def command(self) -> list[str]: return [sys.executable, str(self.root / "eval/run.py"), *self.arguments()]

    def preview(self) -> str:
        self.validate()
        return subprocess.list2cmdline(self.command()) if os.name == "nt" else shlex.join(self.command())

    def execute(self, confirmation: str) -> int | None:
        if confirmation != "run": return None
        self.save()
        return subprocess.run(self.command(), cwd=self.root, check=False).returncode


def render(config: Configuration, rows: list[dict], index: int, section: str, notice: str, columns: int, lines: int, dirty=False) -> str:
    cells = max(1, columns - 1)
    color = "NO_COLOR" not in os.environ
    def line(text, style="normal"):
        return (screen.STYLES[style] if color else "") + screen.fit(text, cells) + (screen.RESET if color else "")
    if columns < 64 or lines < 18: return line("请扩大终端至 64 列 × 18 行；q 退出")
    output = [line(" QASystem / 评测配置", "title"), line(" [评测参数] " + ("● 未保存修改" if dirty else "○ 已载入配置") + "   打开/保存不会发起评测", "muted"),
              line(" " + "  ".join(f"[{s}]" if s == section else s for s in SECTIONS), "title")]
    height = max(1, lines - 11)
    start = max(0, min(index - height + 1, len(rows) - height))
    inner = cells - 2
    output += [line("┌" + "─" * inner + "┐", "muted"), line("│" + screen.pair(" 参数 / 键", "编辑值（未设置时继承默认） ", inner) + "│", "title")]
    for i in range(height):
        pos = start + i
        row = rows[pos] if pos < len(rows) else None
        body = screen.pair((" › " if pos == index else "   ") + row["name"], display(row["effective"]) + " " + ("[覆写]" if row["key"] in (config.cli if row["kind"] == "cli" else config.params) else "[继承]"), inner) if row else " " * inner
        output.append(line("│" + body + "│", "selected" if row and pos == index else "normal"))
    output.append(line("└" + "─" * inner + "┘", "muted"))
    row = rows[index] if rows else None
    output += [line(" " + (row["description"] if row else "无配置项"), "muted"),
               line(" " + (f"CLI 默认: {display(row['default'])}；当前继承值受 params.yaml 和环境变量影响" if row else ""), "muted"),
               line(" " + notice, "warning"), line(" ↑↓/PgUp/PgDn 移动  ←→/Tab 分区  Enter 编辑  Space 开关  d 继承默认", "muted"),
               line(" s 保存  p 命令预览  v 只扫描数据集  r 明确执行评测  q 退出", "title")]
    return "\n".join(output[:lines])


def ui(config: Configuration) -> None:
    index, selected, notice = 0, 0, "全部默认值沿用原评测入口；不更改 Backend 配置。"
    original = json.dumps([config.cli, config.params], sort_keys=True)
    print("\x1b[?1049h", end="", flush=True)
    try:
        while True:
            rows = [r for r in config.rows() if SECTIONS[selected] == "全部" or r["section"] == SECTIONS[selected]]
            index = max(0, min(index, len(rows) - 1))
            columns, lines = shutil.get_terminal_size((100, 28))
            dirty = original != json.dumps([config.cli, config.params], sort_keys=True)
            print("\x1b[H" + render(config, rows, index, SECTIONS[selected], notice, columns, lines, dirty) + "\x1b[J", end="", flush=True)
            key = read_key(); row = rows[index] if rows else None
            try:
                if key in {"q", "escape", "\x03"}: return
                if key in {"down", "j"}: index = min(index + 1, len(rows) - 1)
                elif key in {"up", "k"}: index = max(0, index - 1)
                elif key in {"left", "right", "\t"}: selected = (selected + (-1 if key == "left" else 1)) % len(SECTIONS); index = 0
                elif key in {"pageup", "pagedown"}: index = max(0, min(len(rows) - 1, index + (1 if key == "pagedown" else -1) * max(1, lines - 11)))
                elif key == "d" and row: (config.cli if row["kind"] == "cli" else config.params).pop(row["key"], None)
                elif key == " " and row and (isinstance(row["effective"], bool) or isinstance(config.actions.get(row["key"]), (argparse.BooleanOptionalAction, argparse._StoreTrueAction))):
                    (config.cli if row["kind"] == "cli" else config.params)[row["key"]] = not bool(row["effective"])
                elif key in {"\r", "\n"} and row:
                    choices = config.actions[row["key"]].choices if row["kind"] == "cli" else None
                    print("\n" + row["description"] + ("\n可选: " + ", ".join(map(str, choices)) if choices else ""))
                    config.edit(row, input("新值（空值恢复继承；多项用空格分隔、路径有空格用引号）: "))
                elif key == "s": config.save(); original = json.dumps([config.cli, config.params], sort_keys=True); notice = "已保存到 " + str(config.profile) + "；未启动评测"
                elif key == "p": print("\n" + config.preview()); input("这是命令预览，不会执行。回车返回。")
                elif key == "v":
                    config.save()
                    command = config.command(); command.insert(2, "--dry-run")
                    subprocess.run(command, cwd=config.root, check=False)
                    input("只扫描数据集，不调 Backend / judge。回车返回。")
                elif key == "r":
                    print("\n" + config.preview() + "\n将真实调用 Backend 和 judge，可能产生 API 费用。")
                    code = config.execute(input("输入 run 执行，其他输入取消: "))
                    if code is not None: notice = f"评测退出码 {code}；报告位于 tmp/eval/runs/"; input("回车返回配置界面。")
            except (ValueError, OSError, RuntimeError): notice = "配置不合法或文件冲突；请检查当前项、模式组合与路径。未显示原始异常中的配置值。"
    finally: print("\x1b[0m\x1b[?25h\x1b[?1049l", end="", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=ROOT / "tmp/eval/config/default.json", help="独立评测配置，只允许放在 tmp/ 下")
    parser.add_argument("--preview", action="store_true", help="校验并显示最终命令，不执行、不保存")
    args = parser.parse_args(argv)
    try:
        config = Configuration(args.profile)
        if args.preview: print(config.preview()); return 0
        if not sys.stdin.isatty(): raise ValueError("需要交互终端；SSH 请使用 -t")
        ui(config); return 0
    except (ValueError, OSError, KeyboardInterrupt, EOFError):
        print("评测配置入口未完成；请检查配置文件与交互终端。未启动评测。", file=sys.stderr)
        return 1


if __name__ == "__main__": raise SystemExit(main())
