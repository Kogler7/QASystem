"""Frozen old/fixed verifier prompt study; replay only when explicitly scheduled."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

from jsonschema import Draft202012Validator

from algorithm.knowledge_extract.v3.semantic_evidence import build_verification_messages, VERIFICATION_SCHEMA
from algorithm.knowledge_extract.jsonl_protocol import messages_for_jsonl, record_schema
from eval.tools.property_loop_study import safe_error
from eval.tools.repetition_study import records
from eval.tools.repetition_causal import expansion_trajectory


def read(path):
    return json.loads(Path(path).read_text(encoding="utf8"))


def section(messages, name):
    user = next(m["content"] for m in messages if m["role"] == "user")
    match = re.search(r"## " + re.escape(name) + r"\n(.*?)(?=\n## |\Z)", user, re.S)
    return json.loads(match[1].strip())


def fixed_messages(messages, schema):
    chunk = section(messages, "current chunk")
    items = section(messages, "candidates")
    fixed = build_verification_messages(chunk=SimpleNamespace(**chunk), items=items)
    return messages_for_jsonl(fixed, schema)


def freeze(trace, output):
    output.mkdir(parents=True, exist_ok=True)
    candidates = []
    for file in sorted(trace.glob("*-call.json")):
        call = read(file)
        if call["context"]["pass_name"] != "evidence_verification":
            continue
        parsed_files = [p for p in trace.glob("*-parsed.json") if read(p).get("call_id") == file.stem]
        if not parsed_files:
            continue
        verdicts = read(parsed_files[0])["parsed"]["verdicts"]
        placeholder = sum(v.get("supported") == "no" and any(k in str(v.get("reason", "")).lower()
                          for k in ("占位", "placeholder")) for v in verdicts)
        if int(file.name[:6]) >= 139 and placeholder:
            candidates.append((file, call, placeholder))
    selected = sorted(candidates, key=lambda row: (-row[2], row[0].name))[:2]
    if len(selected) != 2:
        raise ValueError("Need two completed genuine placeholder-rejected property verifier calls")
    cases = []
    for index, (file, call, rejected) in enumerate(selected, 1):
        cases.append({"id": f"historical-{index}", "source_trace": read(trace / "manifest.json")["trace_id"],
                      "source_call": file.stem, "schema": call["schema"],
                      "old_messages": messages_for_jsonl(call["messages"], call["schema"]),
                      "fixed_messages": fixed_messages(call["messages"], call["schema"]),
                      "labels": {"status": "agent-reviewed/provisional; no independent human gold",
                                 "historical_placeholder_no": rejected}})
    items = [
        {"item_id": "control-supported", "kind": "property", "entity": "设备A",
         "properties": {"状态": "停机"}, "entity_context": "设备A当前停机。", "evidence_quote": "设备A当前停机。"},
        {"item_id": "control-cross-item", "kind": "property", "entity": "设备B",
         "properties": {"状态": "停机"}, "entity_context": "设备B当前运行。", "evidence_quote": "设备B当前运行。"},
        {"item_id": "control-unsupported", "kind": "property", "entity": "设备C",
         "properties": {"转速": "99"}, "entity_context": "设备C转速为60。", "evidence_quote": "设备C转速为60。"},
    ]
    template = copy.deepcopy(cases[0]["old_messages"])
    user = next(m for m in template if m["role"] == "user")
    user["content"] = re.sub(r"(## candidates\n).*?(?=\n## )",
                             lambda m: m[1] + json.dumps(items, ensure_ascii=False), user["content"], count=1, flags=re.S)
    cases.append({"id": "synthetic-scope-control", "schema": VERIFICATION_SCHEMA, "old_messages": template,
                  "fixed_messages": fixed_messages(template, VERIFICATION_SCHEMA),
                  "labels": {"status": "synthetic explicit support/contradiction control; not historical human gold",
                             "expected": {items[0]["item_id"]: "yes", items[1]["item_id"]: "no", items[2]["item_id"]: "no"}}})
    manifest = []
    for case in cases:
        path = output / (case["id"] + ".json")
        path.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf8", newline="\n")
        manifest.append({"id": case["id"], "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    plan = [{"case": case["id"], "provider": provider, "arm": arm, "repeat": repeat}
            for repeat in (1, 2) for case in cases for provider in ("matrix-deepseek", "matrix-qwen35")
            for arm in (("old", "fixed") if repeat == 1 else ("fixed", "old"))]
    (output / "manifest.json").write_text(json.dumps({"cases": manifest, "planned_calls": 24,
           "execution": "not started; wait for main matrix and infrastructure reruns",
           "max_output_tokens": 8192, "temperature": 0}, ensure_ascii=False, indent=2), encoding="utf8")
    (output / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf8")
    return manifest, plan


def audit(case, text):
    rows = records(text)
    expected_ids = {i["item_id"] for i in section(case["old_messages"], "candidates")}
    validator = Draft202012Validator(record_schema(case["schema"]))
    valid = [r for r in rows if not list(validator.iter_errors(r))]
    actual = {r["item_id"]: r.get("supported") for r in valid if r["item_id"] in expected_ids}
    controls = case.get("labels", {}).get("expected", {})
    return {"records": len(rows), "schema_valid": len(valid), "expected_items": len(expected_ids),
            "verdict_counts": {v: sum(r.get("supported") == v for r in valid) for v in ("yes", "no", "uncertain")},
            "missing_items": len(expected_ids - actual.keys()), "duplicate_item_ids": len(valid) - len({r["item_id"] for r in valid}),
            "control_correct": sum(actual.get(k) == v for k, v in controls.items()), "control_total": len(controls),
            "trajectory": expansion_trajectory(text), "historical_yes_is_not_groundtruth": True}


def replay(dataset, output, provider=None):
    import os
    import time
    from openai import OpenAI
    from backend.app.services.extraction_config import resolve_config, public_config
    from algorithm.knowledge_extract.request_runtime import ScheduledClient
    if not output.resolve().is_relative_to(Path("tmp").resolve()):
        raise ValueError("Replay artifacts must be under repository tmp/")
    output.mkdir(parents=True, exist_ok=True)
    for row in read(dataset / "plan.json"):
        if provider and row["provider"] != provider:
            continue
        target = output / ("-".join(str(row[k]) for k in ("case", "provider", "arm", "repeat")) + ".json")
        if target.exists():
            continue
        case = read(dataset / (row["case"] + ".json"))
        config = resolve_config({"version": "v3", "provider": row["provider"], "request_timeout": 180, "document_timeout": 900})
        if config["max_output_tokens"] != 8192:
            raise ValueError("Provider output budget differs from frozen 8192")
        credential = os.environ[config["api_key_env"]]
        client = ScheduledClient(OpenAI(api_key=credential, base_url=config["base_url"], max_retries=0), config)
        messages = case[row["arm"] + "_messages"]
        started = time.time(); text = ""; finish = None; usage = None; error = None
        try:
            response = client.chat.completions.create(model=config["model"], messages=messages, temperature=0,
                                                     max_tokens=8192, extra_body={"thinking": {"type": "disabled"}})
            text = response.choices[0].message.content or ""
            finish = response.choices[0].finish_reason
            usage = response.usage.model_dump() if response.usage else None
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": safe_error(exc, credential)}
        summary = {**row, "started_unix": started, "seconds": time.time() - started, "finish_reason": finish,
                   "usage": usage, "error": error, "audit": audit(case, text)}
        target.write_text(json.dumps({"public_config": public_config(config), "messages": messages,
                                      "response_text": text, "summary": summary}, ensure_ascii=False, indent=2), encoding="utf8")
        print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--dataset", type=Path, default=Path("test_data/repetition/verifier-scope-v1"))
    parser.add_argument("--replay-out", type=Path)
    parser.add_argument("--provider", choices=("matrix-deepseek", "matrix-qwen35"))
    args = parser.parse_args()
    if args.replay_out:
        replay(args.dataset, args.replay_out, args.provider)
    else:
        manifest, plan = freeze(args.trace, args.dataset)
        print(json.dumps({"cases": len(manifest), "planned_calls": len(plan), "executed_calls": 0}))
