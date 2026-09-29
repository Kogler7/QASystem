"""Deterministic validation and document scoped accumulation of V3 facts."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import math
import re
from typing import Any

from .schema import CompiledOntology

_PLACEHOLDERS = {"unknown", "unk", "n/a", "na", "none", "null", "无", "未知", "不详"}
_CAPS = {"direct": 1.0, "contextual": 0.79, "uncertain": 0.49}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip().casefold() in _PLACEHOLDERS
    return isinstance(value, (list, dict, tuple, set)) and not value


def _score(value: Any, support: str) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = 0.0
    if not math.isfinite(score):
        score = 0.0
    return max(0.0, min(score, _CAPS[support]))


def _value(value: Any, data_type: str) -> Any:
    """Lossless scalar conversion; never strip units or truncate decimals."""
    dtype = re.sub(r'\s+', '', data_type or '').casefold().rstrip('?')
    if dtype.endswith('[]') or dtype.startswith('list<'):
        inner = data_type[:-2] if dtype.endswith('[]') else data_type[data_type.index('<')+1:-1]
        if not isinstance(value, list):
            raise ValueError('expected array')
        return [_value(item, inner) for item in value if not _empty(item)]
    enum = re.fullmatch(r'(?:enum\s*)?\{(.+)\}', data_type.strip(), re.IGNORECASE)
    if enum or '|' in data_type:
        choices = [x.strip().strip('\"\x27') for x in re.split(r'[,|]', enum.group(1) if enum else data_type)]
        if str(value).strip() not in choices:
            raise ValueError('invalid enum')
        return str(value).strip()
    if dtype in {'int','integer','long','float','double','decimal','number'}:
        if isinstance(value, bool) or isinstance(value, (list, dict)):
            raise ValueError('expected number')
        if not re.fullmatch(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?', str(value).strip()):
            raise ValueError('expected lossless number')
        exact = Decimal(str(value).strip())
        if not exact.is_finite() or (dtype in {'int','integer','long'} and exact != exact.to_integral_value()):
            raise ValueError('invalid number')
        if dtype in {'int','integer','long'} or exact == exact.to_integral_value():
            return int(exact)
        number = float(exact)
        # JSON has no Decimal type. Preserve a numeric string when a Python
        # float cannot round-trip the exact decimal instead of changing value.
        return number if math.isfinite(number) and Decimal(str(number)) == exact else str(exact)
    if dtype in {'bool','boolean'}:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().casefold() in {'true','false'}:
            return value.strip().casefold() == 'true'
        raise ValueError('expected boolean')
    if dtype in {'object','json','dict','map'}:
        if not isinstance(value, dict):
            raise ValueError('expected object')
        return {k:v for k,v in value.items() if not _empty(v)}
    if isinstance(value, (dict,list)):
        raise ValueError('expected scalar')
    return str(value).strip()


def _locator(refs: list[dict], windows: list[dict]) -> str:
    quotes = [ref for ref in refs if isinstance(ref.get('quote'), str) and ref['quote'].strip()]
    if not quotes:
        return 'approximate'
    for ref in quotes:
        texts = [w.get('text','') for w in windows if w.get('chunk_id') == ref['chunk_id']]
        if any(ref['quote'] in text for text in texts):
            continue
        return 'approximate' if any(re.sub(r'\s+','',ref['quote']) in re.sub(r'\s+','',text) for text in texts) else 'unresolved'
    return 'exact'


RELATION_FIELDS = ('from_type', 'from_name', 'rel_type', 'to_type', 'to_name')


def _key(entity):
    return entity['type'], entity['name']


def _merge_metadata(existing, incoming, *, conservative=False):
    """Merge already validated facts, retaining first scalar values."""
    conflicts = []
    props = existing.setdefault('properties', {})
    scores = existing.setdefault('property_confidence', {})
    for name, value in incoming.get('properties', {}).items():
        if name not in props:
            props[name] = deepcopy(value)
            scores[name] = incoming.get('property_confidence', {}).get(name, incoming.get('confidence', 0))
        elif props[name] != value:
            alternatives = existing.setdefault('alternatives', [])
            if not any(item['property'] == name and item['value'] == value for item in alternatives):
                alternatives.append({'property': name, 'value': deepcopy(value),
                                     'source_refs': deepcopy(incoming.get('source_refs', []))})
            conflicts.append(name)
        elif conservative and name in incoming.get('property_confidence', {}):
            scores[name] = min(scores.get(name, 0), incoming['property_confidence'][name])
    for alternative in incoming.get('alternatives', []):
        if alternative not in existing.setdefault('alternatives', []):
            existing['alternatives'].append(deepcopy(alternative))
    for ref in incoming.get('source_refs', []):
        if ref not in existing.setdefault('source_refs', []):
            existing['source_refs'].append(deepcopy(ref))
    if conservative:
        existing['confidence'] = min(existing.get('confidence', 0), incoming.get('confidence', 0))
        supports = (existing.get('support', 'direct'), incoming.get('support', 'direct'))
        existing['support'] = min(supports, key=lambda value: _CAPS.get(value, 0))
    return conflicts


def _coalesce_relations(relations):
    by_key = {}
    for incoming in relations:
        key = tuple(incoming[field] for field in RELATION_FIELDS)
        if key not in by_key:
            by_key[key] = deepcopy(incoming)
        else:
            existing = by_key[key]
            for ref in incoming.get('source_refs', []):
                if ref not in existing.setdefault('source_refs', []):
                    existing['source_refs'].append(deepcopy(ref))
            # Repeated assertions never amplify support.
            existing['confidence'] = min(existing.get('confidence', 0), incoming.get('confidence', 0))
    return list(by_key.values())


class Registry:
    """Entities are addressed exclusively by their ontology type and name."""

    def __init__(self, schema_compiled, document_id):
        self.compiled = schema_compiled
        self.schema = getattr(schema_compiled, 'schema', schema_compiled)
        self.document_id = document_id or ''
        self.entities = []
        self.relations = []
        self._entities = {}

    @staticmethod
    def _issue(path, code, message):
        return {'path': path, 'code': code, 'message': message}

    def _refs(self, raw, windows, path, issues):
        allowed = {}
        for window in windows:
            allowed.setdefault(str(window.get('chunk_id')), []).append(window)
        values = raw if isinstance(raw, list) else []
        if not values:
            values = [{key: window[key] for key in ('chunk_id','start_offset','end_offset') if key in window}
                      for window in windows if window.get('chunk_id') is not None]
        if not values:
            issues.append(self._issue(path, 'MISSING_SOURCE_REF', 'a real input source window is required'))
        refs = []
        for index, ref in enumerate(values):
            chunk_id = str(ref.get('chunk_id', '')) if isinstance(ref, dict) else ''
            candidates = allowed.get(chunk_id, [])
            if not candidates or any(scope and self.document_id and str(scope) != self.document_id
                                     for scope in (candidates[0].get('document_id'), ref.get('document_id'))):
                issues.append(self._issue(f'{path}[{index}]', 'UNKNOWN_SOURCE_REF', 'source ref is not in this document observation'))
                continue
            quote = ref.get('quote') if isinstance(ref.get('quote'),str) else None
            exact = [window for window in candidates if quote and quote in window.get('text','')]
            ranged = [window for window in candidates if 'start_offset' in ref and window.get('start_offset') == ref['start_offset']]
            chosen = exact or ranged or [window for window in candidates if not window.get('context_only')] or candidates
            for window in chosen:
                located = {key: window[key] for key in ('chunk_id','document_id','start_offset','end_offset','document_start_offset','page_no','section_id') if window.get(key) is not None}
                if quote:
                    located['quote'] = quote
                if located not in refs:
                    refs.append(located)
        return refs

    def apply(self, payload, source_windows):
        issues = []
        if not isinstance(payload, dict):
            return [self._issue('', 'INVALID_PAYLOAD', 'payload must be an object')]
        if any(key in payload for key in ('delete', 'deletions', 'remove', 'merges')):
            return [self._issue('', 'UNAUTHORIZED_EDIT', 'only the separate review operation can merge existing facts')]
        raw_entities = payload.get('entities', [])
        if not isinstance(raw_entities, list):
            issues.append(self._issue('entities', 'INVALID_ARRAY', 'entities must be an array'))
            raw_entities = []
        for index, raw in enumerate(raw_entities):
            path = f'entities[{index}]'
            if not isinstance(raw, dict):
                issues.append(self._issue(path, 'INVALID_ENTITY', 'entity must be an object'))
                continue
            if any(field in raw for field in ('id', 'from_id', 'to_id', 'replaces_id', 'delete')):
                issues.append(self._issue(path, 'UNSUPPORTED_ID', 'use type and name; entity IDs and replacement operations are not supported'))
                continue
            cls = self.schema.resolve_class(raw.get('type'))
            name = _text(raw.get('name'))
            if cls is None:
                issues.append(self._issue(path+'.type', 'UNKNOWN_ENTITY_TYPE', 'type must be in the ontology'))
                continue
            if not name or name.casefold() in _PLACEHOLDERS:
                issues.append(self._issue(path+'.name', 'INVALID_ENTITY_NAME', 'name must be nonempty'))
                continue
            support = _text(raw.get('support')).casefold() or 'direct'
            if support not in _CAPS:
                issues.append(self._issue(path+'.support', 'UNSUPPORTED_SUPPORT', 'unsupported facts are not accepted'))
                continue
            if raw.get('properties') is not None and not isinstance(raw['properties'], dict):
                issues.append(self._issue(path+'.properties', 'INVALID_PROPERTIES', 'properties must be an object'))
                continue
            properties, scores = {}, {}
            for key, value in (raw.get('properties') or {}).items():
                prop = self.schema.resolve_property(cls.identifier, key)
                if prop is None:
                    issues.append(self._issue(path+'.properties.'+str(key), 'UNKNOWN_PROPERTY', 'property must be in the ontology'))
                    continue
                if _empty(value):
                    continue
                try:
                    value = _value(value, prop.data_type)
                except (ValueError, TypeError, OverflowError):
                    issues.append(self._issue(path+'.properties.'+str(key), 'INVALID_PROPERTY_VALUE', f'expected ontology value type {prop.data_type}'))
                    continue
                if _empty(value):
                    continue
                properties[prop.identifier] = value
                raw_scores = raw.get('property_confidence')
                score = raw_scores.get(key, raw.get('confidence', 0)) if isinstance(raw_scores, dict) else raw.get('confidence', 0)
                scores[prop.identifier] = _score(score, support)
            previous_errors = len(issues)
            refs = self._refs(raw.get('source_refs'), source_windows, path+'.source_refs', issues)
            if len(issues) != previous_errors:
                continue
            candidate = {'type': cls.identifier, 'name': name, 'properties': properties,
                         'property_confidence': scores, 'confidence': _score(raw.get('confidence', 0), support),
                         'support': support, 'source_refs': refs, 'locator_status': _locator(refs, source_windows),
                         'alternatives': []}
            key = _key(candidate)
            if key not in self._entities:
                self.entities.append(candidate)
                self._entities[key] = candidate
            else:
                for prop in _merge_metadata(self._entities[key], candidate):
                    issues.append(self._issue(path+'.properties.'+prop, 'PROPERTY_CONFLICT', 'first value retained; alternative recorded'))
        raw_relations = payload.get('relations', [])
        if not isinstance(raw_relations, list):
            issues.append(self._issue('relations', 'INVALID_ARRAY', 'relations must be an array'))
            raw_relations = []
        for index, raw in enumerate(raw_relations):
            path = f'relations[{index}]'
            if not isinstance(raw, dict):
                issues.append(self._issue(path, 'INVALID_RELATION', 'relation must be an object'))
                continue
            if any(field in raw for field in ('id', 'from_id', 'to_id', 'replaces_id', 'delete')):
                issues.append(self._issue(path, 'UNSUPPORTED_ID', 'use the five type/name relation fields'))
                continue
            source_type = self.schema.resolve_class(raw.get('from_type'))
            target_type = self.schema.resolve_class(raw.get('to_type'))
            source = (source_type.identifier, _text(raw.get('from_name'))) if source_type else None
            target = (target_type.identifier, _text(raw.get('to_name'))) if target_type else None
            signatures = self.schema.resolve_relation(_text(raw.get('rel_type')))
            if source not in self._entities or target not in self._entities or not signatures:
                issues.append(self._issue(path, 'INVALID_RELATION', 'both named endpoints must exist; relation must be in ontology'))
                continue
            valid = [signature for signature in signatures if self.schema.is_assignable(source[0], signature.source)
                     and self.schema.is_assignable(target[0], signature.target)]
            if not valid:
                allowed = [(signature.source, signature.identifier, signature.target) for signature in signatures]
                issues.append(self._issue(path, 'INVALID_RELATION_DIRECTION', f'actual {source[0]} -> {target[0]}; allowed {allowed}'))
                continue
            support = _text(raw.get('support')).casefold() or 'direct'
            if support not in _CAPS:
                issues.append(self._issue(path, 'UNSUPPORTED_SUPPORT', 'unsupported relation'))
                continue
            previous_errors = len(issues)
            refs = self._refs(raw.get('source_refs'), source_windows, path+'.source_refs', issues)
            if len(issues) != previous_errors:
                continue
            relation = dict(zip(RELATION_FIELDS, (source[0], source[1], valid[0].identifier, target[0], target[1])))
            relation.update(confidence=_score(raw.get('confidence', 0), support), support=support,
                            source_refs=refs, locator_status=_locator(refs, source_windows))
            self.relations.append(relation)
        self.relations = _coalesce_relations(self.relations)
        return issues

    def apply_merges(self, groups, min_confidence=.8):
        """Validate each semantic merge atomically before rewriting any facts."""
        if not isinstance(groups, list):
            return [self._issue('merges', 'INVALID_MERGE', 'merges must be an array')]
        issues = []
        for index, group in enumerate(groups):
            path = f'merges[{index}]'
            if not isinstance(group, dict) or not isinstance(group.get('canonical'), dict) or not isinstance(group.get('members'), list):
                issues.append(self._issue(path, 'INVALID_MERGE', 'canonical and members must be type/name references'))
                continue
            try:
                confidence = float(group.get('confidence', 0))
            except (TypeError, ValueError):
                confidence = 0
            if not math.isfinite(confidence) or not min_confidence <= confidence <= 1:
                issues.append(self._issue(path, 'LOW_MERGE_CONFIDENCE', 'merge confidence must be between threshold and 1'))
                continue
            canonical = (_text(group['canonical'].get('type')), _text(group['canonical'].get('name')))
            keys = [canonical]
            for member in group['members']:
                if isinstance(member, dict):
                    key = (_text(member.get('type')), _text(member.get('name')))
                    if key not in keys:
                        keys.append(key)
                else:
                    keys.append(('', ''))
            if len(keys) < 2 or any(key not in self._entities or key[0] != canonical[0] for key in keys):
                issues.append(self._issue(path, 'INVALID_MERGE', 'all members must exist and share the canonical type'))
                continue
            members = [self._entities[key] for key in keys]
            union = {}
            conflict = False
            for member in members:
                observations = list(member['properties'].items())
                observations.extend((item['property'], item['value'])
                                    for item in member.get('alternatives', []))
                for prop, value in observations:
                    if prop in union and union[prop] != value:
                        conflict = True
                    union[prop] = value
            if conflict:
                issues.append(self._issue(path, 'MERGE_PROPERTY_CONFLICT', 'different nonempty attribute values cannot be merged'))
                continue
            rewrites = []
            creates_loop = False
            for relation in self.relations:
                source = (relation['from_type'], relation['from_name'])
                target = (relation['to_type'], relation['to_name'])
                new_source = canonical if source in keys else source
                new_target = canonical if target in keys else target
                if source != target and new_source == new_target:
                    creates_loop = True
                    break
                changed = deepcopy(relation)
                changed.update(from_type=new_source[0], from_name=new_source[1], to_type=new_target[0], to_name=new_target[1])
                rewrites.append(changed)
            if creates_loop:
                issues.append(self._issue(path, 'MERGE_NEW_SELF_LOOP', 'merge would turn a relation between distinct entities into a self-loop'))
                continue
            target = self._entities[canonical]
            for key in keys[1:]:
                member = self._entities.pop(key)
                _merge_metadata(target, member, conservative=True)
                self.entities.remove(member)
            self.relations = _coalesce_relations(rewrites)
        return issues

    def known_entities(self):
        return [{field: deepcopy(entity[field]) for field in ('type', 'name', 'properties')} for entity in self.entities]

    def public_result(self):
        return {'entities': self.known_entities(),
                'relations': [{field: relation[field] for field in RELATION_FIELDS} for relation in self.relations]}

    def facts(self):
        return {'entities': deepcopy(self.entities), 'relations': deepcopy(self.relations)}


def coalesce_results(results):
    """Coalesce trusted per-document results, without inventing cross-document links."""
    entities, relations = {}, []
    for result in results:
        for incoming in result.get('entities', []):
            key = _key(incoming)
            if key not in entities:
                entities[key] = deepcopy(incoming)
            else:
                _merge_metadata(entities[key], incoming)
        relations.extend(result.get('relations', []))
    return {'entities': list(entities.values()), 'relations': _coalesce_relations(relations)}


def display_property_names(facts, schema):
    """Match the existing public contract while retaining IDs inside validation."""
    result = deepcopy(facts)
    for entity in result['entities']:
        labels = {}
        for key in entity['properties']:
            prop = schema.resolve_property(entity['type'], key)
            labels[key] = prop.display_name if prop else key
        # Ambiguous display labels must not silently overwrite distinct values.
        if len(set(labels.values())) != len(labels):
            labels = {key:key for key in labels}
        entity['properties'] = {labels[key]:value for key,value in entity['properties'].items()}
        entity['property_confidence'] = {labels.get(key,key):value for key,value in entity.get('property_confidence',{}).items()}
        for alternative in entity.get('alternatives', []):
            prop = schema.resolve_property(entity['type'], alternative['property'])
            alternative['property'] = labels.get(alternative['property'], prop.display_name if prop else alternative['property'])
    return result
