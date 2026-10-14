import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("github_sync", Path(__file__).parents[1] / "tools" / "sync_github.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def run(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    run(root, "init", "--initial-branch=develop")
    run(root, "config", "user.name", "Fixture")
    run(root, "config", "user.email", "fixture@localhost")
    run(root, "remote", "add", "origin", str(tmp_path / "origin.git"))
    (root / "app.py").write_text("print('first')\n")
    (root / ".env").write_text("SOME_SECRET=historical-deployment-value\n")
    run(root, "add", ".")
    run(root, "commit", "-m", "first")
    run(root, "update-ref", "refs/remotes/origin/develop", "HEAD")
    return root


def test_deterministic_filter_and_incremental_fast_forward(source, tmp_path):
    first = sync.prepare(source, tmp_path / "cache", fetch=False)
    same = sync.prepare(source, tmp_path / "cache", fetch=False)
    assert first["refs"] == same["refs"]
    cleaned = Path(first["repository"])
    assert ".env" not in run(cleaned, "rev-list", "--objects", "--all").splitlines()
    assert not any(line.endswith(" .env") for line in run(cleaned, "rev-list", "--objects", "--all").splitlines())
    target = tmp_path / "target.git"
    target.mkdir()
    run(target, "init", "--bare")
    sync.publish(cleaned, str(target))
    (source / "app.py").write_text("print('second')\n")
    run(source, "add", ".")
    run(source, "commit", "-m", "second")
    run(source, "update-ref", "refs/remotes/origin/develop", "HEAD")
    second = sync.prepare(source, tmp_path / "cache", fetch=False)
    sync.publish(Path(second["repository"]), str(target))
    assert run(target, "rev-parse", "develop") == second["refs"]["refs/heads/develop"]["cleaned"]
    assert run(source, "rev-parse", "HEAD") == second["refs"]["refs/heads/develop"]["source"]


def test_new_credentials_block_publication_without_printing_value(source, tmp_path):
    secret = "sk-" + "abcdefghijklmnopqrstuv012345678901234567890123456789"
    (source / "app.py").write_text("API_KEY='" + secret + "'\n")
    run(source, "add", ".")
    run(source, "commit", "-m", "bad credential")
    run(source, "update-ref", "refs/remotes/origin/develop", "HEAD")
    with pytest.raises(sync.SyncError) as error:
        sync.prepare(source, tmp_path / "cache", fetch=False)
    assert "app.py" in str(error.value)
    assert secret not in str(error.value)


def test_target_divergence_rejects_without_force(source, tmp_path):
    prepared = sync.prepare(source, tmp_path / "cache", fetch=False)
    target = tmp_path / "target"
    target.mkdir()
    run(target, "init", "--bare")
    # An unrelated branch prevents overwriting it; primary remains untouched.
    other = tmp_path / "other"
    other.mkdir()
    run(other, "init", "--initial-branch=develop")
    run(other, "config", "user.name", "Fixture")
    run(other, "config", "user.email", "fixture@localhost")
    (other / "other.txt").write_text("independent")
    run(other, "add", ".")
    run(other, "commit", "-m", "independent")
    run(other, "push", str(target), "develop")
    before = run(target, "rev-parse", "develop")
    with pytest.raises(sync.SyncError):
        sync.publish(Path(prepared["repository"]), str(target))
    assert run(target, "rev-parse", "develop") == before


def test_policy_version_change_requires_review(source, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "policy-version.json").write_text(json.dumps({"version": 0}))
    with pytest.raises(sync.SyncError, match="Policy version changed"):
        sync.prepare(source, cache, fetch=False)


def test_source_disconnect_reports_failure(source, tmp_path):
    with pytest.raises(sync.SyncError):
        sync.prepare(source, tmp_path / "cache")


def test_fake_token_and_example_config_remain_allowed():
    assert not sync.credentials(b"sk-" + b"a" * 48)
    assert sync.prohibited("docker/.env.deploy.example") is None
    assert sync.prohibited("module/.env")
