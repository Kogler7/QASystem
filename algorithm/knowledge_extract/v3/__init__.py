"""Ontology-driven extraction V3. Existing V2 callers remain unchanged."""
from .context import ExtractionChunk
from .harness import extract_detailed


def extract(chunks, ontology: str, **kwargs):
    """Return scored facts. Inspect status before using an incomplete result."""
    return extract_detailed(chunks, ontology, **kwargs)


# Import adapter after ``extract_detailed`` is defined so the adapter can re-export
# the package-level symbol; this also lets tests monkeypatch
# ``algorithm.knowledge_extract.v3.extract_detailed`` and affect ``extract_aggregate``.
from .adapter import extract_aggregate


__all__ = ['ExtractionChunk', 'extract', 'extract_aggregate', 'extract_detailed']
