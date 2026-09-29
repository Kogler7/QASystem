"""V3 ontology contract, backed by the established V2 PlantUML parser."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    from ..v2.ontology_schema import OntologySchema
except ImportError:
    from algorithm.knowledge_extract.v2.ontology_schema import OntologySchema


@dataclass(frozen=True)
class CompiledOntology:
    source: str
    schema: OntologySchema
    prompt_contract: dict[str, Any]


def compile_ontology(text: str) -> CompiledOntology:
    """Compile an ontology into the compact, provider independent V3 contract."""
    source = text or ""
    parsed = OntologySchema.parse(source)
    if not parsed.classes:
        raise ValueError("ontology contains no classes")
    if parsed.unresolved:
        raise ValueError(f"ontology contains unresolved declarations: {parsed.unresolved[0]}")
    classes = []
    for cls in parsed.classes:
        properties = []
        for prop in parsed._properties_for(cls.identifier):
            properties.append({
                "id": prop.identifier,
                "name": prop.display_name,
                "type": prop.data_type,
                "description": prop.comment,
            })
        classes.append({
            "id": cls.identifier,
            "name": cls.display_name,
            "identity": list(parsed.identity_properties_for(cls.identifier)),
            "properties": properties,
            "parents": sorted(parsed.ancestors(cls.identifier)),
        })
    relations = [{
        "id": rel.identifier,
        "from": rel.source,
        "to": rel.target,
        "source_cardinality": rel.source_cardinality,
        "target_cardinality": rel.target_cardinality,
    } for rel in parsed.relations]
    contract = {
        "source_definition": source,
        "classes": classes,
        "relations": relations,
        "output": {
            "entities": "array",
            "relations": "array",
            "supports": ["direct", "contextual", "uncertain"],
        },
    }
    return CompiledOntology(source=source, schema=parsed, prompt_contract=contract)
