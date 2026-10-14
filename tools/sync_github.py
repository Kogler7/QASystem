"""Prepare an independently versioned cleaned history of published source; publish explicitly."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

LIMIT = 100 * 1024 * 1024
TOKEN = re.compile(rb"\b(?:sk-[A-Za-z0-9_-]{32,}|gh[pousr]_[A-Za-z0-9]{25,}|AKIA[A-Z0-9]{16})\b")
PRIVATE_KEY = re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----")


class SyncError(RuntimeError):
    pass


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if result.returncode:
        # Do not include process output: remote URLs or hooks can contain credentials.
        reason = ""
        if args[0] == "push" and any(word in result.stderr for word in ("non-fast-forward", "fetch first", "already exists")):
            reason = "; target divergence: refusing non-fast-forward update"
        raise SyncError(f"Git command failed ({result.returncode}): {args[0]}{reason}")
    return result.stdout.strip()


def prohibited(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    name = parts[-1]
    if name == ".env" or (name.startswith(".env.") and not name.endswith(".example")):
        return "deployment environment"
    if name in {"id_rsa", "id_ed25519"} or name.endswith((".p12", ".pfx", ".key")):
        return "private credential file"
    if parts[0] in {"runtime-config", "tmp", "deploy", "models", "data", ".venv"}:
        return "runtime artifact"
    if "chroma_db" in parts or "lightning_logs" in parts:
        return "runtime or training artifact"
    if name.endswith((".safetensors", ".ckpt", ".pt", ".bin", ".sqlite", ".sqlite3", ".db")):
        return "weights or database"
    return None


def entries(root: Path, revision: str) -> list[tuple[str, str, str, int]]:
    raw = subprocess.check_output(["git", "-C", str(root), "ls-tree", "-r", "-l", "-z", revision])
    rows = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        meta, path = item.split(b"\t", 1)
        mode, kind, oid, size = meta.decode().split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise SyncError("Unsupported symlink/submodule in source; review required")
        rows.append((path.decode("utf-8"), mode, oid, int(size)))
    return rows


def blobs(root: Path, rows):
    process = subprocess.Popen(["git", "-C", str(root), "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for path, mode, oid, size in rows:
            process.stdin.write((oid + "\n").encode())
            process.stdin.flush()
            header = process.stdout.readline().decode().split()
            if len(header) != 3 or header[1] != "blob" or int(header[2]) != size:
                raise SyncError("Unexpected source object")
            content = process.stdout.read(size)
            if len(content) != size or process.stdout.read(1) != b"\n":
                raise SyncError("Incomplete source object")
            yield path, mode, content
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait()


def credentials(content: bytes) -> bool:
    if PRIVATE_KEY.search(content):
        return True
    return any(len(set(match.group())) >= 12 for match in TOKEN.finditer(content))


POLICY_VERSION = 1
PATH_REGEXES = (r"(^|/)\.env$", r"(^|/)(chroma_db|lightning_logs)/", r".*\.(safetensors|ckpt|pt|bin|sqlite|sqlite3|db)$")
EXCLUDED = [
    ".env", ".claude/settings.local.json", "frontend/frontend/", "tmp/", "runtime-config/",
    "deploy/runtime/", "algorithm/NL_to_cypher/bge-small-zh-v1.5/",
    "algorithm/NL_to_cypher/chroma_db/", "backend/chroma_db/",
    "algorithm/confidence_calculate/sbert_model/", "algorithm/confidence_calculate/lightning_logs/",
    "algorithm/confidence_calculate/embeddings/", "algorithm/confidence_calculate/final_model.pt",
    "algorithm/preproc/LibreOffice-fresh.full-x86_64.AppImage",
]


def source_refs(root: Path, source: str, fetch: bool = True) -> dict[str, str]:
    if fetch:
        git(root, "fetch", "--prune", "--no-tags", source,
            f"+refs/heads/*:refs/remotes/{source}/*",
            f"+refs/tags/*:refs/github-sync/source-tags/*")
    result = {}
    for line in git(root, "for-each-ref", "--format=%(refname) %(objectname)",
                    f"refs/remotes/{source}", "refs/github-sync/source-tags").splitlines():
        name, oid = line.split()
        if name == f"refs/remotes/{source}/HEAD":
            continue
        if name.startswith(f"refs/remotes/{source}/"):
            target = "refs/heads/" + name[len(f"refs/remotes/{source}/"):]
        else:
            target = "refs/tags/" + name[len("refs/github-sync/source-tags/"):]
        result[target] = oid
    if not result:
        raise SyncError("Source has no published refs")
    return result


def audit_history(root: Path, refs: list[str]) -> dict:
    objects = git(root, "rev-list", "--objects", *refs).splitlines()
    paths = {line.split(" ", 1)[0]: line.split(" ", 1)[1] for line in objects if " " in line}
    response = subprocess.run(["git", "-C", str(root), "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                              input="\n".join(paths) + "\n", capture_output=True, text=True, check=True)
    rows = []
    for line in response.stdout.splitlines():
        oid, kind, size = line.split()
        if kind == "blob":
            rows.append((paths[oid], "100644", oid, int(size)))
    failures = [{"path": path, "reason": prohibited(path) or "GitHub size limit"}
                for path, _, _, size in rows if prohibited(path) or size > LIMIT]
    for path, _, content in blobs(root, rows):
        if credentials(content):
            failures.append({"path": path, "reason": "credential pattern"})
    if failures:
        raise SyncError("History audit rejected paths: " + json.dumps(failures, ensure_ascii=False))
    return {"blob_versions": len(rows), "max_blob_bytes": max((row[3] for row in rows), default=0)}


def prepare(root: Path, candidate: Path, source: str = "origin", branch: str = "develop", fetch: bool = True) -> dict:
    import importlib.util
    import sys
    import tempfile
    root = root.resolve()
    cache = candidate.resolve()
    if cache == root or root.is_relative_to(cache):
        raise SyncError("Cache must be separate from primary repository")
    cache.mkdir(parents=True, exist_ok=True)
    import hashlib
    policy = cache / "policy-version.json"
    policy_data = {"version": POLICY_VERSION, "digest": hashlib.sha256(json.dumps([EXCLUDED, PATH_REGEXES, LIMIT], ensure_ascii=False).encode()).hexdigest()}
    if policy.exists() and json.loads(policy.read_text()) != policy_data:
        raise SyncError("Policy version changed; explicit history rewrite review required")
    refs = source_refs(root, source, fetch)
    if "refs/heads/" + branch not in refs:
        raise SyncError("Requested source branch not present")
    source_url = git(root, "remote", "get-url", source)
    latest = cache / "latest.json"
    if latest.is_file():
        prior = json.loads(latest.read_text(encoding="utf-8"))
        repo = Path(prior["repository"]).resolve()
        if repo.parent == cache and prior["source_remote"] == source_url and {k: v["source"] for k, v in prior["refs"].items()} == refs:
            observed = dict(line.split() for line in git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags").splitlines())
            if observed != {k: v["cleaned"] for k, v in prior["refs"].items()}:
                raise SyncError("Cached refs differ from audit mapping")
            prior["audit"] = audit_history(repo, list(observed))
            prior["published"] = False
            return prior
    module = importlib.util.find_spec("git_filter_repo")
    if not module:
        raise SyncError("Install development tool git-filter-repo==2.47.0 first")
    # A new isolated bare repository for each pass preserves deterministic rewritten IDs.
    # Alternates are read-only; primary refs and objects are never rewritten or removed.
    cleaned = Path(tempfile.mkdtemp(prefix="filtered-", dir=cache))
    git(cleaned, "init", "--bare")
    git(cleaned, "config", "core.protectNTFS", "false")
    objects = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "objects"))
    (cleaned / "objects" / "info" / "alternates").write_bytes((str(objects).replace("\\", "/") + "\n").encode("utf-8"))
    for name, oid in refs.items():
        git(cleaned, "update-ref", name, oid)
    command = [sys.executable, module.origin, "--force", "--invert-paths",
               "--prune-empty", "never", "--prune-degenerate", "never", "--strip-blobs-bigger-than", "100M"]
    for path in EXCLUDED:
        command += ["--path", path]
    for pattern in PATH_REGEXES:
        command += ["--path-regex", pattern]
    # --force applies only to the throwaway local copy, never to a remote push.
    import os
    filter_env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    process = subprocess.run(command, cwd=cleaned, env=filter_env, capture_output=True, text=True)
    if process.returncode:
        (cleaned / "filter-diagnostic.txt").write_text(process.stderr, encoding="utf-8")
        raise SyncError("Independent history filtering failed; local filter-diagnostic.txt")
    actual = {}
    for line in git(cleaned, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags").splitlines():
        name, oid = line.split()
        actual[name] = oid
    if set(actual) != set(refs):
        raise SyncError("Filtering unexpectedly removed refs")
    audit = audit_history(cleaned, list(actual))
    mapping = {name: {"source": oid, "cleaned": actual[name]} for name, oid in refs.items()}
    result = {"policy_version": POLICY_VERSION, "source_remote": source_url, "repository": str(cleaned),
              "refs": mapping, "audit": audit, "published": False}
    (cleaned / "source-map.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    policy.write_text(json.dumps(policy_data) + "\n", encoding="utf-8")
    maps = cache / "mappings"
    maps.mkdir(exist_ok=True)
    map_id = hashlib.sha256(json.dumps(refs, sort_keys=True).encode()).hexdigest()[:16]
    (maps / (map_id + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    commit_map = cleaned / "filter-repo" / "commit-map"
    if commit_map.exists():
        (maps / (map_id + "-commit-map.txt")).write_bytes(commit_map.read_bytes())
    (cache / "latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    # Only completed, tool-owned repositories directly beneath this cache can be removed.
    import shutil
    older = sorted([p for p in cache.glob("filtered-*") if p.is_dir() and (p / "source-map.json").is_file()],
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for old in older[2:]:
        resolved = old.resolve()
        if resolved.parent != cache or resolved == cleaned or not (resolved / "HEAD").is_file():
            raise SyncError("Unsafe cache cleanup target")
        if json.loads((resolved / "source-map.json").read_text(encoding="utf-8")).get("policy_version") != POLICY_VERSION:
            continue
        shutil.rmtree(resolved)
    return result


def publish(candidate: Path, target: str, branch: str = "develop") -> None:
    if not (candidate / "source-map.json").is_file():
        raise SyncError("Candidate has no audit/source mapping")
    metadata = json.loads((candidate / "source-map.json").read_text(encoding="utf-8"))
    if target == metadata["source_remote"]:
        raise SyncError("Refusing to publish cleaned history back to primary source")
    mapping = metadata["refs"]
    audit_history(candidate, list(mapping))
    # Atomic normal push: unrelated or rewritten target refs reject the entire update.
    git(candidate, "push", "--atomic", target, *[f"{name}:{name}" for name in sorted(mapping)])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--source", default="origin")
    parser.add_argument("--branch", default="develop")
    parser.add_argument("--target", default="git@github.com:Kogler7/QASystem.git")
    parser.add_argument("--publish", action="store_true", help="Publish audited, filtered history without force")
    args = parser.parse_args()
    try:
        candidate = args.candidate or args.root / "tmp" / "github-sync-v1"
        result = prepare(args.root, candidate, args.source, args.branch)
        if args.publish:
            publish(Path(result["repository"]), args.target, args.branch)
            result["published"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (SyncError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"History sync blocked: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
