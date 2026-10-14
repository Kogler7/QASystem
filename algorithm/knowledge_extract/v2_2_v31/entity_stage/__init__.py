"""Vendored copy of v3.1 entity extraction for v2.2-v31.

Runtime does **not** import ``algorithm.knowledge_extract.v3_1``.
Re-sync this tree when v3.1 entity behavior changes.
"""
from .split_extractor import extract_split

__all__ = ["extract_split"]
