"""Descriptive completeness counters; schema fill is not semantic accuracy."""
from collections import Counter


def completeness_metrics(entities, plan):
    empty, eligible, filled = Counter(), Counter(), Counter()
    for row in entities:
        type_id = row['type']
        properties = row.get('properties') or {}
        slots = {p.identifier for p in plan.class_specs[type_id].properties}
        values = {key for key, value in properties.items()
                  if key in slots and value is not None and value != '' and value != []}
        eligible[type_id] += len(slots)
        filled[type_id] += len(values)
        if slots and not values:
            empty[type_id] += 1
    total = sum(eligible.values())
    return {'entities_without_properties': sum(empty.values()),
            'empty_property_entities_by_type': dict(empty),
            'filled_property_slots': sum(filled.values()), 'eligible_property_slots': total,
            'schema_fill_rate': sum(filled.values()) / total if total else None,
            'by_type': {key: {'filled': filled[key], 'eligible': eligible[key]}
                        for key in sorted(eligible)}}
