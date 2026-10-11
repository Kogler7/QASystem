import copy
import importlib
import json
import re
from types import SimpleNamespace
from types import ModuleType
import sys

import pytest

from algorithm.knowledge_extract.verification_scope import verification_scope


def payload(messages):
    return json.loads(re.search(r"## current chunk\n(.*?)\n## candidates", messages[1]["content"], re.S)[1])


@pytest.mark.parametrize("version", ["v3", "latest"])
def test_scoped_authority_is_consistent_and_does_not_expose_whole_chunk(version):
    module = importlib.import_module(f"algorithm.knowledge_extract.{version}.semantic_evidence")
    chunk = SimpleNamespace(chunk_id="c", document_id="d", text="whole chunk secret")
    items = [{"item_id": "one", "entity_context": "A的锚定原文"},
             {"item_id": "two", "entity_context": "B的锚定原文"}]
    snapshot = copy.deepcopy(items)
    messages = module.build_verification_messages(chunk=chunk, items=items)
    assert payload(messages)["text"] == ""
    assert payload(messages)["source_scope"] == "per_item_entity_context"
    assert "whole chunk secret" not in str(messages)
    assert "占位" not in str(messages)
    for message in messages:
        assert "entity_context" in message["content"]
        assert "不得借用其他 item" in message["content"]
    assert "只依据给定的 current chunk 原文核验" not in messages[0]["content"]
    assert "该候选的授权原文能支持" in messages[0]["content"]
    assert items == snapshot


@pytest.mark.parametrize("items", [[], [{"item_id": "one"}], [{"entity_context": "  "}]])
def test_empty_or_unscoped_keeps_original_chunk_contract(items):
    chunk = SimpleNamespace(chunk_id="c", document_id="d", text="完整原文")
    result, instruction = verification_scope(chunk, items)
    assert result == {"chunk_id": "c", "document_id": "d", "text": "完整原文"}
    assert instruction == ""


def test_mixed_context_authority_is_per_item_and_fallback_is_explicit():
    chunk = SimpleNamespace(chunk_id="c", document_id="d", text="完整原文")
    result, instruction = verification_scope(chunk, [{"entity_context": "A范围"}, {"item_id": "B"}])
    assert result["text"] == "完整原文"
    assert "未提供非空 entity_context 时，才依据 current chunk.text" in instruction
    assert "不得借用其他 item" in instruction


@pytest.mark.parametrize("version", ["v2", "v2_2", "v3_1"])
def test_other_versions_keep_existing_full_chunk_contract(version):
    module = importlib.import_module(f"algorithm.knowledge_extract.{version}.semantic_evidence")
    messages = module.build_verification_messages(
        chunk=SimpleNamespace(chunk_id="c", document_id="d", text="完整原文"),
        items=[{"item_id": "one", "entity_context": "范围"}])
    assert payload(messages)["text"] == "完整原文"


@pytest.mark.parametrize("version", ["v3", "latest"])
def test_loaded_verifier_does_not_lazily_reimport_under_temporary_parent_stub(version, monkeypatch):
    module = importlib.import_module(f"algorithm.knowledge_extract.{version}.semantic_evidence")
    monkeypatch.setitem(sys.modules, "algorithm.knowledge_extract", ModuleType("algorithm.knowledge_extract"))
    messages = module.build_verification_messages(
        chunk=SimpleNamespace(chunk_id="c", document_id="d", text="完整原文"),
        items=[{"item_id": "one", "entity_context": "锚定原文"}])
    assert payload(messages)["source_scope"] == "per_item_entity_context"
    assert "不得借用其他 item" in messages[0]["content"]
