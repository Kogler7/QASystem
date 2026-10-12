"""Keyboard configuration editor. Opening it never starts or hashes models."""
from __future__ import annotations

import argparse
from datetime import datetime
import getpass
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import sys

try:
    from docker import setup_llm as helper
except ImportError:
    import setup_llm as helper

# This utility also ships outside the application package.
HOT_KEYS = frozenset({
    "KG_EXTRACT_ALGO_VERSION", "KG_EXTRACT_LLM_BASE_URL", "KG_EXTRACT_LLM_MODEL",
    "KG_EXTRACT_LLM_MAX_TOKENS", "KG_EXTRACT_LLM_MAX_INPUT_TOKENS",
    "KG_EXTRACT_LLM_MAX_OUTPUT_TOKENS", "KG_EXTRACT_LLM_MAX_CONCURRENCY",
    "KG_EXTRACT_LLM_QPS", "KG_EXTRACT_LLM_QPM", "KG_EXTRACT_LLM_TIMEOUT_SECONDS",
    "KG_EXTRACT_LLM_DISABLE_THINKING", "KG_EXTRACT_LLM_QUOTA_GROUP",
    "KG_EXTRACT_V31_REQUEST_TIMEOUT", "KG_EXTRACT_V31_DOCUMENT_TIMEOUT",
    "QASYSTEM_JUDGE_BASE_URL", "QASYSTEM_JUDGE_MODEL", "QASYSTEM_JUDGE_ADAPTER",
    "QASYSTEM_JUDGE_MAX_TOKENS",
})


def secret(key: str) -> bool:
    return any(word in key for word in ("KEY", "PASSWORD", "SECRET", "TOKEN", "DATABASE_URL", "REDIS_URL")) and not any(
        word in key for word in ("MAX_TOKENS", "INPUT_TOKENS", "OUTPUT_TOKENS", "BATCHED_TOKENS"))


def schema(text: str) -> list[dict]:
    """Use the release's .env.example as the single defaults/help catalog."""
    comments, rows, group = [], [], "通用"
    for line in text.splitlines():
        if not line.strip():
            comments = []
        elif line.startswith("#"):
            comments.append(line[1:].strip())
        elif re.match(r"^[A-Z][A-Z0-9_]*=", line):
            key = line.split("=", 1)[0]
            if comments:
                group = comments[0]
            default = helper.read_env(line)[key]
            rows.append(dict(key=key, default=default, description=" / ".join(comments) or f"{key}；当前版本默认值见下方",
                             group=group, hot=key in HOT_KEYS, secret=secret(key)))
            comments = []
    return rows


def slug(repo: str) -> str:
    return "llm_" + re.sub(r"[^a-z0-9]+", "_", repo.lower()).strip("_")


