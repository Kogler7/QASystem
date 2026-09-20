"""Ontology-driven extraction V3. Existing V2 callers remain unchanged."""
from .context import ExtractionChunk
from .harness import extract_detailed


def extract(chunks, ontology: str, **kwargs):
    """Return scored facts. Inspect status before using an incomplete result."""
    return extract_detailed(chunks, ontology, **kwargs)


__all__ = ['ExtractionChunk', 'extract', 'extract_detailed']
