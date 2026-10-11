"""Freeze lossless JSON/JSONL/adaptive variants through the existing Backend."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil

from eval import run


TABLE_FENCE = re.compile(r'(?m)^([ \t]*```json[ \t]*\r?\n)(.*?)(^[ \t]*```[ \t]*$)', re.DOTALL | re.IGNORECASE)


def convert_record_tables(markdown: str) -> tuple[str, int]:
    """Change serialization only: keep every field/value/row and surrounding prose."""
    converted = 0
    def replace(match):
        nonlocal converted
        try:
            table = json.loads(match.group(2))
        except ValueError:
            return match.group(0)
        if not isinstance(table, dict) or not isinstance(table.get('columns'), list):
            return match.group(0)
        rows = table.get('rows')
        if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
            return match.group(0)
        # Reject ancillary table-level fields rather than silently discard them.
        if set(table) != {'columns', 'rows'}:
            raise ValueError('record table has extra fields; requires an explicit migration')
        converted += 1
        body = '\n'.join(json.dumps(row, ensure_ascii=False, separators=(',', ':')) for row in rows)
        return '```jsonl\n' + body + '\n' + match.group(3)
    return TABLE_FENCE.sub(replace, markdown), converted


def validate_ranges(markdown: str, items: list[dict]) -> None:
    if not items:
        raise ValueError('no chunk items')
    previous = -1
    covered_end = 0
    for item in items:
        span = item.get('range')
        if (not isinstance(span, list) or len(span) != 2 or any(type(v) is not int for v in span)
                or not 0 <= span[0] <= span[1] <= len(markdown)
                or span[0] < previous or markdown[span[0]:span[1]] != item.get('text')):
            raise ValueError('chunk text/source range mismatch')
        previous = span[0]
        if span[0] > covered_end:
            raise ValueError('chunk source range coverage gap')
        covered_end = max(covered_end, span[1])
    if covered_end != len(markdown):
        raise ValueError('chunk source range coverage gap')


def prepare(source: Path, destination: Path, cases: list[str], params: dict,
            variant: str, source_commit: str) -> list[dict]:
    if destination.exists():
        raise ValueError('refuse to overwrite frozen inputs')
    records = []
    for label in cases:
        case = source / label
        output = destination / label
        shutil.copytree(case, output, ignore=shutil.ignore_patterns('chunks.json', 'attempts', 'backend_*', 'scores.json'))
        original = (case / 'document.md').read_text(encoding='utf8')
        markdown, tables = (convert_record_tables(original) if variant != 'json' else (original, 0))
        run.write_text(output / 'document.md', markdown)
        options = deepcopy(params.get('options') or {})
        chunking = options.setdefault('chunking', {})
        chunking.pop('chunk_threshold', None)
        chunking.update(adaptive_entity_count=variant == 'jsonl-adaptive', entity_count_model='clef-flash')
        items, result = run._chunk_markdown(params['backend_url'], markdown, case.name,
                                          options, params, output)
        if result['status'] != 'succeeded':
            raise RuntimeError(f'chunking failed for {label}; no whole-document fallback allowed')
        validate_ranges(markdown, items)
        row = {'case': label, 'variant': variant, 'backend_source_commit': source_commit,
               'converted_tables': tables, 'original_chars': len(original), 'markdown_chars': len(markdown),
               'source_markdown_sha256': hashlib.sha256(original.encode()).hexdigest(),
               'markdown_sha256': hashlib.sha256(markdown.encode()).hexdigest(),
               'chunks_sha256': hashlib.sha256((output/'chunks.json').read_bytes()).hexdigest(),
               'chunk_count': len(items), 'chunk_sizes': [len(item['text']) for item in items],
               'chunk_ranges': [item['range'] for item in items], 'chunking_options': chunking,
               'chunk_seconds': result['elapsed_seconds']}
        records.append(row)
        run.write_json(destination / 'prepared.json', records)
        run.log(f"Frozen {variant}/{label}: {len(items)} chunks, {tables} tables converted")
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--params', type=Path, required=True)
    parser.add_argument('--cases', nargs='+', required=True)
    parser.add_argument('--variant', choices=['json', 'jsonl', 'jsonl-adaptive'], required=True)
    parser.add_argument('--source-commit', required=True)
    args = parser.parse_args()
    if 'tmp' not in args.output.resolve().parts or any(not (args.source / c).resolve().is_relative_to(args.source.resolve()) for c in args.cases):
        parser.error('inputs must stay in source; outputs must stay under tmp')
    prepare(args.source, args.output, args.cases, run.load_params(args.params), args.variant, args.source_commit)


if __name__ == '__main__':
    main()