class Configuration:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.catalog = json.loads(Path(__file__).with_name("llm-models.json").read_text(encoding="utf-8"))
        self.template = (root / ".env.example").read_text(encoding="utf-8-sig")
        self.fields = schema(self.template)
        self.original = (root / ".env").read_text(encoding="utf-8-sig") if (root / ".env").exists() else ""
        self.values = {**helper.read_env(self.template), **helper.read_env(self.original)}
        directory = Path(self.values.get("QASYSTEM_RUNTIME_CONFIG_DIR", "./runtime-config"))
        self.directory = (root / directory).resolve() if not directory.is_absolute() else directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        hot = self.directory / "settings.json"
        if hot.exists():
            current = json.loads(hot.read_text())
            if not isinstance(current, dict) or any(k not in HOT_KEYS or not isinstance(v, str) for k, v in current.items()):
                raise ValueError("热配置文件内容无效")
            self.values.update(current)
        models = self.directory / "models.json"
        self.models = json.loads(models.read_text()) if models.exists() else {}
        applied = self.directory / "applied-models.json"
        self.previous_models = json.loads(applied.read_text()) if applied.exists() else {}
        for index, model in enumerate(self.catalog):
            self.models.setdefault(model["repo"], dict(enabled=False, gpu="", port=str(9997 + index),
                context="32768", utilization="0.90"))
        # Back up on entry, before any edits. Permission-restricted, never displayed.
        self.backup = self.directory / ("backups/" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        self.backup.mkdir(parents=True, mode=0o700)
        for name, source in [("deployment.env", root / ".env"), ("settings.json", hot),
                             ("models.json", models), ("applied-models.json", applied),
                             ("models.compose.json", self.directory / "models.compose.json")]:
            if source.exists():
                target = self.backup / name
                target.write_bytes(source.read_bytes())
                target.chmod(0o600)
        self.expected = {p: p.read_bytes() if p.exists() else None for p in
            (root / ".env", hot, models, self.directory / "models.compose.json")}

    def model_root(self) -> Path:
        path = Path(self.values["HOST_MODELS_DIR"]).expanduser()
        return (self.root / path).resolve() if not path.is_absolute() else path.resolve()

    def rows(self, query: str = "") -> list[dict]:
        rows = []
        for model in self.catalog:
            repo = model["repo"]
            state = self.models[repo]
            exists = (self.model_root() / "llm" / repo.replace("/", "--")).is_dir()
            rows.append(dict(kind="model", repo=repo, key="enabled", label=f"[{ 'x' if state['enabled'] else ' ' }] {repo} ({'权重已找到' if exists else '无本地权重'})"))
            if state["enabled"]:
                for key, name in (("gpu", "GPU编号"), ("port", "宿主端口"), ("context", "上下文上限"), ("utilization", "显存比例")):
                    rows.append(dict(kind="model", repo=repo, key=key, label=f"    {name}: {state[key]}"))
        for field in self.fields:
            value = self.values.get(field["key"], "")
            shown = "******" if field["secret"] and value else value
            rows.append(dict(kind="env", **field, label=f"{'热更新' if field['hot'] else '启动配置'}  {field['key']} = {shown}"))
        return [r for r in rows if query.lower() in (r["label"] + r.get("description", "")).lower()]

    def validate(self, probe: bool = False) -> list[str]:
        errors, ports, gpus = [], {}, {}
        allowed = set(helper.read_env("GPU_DEVICE_IDS=" + self.values.get("GPU_DEVICE_IDS", ""))["GPU_DEVICE_IDS"].split(","))
        reserved = set(self.values.get("EXECUTION_GPU_RESERVED_DEVICE_IDS", "").split(","))
        own = {m["gpu"] for m in self.previous_models.values() if m.get("enabled")}
        for key in ("BACKEND_HOST_PORT", "AGENT_HOST_PORT", "CLEF_PORT", "NANOJEV_PORT"):
            if self.values.get(key):
                try:
                    port = int(self.values[key])
                    if not 1 <= port <= 65535: raise ValueError()
                    if port in ports: errors.append(f"{key} 与 {ports[port]} 端口重复")
                    ports[port] = key
                except ValueError: errors.append(f"{key} 必须是有效端口")
        for repo, model in self.models.items():
            if not model["enabled"]: continue
            try:
                port, length, ratio = int(model["port"]), int(model["context"]), float(model["utilization"])
                if not 1 <= port <= 65535 or not 1024 <= length <= 262144 or not math.isfinite(ratio) or not 0 < ratio < 1:
                    raise ValueError()
                if port in ports: errors.append(f"{repo} 与 {ports[port]} 端口重复")
                ports[port] = repo
                gpu = model["gpu"]
                if not gpu.isdigit() or (self.values.get("GPU_DEVICE_IDS") and gpu not in allowed):
                    errors.append(f"{repo} GPU 不在允许列表")
                if gpu in gpus: errors.append(f"{repo} 与 {gpus[gpu]} 使用同一卡")
                if gpu in reserved and gpu not in own: errors.append(f"{repo} GPU 已被其他服务预留")
                gpus[gpu] = repo
                path = self.model_root() / "llm" / repo.replace("/", "--")
                if not path.is_dir(): errors.append(f"{repo} 本地权重目录不存在")
                previous = self.previous_models.get(repo, {})
                unchanged_port = previous.get("enabled") and previous.get("port") == model["port"]
                if probe and not unchanged_port:
                    with socket.socket() as sock: sock.bind(("127.0.0.1", port))
            except (ValueError, OSError): errors.append(f"{repo} 参数无效或端口被其他进程占用")
        for key, value in self.values.items():
            if key in HOT_KEYS and value and any(suffix in key for suffix in ("MAX_TOKENS", "INPUT_TOKENS", "OUTPUT_TOKENS", "CONCURRENCY", "QPS", "QPM", "TIMEOUT")):
                try:
                    number = float(value)
                    if not math.isfinite(number) or number < 0 or (number == 0 and not key.endswith(("QPS", "QPM"))): raise ValueError()
                    if "TIMEOUT" not in key and not number.is_integer(): raise ValueError()
                except ValueError: errors.append(f"{key} 数值无效")
        if self.values.get("KG_EXTRACT_ALGO_VERSION") not in {"latest", "v0", "v2", "v2.1", "v2.2", "v3", "v3.1", "jev"}:
            errors.append("抽取算法版本无效")
        a, b = self.values.get("KG_EXTRACT_LLM_MAX_TOKENS"), self.values.get("KG_EXTRACT_LLM_MAX_OUTPUT_TOKENS")
        if a and b and a != b: errors.append("MAX_TOKENS 与 MAX_OUTPUT_TOKENS 必须一致（或后者留空）")
        for key in ("KG_EXTRACT_LLM_BASE_URL", "QASYSTEM_JUDGE_BASE_URL"):
            value = self.values.get(key, "")
            if value:
                from urllib.parse import urlsplit
                url = urlsplit(value)
                if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                    errors.append(f"{key} 必须是无凭据的 HTTP(S) 地址")
        if self.values.get("QASYSTEM_ENV_IMAGE", "").split(":")[0] != "qasystem-env-v1": errors.append("环境镜像必须为 qasystem-env-v1")
        return errors

    def compose(self) -> dict:
        services = {}
        for repo, model in self.models.items():
            if not model["enabled"]: continue
            services[slug(repo)] = dict(image=self.values["QASYSTEM_ENV_IMAGE"], pull_policy="never",
                restart="unless-stopped", init=True, gpus="all", environment={"CUDA_VISIBLE_DEVICES": model["gpu"],
                "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "VLLM_NO_USAGE_STATS": "1"},
                volumes=[f"{self.model_root()}:/models:ro"], ports=[f"127.0.0.1:{model['port']}:8000"],
                command=["vllm", "serve", "/models/llm/" + repo.replace("/", "--"), "--served-model-name", repo,
                         "--max-model-len", model["context"], "--gpu-memory-utilization", model["utilization"],
                         "--max-num-seqs", self.values.get("LLM_MAX_NUM_SEQS", "8"), "--max-num-batched-tokens",
                         self.values.get("LLM_MAX_NUM_BATCHED_TOKENS", "2048"), "--dtype", self.values.get("LLM_DTYPE", "bfloat16"), "--language-model-only"],
                healthcheck=dict(test=["CMD", "curl", "-fsS", "http://127.0.0.1:8000/health"], interval="15s", timeout="5s", retries=40, start_period="300s"))
        return {"services": services}

    def save(self) -> None:
        errors = self.validate(probe=True)
        if errors: raise ValueError("；".join(errors))
        for path, expected in self.expected.items():
            if (path.read_bytes() if path.exists() else None) != expected:
                raise ValueError("配置已被其他进程修改；请退出并重新打开，避免覆盖")
        if Path(self.values.get("QASYSTEM_RUNTIME_CONFIG_DIR", "./runtime-config")) != Path(helper.read_env(self.original).get("QASYSTEM_RUNTIME_CONFIG_DIR", "./runtime-config")):
            raise ValueError("运行时目录是启动位置，不能在当前编辑会话中移动")
        static = {k: v for k, v in self.values.items() if k not in HOT_KEYS}
        # Preserve historical credentials/comments/unknown keys and existing hot fallbacks.
        original_values = helper.read_env(self.original or self.template)
        # Unchanged credentials must keep their original quoting and interpolation semantics.
        changes = {k: v for k, v in static.items() if original_values.get(k) != v}
        env = helper.update_env(self.original or self.template, changes)
        payloads = {self.root / ".env": env,
            self.directory / "settings.json": json.dumps({k: self.values[k] for k in HOT_KEYS if k in self.values}, ensure_ascii=False, indent=2),
            self.directory / "models.json": json.dumps(self.models, ensure_ascii=False, indent=2),
            self.directory / "models.compose.json": json.dumps(self.compose(), ensure_ascii=False, indent=2)}
        written = []
        try:
            for path, content in payloads.items():
                helper.atomic_write(path, content + "\n")
                written.append(path)
        except Exception:
            for path in reversed(written):
                old = self.expected[path]
                if old is None: path.unlink(missing_ok=True)
                else: helper.atomic_write(path, old.decode("utf-8-sig"))
            raise
        self.expected = {p: p.read_bytes() for p in payloads}

    def verify_weights(self) -> None:
        for entry in self.catalog:
            if self.models[entry["repo"]]["enabled"]:
                folder = self.model_root() / "llm" / entry["repo"].replace("/", "--")
                for i, part in enumerate(entry["files"], 1):
                    helper.progress(f"{entry['repo']} 校验 {i}/{len(entry['files'])}，{part['size']/1024**3:.2f} GiB：{part['rfilename']}")
                    helper.verify(folder, [part])

    def apply(self) -> None:
        """Explicit action only; never called on entry or Save."""
        errors = self.validate(probe=True)
        if errors: raise ValueError("；".join(errors))
        image = self.values["QASYSTEM_ENV_IMAGE"]
        helper.run(["docker", "image", "inspect", image], cwd=self.root)
        helper.run(["docker", "run", "--rm", "--entrypoint", "python", image, "-c",
            "import vllm; assert tuple(map(int,vllm.__version__.split('.')[:2])) >= (0,31)"], cwd=self.root, timeout=120)
        command = ["docker", "compose", "--project-directory", str(self.root), "-f", str(self.root / "docker-compose.yml")]
        prior_compose = self.directory / "applied.compose.json"
        if prior_compose.exists(): command += ["-f", str(prior_compose)]
        command += ["-f", str(self.directory / "models.compose.json")]
        self.save()
        helper.run(command + ["config", "--quiet"], cwd=self.root)
        changed = [slug(repo) for repo, old in self.previous_models.items() if old.get("enabled") and
                   self.models.get(repo) != old]
        if changed:
            helper.progress("停用或重启本界面管理且参数发生变化的模型；不处理其他模型服务")
            helper.run(command + ["stop", *changed], cwd=self.root, timeout=120)
        gpus = helper.gpu_rows(helper.run(["docker", "run", "--rm", "--gpus", "all", "--entrypoint", "nvidia-smi", image,
            "--query-gpu=index,name,memory.total,memory.used,utilization.gpu", "--format=csv,noheader,nounits"], cwd=self.root))
        for entry in self.catalog:
            model = self.models[entry["repo"]]
            if not model["enabled"]: continue
            if self.previous_models.get(entry["repo"], {}) == model: continue
            check = dict(self.values, LLM_GPU_MEMORY_UTILIZATION=model["utilization"])
            own = {m["gpu"] for m in self.previous_models.values() if m.get("enabled")}
            check["EXECUTION_GPU_RESERVED_DEVICE_IDS"] = ",".join(set(check.get("EXECUTION_GPU_RESERVED_DEVICE_IDS", "").split(",")) - own)
            size = sum(p["size"] for p in entry["files"] if p["rfilename"].endswith(".safetensors"))
            helper.choose_gpu(gpus, model["gpu"], check, size)
        # Reserve GPUs only when deploying, not when merely saving a plan.
        reserved = set(filter(None, self.values.get("EXECUTION_GPU_RESERVED_DEVICE_IDS", "").split(",")))
        reserved.update(m["gpu"] for m in self.models.values() if m["enabled"])
        reserved.update(m["gpu"] for m in self.previous_models.values() if m.get("enabled"))
        env_path = self.root / ".env"
        helper.atomic_write(env_path, helper.update_env(env_path.read_text(),
            {"EXECUTION_GPU_RESERVED_DEVICE_IDS": ",".join(sorted(reserved))}))
        self.expected[env_path] = env_path.read_bytes()
        self.values["EXECUTION_GPU_RESERVED_DEVICE_IDS"] = ",".join(sorted(reserved))
        helper.progress("更新启动配置和目录挂载（Backend/Agent 会重建）；请确保现有任务已完成")
        helper.run(command + ["up", "-d", "--no-deps", "--force-recreate", "backend", "agent"], cwd=self.root, timeout=120)
        names = list(self.compose()["services"])
        if names:
            helper.run(command + ["up", "-d", "--no-deps", *names], cwd=self.root, timeout=120)
        helper.progress("启动命令已提交；模型加载及健康状态请查看 docker compose ps / logs。保存不等于部署成功。")
        self.previous_models = json.loads(json.dumps(self.models))
        helper.atomic_write(self.directory / "applied-models.json", json.dumps(self.previous_models, indent=2))
        helper.atomic_write(prior_compose, json.dumps(self.compose(), indent=2))


def read_key() -> str:
    if os.name == "nt":
        import msvcrt
        key = msvcrt.getwch()
        if key in {"\x00", "\xe0"}: return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(msvcrt.getwch(), "")
        return key
    import termios, tty, select
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        key = os.read(fd, 1).decode(errors="ignore")
        if key == "\x1b":
            tail = ""
            while select.select([sys.stdin], [], [], 0.05)[0]: tail += os.read(fd, 1).decode(errors="ignore")
            return {"[A": "up", "[B": "down", "[C": "right", "[D": "left"}.get(tail, "escape")
        return key
    finally: termios.tcsetattr(fd, termios.TCSADRAIN, original)


def ui(config: Configuration) -> None:
    index, query, notice = 0, "", "已备份原配置；未校验权重、未启动服务。"
    while True:
        rows = config.rows(query)
        index = min(index, max(0, len(rows) - 1))
        height = max(4, shutil.get_terminal_size((100, 28)).lines - 12)
        start = max(0, index - height + 1)
        print("\x1b[2J\x1b[H", end="")
        print("QASystem 配置 | ↑↓/j k 移动 | 空格开关 | Enter 编辑 | / 搜索 | d 恢复默认")
        print("s 保存配置（不部署） | v 校验已勾选权重 | a 应用启动配置并部署 | q 退出")
        print(f"备份: {config.backup}\n搜索: {query or '全部'}\n")
        for i, row in enumerate(rows[start:start + height], start): print(("> " if i == index else "  ") + row["label"])
        row = rows[index] if rows else None
        if row and row["kind"] == "env":
            default = "******" if row["secret"] and row["default"] else (row["default"] or "(空)")
            print(f"\n说明: {row['description']}\n默认: {default}\n生效: {'保存后，新提交请求读取；在途任务保留原参数' if row['hot'] else '保存只写配置，需相应服务重启/重建后生效'}")
        elif row:
            print("\n勾选表示计划启用；保存不会启动。权重检查和部署需分别按 v / a。\n模型启动参数修改需要重建该模型容器。")
        print("\n" + notice)
        key = read_key()
        try:
            if key in {"q", "\x03", "escape"}: return
            if key in {"down", "j"}: index = min(len(rows) - 1, index + 1)
            elif key in {"up", "k"}: index = max(0, index - 1)
            elif key == "/": query = input("筛选配置名称/说明（空为全部）: ").strip(); index = 0
            elif key in {" ", "\r", "\n", "d"} and row:
                if row["kind"] == "model":
                    state = config.models[row["repo"]]
                    if row["key"] == "enabled": state["enabled"] = not state["enabled"]
                    elif key != " ": state[row["key"]] = input(f"{row['label']} → ").strip()
                else:
                    name = row["key"]; value = config.values.get(name, "")
                    if key == "d": config.values[name] = row["default"]
                    elif key == " " and (value.lower() in {"true", "false"} or
                            (value in {"0", "1"} and any(s in name for s in ("ENABLED", "DISABLE", "VERIFY", "SCHEDULING")))):
                        config.values[name] = {"true": "false", "false": "true", "0": "1", "1": "0"}[value.lower()]
                    elif key != " ": config.values[name] = (getpass.getpass if row["secret"] else input)(f"{name} 新值: ")
            elif key == "s": config.save(); notice = "已生成配置；热配置对新请求生效。启动配置尚未应用，未启动模型。"
            elif key == "v":
                if input("将完整读取已勾选模型的权重，可能耗时数分钟。输入 verify 开始: ") == "verify":
                    config.verify_weights(); input("权重校验完成，回车返回。")
            elif key == "a":
                print("将保存配置、重建 Backend/Agent 并启动勾选模型；请先完成在途任务。")
                if input("输入 deploy 执行，其他输入返回: ") == "deploy":
                    config.apply(); input("回车返回配置界面。")
        except (ValueError, OSError, RuntimeError) as exc: notice = str(exc)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path)
    parser.add_argument("--check", action="store_true", help="只检查配置结构与冲突，不部署、不校验权重")
    args = parser.parse_args(argv)
    try:
        config = Configuration((args.project_dir or helper.project_dir()).resolve())
        if args.check:
            errors = config.validate()
            print("\n".join(errors) if errors else "配置检查通过（未部署、未校验权重）")
            return int(bool(errors))
        if not sys.stdin.isatty(): raise ValueError("配置界面需要交互终端，请使用 ssh -t")
        ui(config)
        return 0
    except (OSError, ValueError, KeyboardInterrupt, EOFError):
        print("配置界面未完成；未显示配置值，请检查路径、JSON 格式或交互终端。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
