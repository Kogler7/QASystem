"""Public façade for the *graph_ops* package.

The sub-modules are imported eagerly so `graph_ops.` users see a flat API
layer, e.g.::

    from graph_ops import compute_affected_pairs, expand_nodes

If the modules live at the top-level (because the repo hasn’t been
re-structured yet) the fallback `ImportError` handler dynamically loads
them so the import still works.
"""

"""
Graph Operations Module for UpSHACL-Neo4j.
Exposes CRUD, Traversal, and Delta calculation logic.
"""

# src/graph_ops/__init__.py
"""
Graph Operations Module for UpSHACL-Neo4j.
"""

# 直接显式导入，拒绝动态加载的坑 [cite: 2026-03-09]
from .crud import (
    batch,
    cleanup_temp_graphs,
    count_triples_in_graph,
    sample_triples_from_graph,
    insert_triples,
    delete_triples,
)
from .traversal import is_valid_uri, expand_nodes, expand_shapes
from .delta import compute_affected_pairs, build_reduced_graphs

__all__ = [
    "batch",
    "cleanup_temp_graphs",
    "count_triples_in_graph",
    "sample_triples_from_graph",
    "insert_triples",
    "delete_triples",
    "is_valid_uri",
    "expand_nodes",
    "expand_shapes",
    "compute_affected_pairs",
    "build_reduced_graphs",
]