#!/usr/bin/env python3
"""按类型选择 1–2 个 case，再交给统一评测入口执行。"""
from __future__ import annotations

import random
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

try:  # pytest / python3 -m eval.mini_eval
    from eval import run
except ImportError:  # python3 eval/mini_eval.py
    import run


CONFIG_KEYS = {"cases_per_type", "per_type", "selection", "seed"}


def _case_count(value: Any, name: str) -> int:
    if type(value) is not int or value not in (1, 2):
        raise ValueError(f"{name} 只允许整数 1 或 2")
    return value


def select_cases(workspace: Path, config: Any) -> tuple[list[Path], str, int | None]:
    """使用统一入口的目录规则，返回有界选择和实际随机种子。"""
    if not isinstance(config, dict):
        raise ValueError("mini_eval 必须是 YAML 对象")
    unknown_keys = set(config) - CONFIG_KEYS
    if unknown_keys:
        raise ValueError(f"mini_eval 未知配置项: {', '.join(sorted(map(str, unknown_keys)))}")
    count = _case_count(config.get("cases_per_type", 1), "mini_eval.cases_per_type")
    per_type = config.get("per_type", {})
    if not isinstance(per_type, dict):
        raise ValueError("mini_eval.per_type 必须是 YAML 对象")
    for name, value in per_type.items():
        if not isinstance(name, str) or not name or name != name.strip():
            raise ValueError("mini_eval.per_type 的类型名称必须是非空字符串且没有首尾空格")
        _case_count(value, f"mini_eval.per_type.{name}")
    selection = config.get("selection", "first")
    if selection not in ("first", "random"):
        raise ValueError("mini_eval.selection 只允许 first 或 random")
    seed = config.get("seed", 42)
    if seed is not None and type(seed) is not int:
        raise ValueError("mini_eval.seed 必须是整数或 null")
    if not workspace.is_dir():
        raise ValueError(f"评测工作区不存在或不是目录: {workspace}")

    candidates = {
        type_dir.name: cases
        for type_dir in run.discover_types(workspace)
        if (cases := run.discover_cases(type_dir))
    }
    if not candidates:
        raise ValueError("mini_eval 未发现任何有效 case，不会启动评测")
    unknown_types = set(per_type) - candidates.keys()
    if unknown_types:
        raise ValueError(f"mini_eval.per_type 中的类型不存在或没有有效 case: {', '.join(sorted(unknown_types))}")

    if selection == "random" and seed is None:
        seed = secrets.randbits(64)
    rng = random.Random(seed)
    chosen: list[Path] = []
    for name, cases in candidates.items():
        take = min(per_type.get(name, count), len(cases))
        selected = cases[:take] if selection == "first" else sorted(rng.sample(cases, take))
        chosen.extend(path.resolve() for path in selected)
    return chosen, selection, seed


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    args = run.parse_args(raw_args)
    if args.targets or args.groups or args.cases or any(
        arg.split("=", 1)[0] in {"--groups", "--cases"} for arg in raw_args
    ):
        run.log("mini_eval 不接受路径、--groups 或 --cases；请使用 --mini-cases-per-type / --mini-per-type")
        return 1

    workspace = Path(args.workspace).expanduser().resolve()
    try:
        per_type = {}
        for item in args.mini_per_type.split(","):
            if not item.strip(): continue
            name, count = item.split("=", 1)
            per_type[name.strip()] = int(count)
        selection_config = {"cases_per_type": args.mini_cases_per_type, "per_type": per_type,
                            "selection": args.mini_selection, "seed": args.mini_seed}
        chosen, selection, seed = select_cases(workspace, selection_config)
    except (OSError, RuntimeError, ValueError) as exc:
        run.log(str(exc))
        return 1

    run.log(f"mini_eval: 策略={selection}, seed={seed}, 共选择 {len(chosen)} 个 case")
    for case in chosen:
        run.log(f"  {case.relative_to(workspace)}")
    # 位置参数放在选项之前，避免被 --mode 的 nargs='+' 吞掉。
    # 单独的 '--' 已由解析器验证其后没有目标，转发时无需保留。
    forwarded_args = [arg for arg in raw_args if arg != "--"]
    command = [
        sys.executable, str(Path(run.__file__).resolve()),
        *(str(case) for case in chosen), *forwarded_args,
        "--workspace", str(workspace),
    ]
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
