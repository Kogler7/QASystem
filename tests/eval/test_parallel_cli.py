"""Real CLI processes against a local recording HTTP stub; no model calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from eval import run


def test_parallel_directory_allocation_and_retention(tmp_path):
    started = datetime(2020, 1, 2, 3, 4, 5)
    protected = tmp_path / "20200102-030405"
    protected.mkdir()
    (protected / "keep").write_text("original")
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: run.create_run_directory(tmp_path, started, {protected}), range(12)))
    assert len(set(paths)) == 12
    assert all(run.RUN_ID_PATTERN.fullmatch(p.name) for p in paths)
    result = run.cleanup_old_runs(tmp_path, 1, now=datetime(2020, 2, 1), protected={protected})
    assert len(result["deleted"]) == 12
    assert (protected / "keep").read_text() == "original"


def test_two_cli_processes_keep_parameters_requests_and_artifacts_isolated(tmp_path):
    workspace = tmp_path / "workspace"
    case = workspace / "rule" / "one"
    (case / "docs").mkdir(parents=True)
    (case / "docs" / "sample.md").write_text("source", encoding="utf-8")
    ontology = workspace / "rule" / "ontology"
    ontology.mkdir()
    (ontology / "schema.puml").write_text("@startuml\nclass T {\n name: String\n}\n@enduml")
    (case / "extract").mkdir()
    graph = {"entities": [{"type": "T", "name": "n1"}], "relations": []}
    (case / "extract" / "golden.json").write_text(json.dumps(graph))
    source = tmp_path / "frozen"
    (source / "rule" / "one").mkdir(parents=True)
    (source / "rule" / "one" / "document.md").write_text("reused parse, fresh chunking")
    requests = []
    barrier = threading.Barrier(2)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            if self.path == run.CHUNK_PATH:
                barrier.wait(timeout=20)  # Proves both independent runs overlap in time.
                data = {"items": [{"text": f"overlap={body['options']['chunk_overlap']}"}]}
            else:
                assert self.path == run.EXTRACT_PATH
                data = {"graph": graph, "quality": {"extract_version": body["options"]["version"],
                        "effective_config": body["options"]}}
            payload = json.dumps({"code": 0, "data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    params = tmp_path / "params.yaml"
    params.write_text(json.dumps({"backend_url": f"http://127.0.0.1:{server.server_port}",
                                 "options": {"chunking": {"chunk_overlap": 50}}, "mini_eval": {}}))
    runs = tmp_path / "runs"
    # Fix the timestamp to reproduce the old same-second collision deterministically.
    bootstrap = (
        "import sys; from pathlib import Path; from datetime import datetime; from eval import run; "
        "run.RUNS_DIR=Path(sys.argv.pop(1)); "
        "run.datetime=type('FixedDateTime',(datetime,),{'now':classmethod(lambda cls: cls(2026,10,11,12,0,0))}); "
        "raise SystemExit(run.main())"
    )
    env = {k: v for k, v in os.environ.items() if k not in {"QASYSTEM_BACKEND_URL", "QASYSTEM_MCP_URL", "AUTHORIZATION"}}
    env.update(PYTHONIOENCODING="utf-8", NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")

    def invoke(spec):
        version, provider, overlap, timeout = spec
        return subprocess.run([sys.executable, "-c", bootstrap, str(runs), "--workspace", str(workspace),
            "--params", str(params), "--from-run", str(source), "--mode", "chunk-extract",
            "--match-mode", "identity", "--runs-retention-days", "0",
            "--extract-version", version, "--extract-provider", provider, "--extract-model", provider + "-model",
            "--extract-request-timeout", str(timeout), "--chunk-overlap", str(overlap), "--chunk-size", "1000"],
            cwd=run.KIT_DIR.parent, env=env, capture_output=True, text=True, encoding="utf-8", timeout=40)

    specs = [("v2", "provider-a", 0, 10), ("v3.1", "provider-b", 80, 20)]
    before = params.read_bytes()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(invoke, specs))
        assert all(r.returncode == 0 for r in results), [(r.stdout, r.stderr) for r in results]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert params.read_bytes() == before
    directories = list(runs.iterdir())
    assert len(directories) == 2
    assert len(requests) == 4
    for version, provider, overlap, timeout in specs:
        matching = [b for path, b in requests if path == run.EXTRACT_PATH and b["options"]["provider"] == provider]
        assert len(matching) == 1
        body = matching[0]
        assert body["items"] == [{"text": f"overlap={overlap}"}]
        assert body["options"]["version"] == version
        assert body["options"]["request_timeout"] == timeout
        assert body["options"]["model"] == provider + "-model"
    for directory in directories:
        options = json.loads((directory / "params.resolved.json").read_text())["options"]
        artifact = json.loads((directory / "rule" / "one" / "backend_extract.json").read_text())
        assert artifact["data"]["quality"]["effective_config"] == options["extraction"]
        chunk = json.loads((directory / "rule" / "one" / "chunks.json").read_text())
        assert chunk["data"]["items"][0]["text"] == f"overlap={options['chunking']['chunk_overlap']}"
