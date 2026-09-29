"""Build per-domain benchmark ontologies from the frontend full ontology payload.

Usage:
  python -m algorithm.knowledge_extract.v2.build_benchmark_ontology --dry-run
  python -m algorithm.knowledge_extract.v2.build_benchmark_ontology --write

Frontend serialization is kept: property // comments, class definition notes,
note on link relation semantics, cardinalities and rdfs_subClassOf inheritance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

CLASS_RE = re.compile(r'^\s*class\s+"(?P<name>[^"]+)"\s*(?:as\s+(?P<as>\w+))?\s*<<(?P<alias>[^>]+)>>\s*(?P<brace>\{)?\s*$')
CLASS_PLAIN_RE = re.compile(r'^\s*class\s+"(?P<name>[^"]+)"\s*(?:as\s+(?P<as>\w+))?\s*<<(?P<alias>[^>]+)>>\s*$')
REL_RE = re.compile(r'^\s*"(?P<src>[^"]+)"\s*(?:"(?P<sc>[^"]*)"\s*)?-->\s*(?:"(?P<tc>[^"]*)"\s*)?"(?P<dst>[^"]+)"\s*:\s*(?P<label>\S+)\s*(?:<<(?P<alias>[^>]+)>>)?\s*$')
NOTE_CLASS_RE = re.compile(r"(?ms)^note right of\s+(?P<target>[^\n]+?)\s*\n(?P<body>.*?)^end note\s*$")
NOTE_LINK_RE = re.compile(r"(?ms)^note on link\s*\n(?P<body>.*?)^end note\s*$")
ALIAS_RE = re.compile(r"<<([^>]+)>>")


@dataclass
class ClassDef:
    name: str
    alias: str
    props: list[str] = field(default_factory=list)
    desc: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class RelationDef:
    src: str
    dst: str
    label: str
    alias: str
    src_card: str = ""
    dst_card: str = ""
    note: str = ""


@dataclass
class OntologyModel:
    classes: dict[str, ClassDef] = field(default_factory=dict)
    relations: list[RelationDef] = field(default_factory=list)
    inherit: list[tuple[str, str]] = field(default_factory=list)  # (child, parent)
    symbols: dict[str, str] = field(default_factory=dict)  # display/as/alias -> alias

    def resolve(self, value: str) -> str | None:
        return self.symbols.get(value.strip())


def parse_frontend(text: str) -> OntologyModel:
    model = OntologyModel()
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = CLASS_RE.match(line) or CLASS_PLAIN_RE.match(line)
        if m:
            alias = m.group("alias").strip()
            cdef = ClassDef(name=m.group("name").strip(), alias=alias)
            model.classes[alias] = cdef
            model.symbols[cdef.name] = alias
            model.symbols[alias] = alias
            if m.groupdict().get("as"):
                model.symbols[m.group("as")] = alias
            if m.groupdict().get("brace"):
                i += 1
                while i < len(lines) and lines[i].strip() != "}":
                    body = lines[i]
                    stripped = body.strip()
                    if stripped.startswith("-"):
                        cdef.props.append(body.rstrip())
                    elif stripped and not stripped.startswith("'"):
                        cdef.desc.append(body.rstrip())
                    i += 1
        i += 1
    # map each note-on-link to the relation line immediately above it
    note_by_relation_line: dict[int, str] = {}
    for note in NOTE_LINK_RE.finditer(text):
        start_line = text[: note.start()].count("\n") + 1
        prev = start_line - 1
        while prev >= 1 and not lines[prev - 1].strip():
            prev -= 1
        if prev >= 1 and REL_RE.match(lines[prev - 1]):
            note_by_relation_line[prev] = " ".join(p.strip() for p in note.group("body").splitlines() if p.strip())
    for line_no, line in enumerate(lines, start=1):
        m = REL_RE.match(line)
        if not m:
            continue
        alias = (m.group("alias") or m.group("label")).strip()
        src = model.resolve(m.group("src")) or m.group("src").strip()
        dst = model.resolve(m.group("dst")) or m.group("dst").strip()
        if alias.casefold().replace("_", "").replace(":", "") == "rdfssubclassof":
            model.inherit.append((src, dst))
            continue
        note = note_by_relation_line.get(line_no, "")
        model.relations.append(RelationDef(src=src, dst=dst, label=m.group("label"), alias=alias,
                                           src_card=m.group("sc") or "", dst_card=m.group("tc") or "", note=note))
    # class notes
    for m in NOTE_CLASS_RE.finditer(text):
        alias = model.resolve(m.group("target")) or m.group("target").strip()
        cdef = model.classes.get(alias)
        if cdef:
            cdef.note = " ".join(p.strip() for p in m.group("body").splitlines() if p.strip())
    return model


def seeds_from_dir(ontology_dir: Path) -> tuple[set[str], set[str], set[str]]:
    classes: set[str] = set()
    relations: set[str] = set()
    files: set[str] = set()
    for path in sorted(ontology_dir.glob("*")):
        if path.suffix.lower() not in {".puml", ".uml", ".plantuml"} or not path.is_file():
            continue
        files.add(path.name)
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "-->" in line:
                if "rdfs_subClassOf" in line or "rdfs:subClassOf" in line:
                    continue
                label = line.split(":", 1)[-1].strip() if ":" in line else ""
                tokens = label.split()
                if tokens:
                    relations.add(tokens[0])
                for alias in ALIAS_RE.findall(line):
                    relations.add(alias.strip())
                continue
            if "class " in line or line.strip().startswith("entity "):
                for alias in ALIAS_RE.findall(line):
                    for part in re.split(r",\s*", alias.strip()):
                        if part:
                            classes.add(part)
    return classes, relations, files


def select_subgraph(model: OntologyModel, seed_classes: set[str], seed_relations: set[str]) -> tuple[set[str], list[RelationDef], list[tuple[str, str]], list[str]]:
    missing = sorted(seed_classes - set(model.classes))
    reverse_inherit: dict[str, list[str]] = {}
    for child, parent in model.inherit:
        reverse_inherit.setdefault(child, []).append(parent)
    selected = {c for c in seed_classes if c in model.classes}
    # expand ancestors and endpoints of seed relations
    changed = True
    while changed:
        changed = False
        for c in list(selected):
            for parent in reverse_inherit.get(c, []):
                if parent not in selected:
                    selected.add(parent)
                    changed = True
        for rel in model.relations:
            if rel.alias in seed_relations and (rel.src in model.classes and rel.dst in model.classes):
                for endpoint in (rel.src, rel.dst):
                    if endpoint not in selected:
                        selected.add(endpoint)
                        changed = True
    # domain closure: seed classes + ancestors + endpoints of seed relations only.
    changed = True
    while changed:
        changed = False
        for c in list(selected):
            for parent in reverse_inherit.get(c, []):
                if parent not in selected:
                    selected.add(parent)
                    changed = True
    relations = [rel for rel in model.relations if rel.src in selected and rel.dst in selected]
    inherit = [(child, parent) for child, parent in model.inherit if child in selected and parent in selected]
    return selected, relations, inherit, missing


def emit(model: OntologyModel, selected: set[str], relations: list[RelationDef], inherit: list[tuple[str, str]], *, group: str, source_hash: str) -> str:
    out: list[str] = [f"@startuml {group}_Ontology_from_frontend",
                      f"' generated from tests/ontology.puml sha256={source_hash} group={group}",
                      "skinparam classAttributeIconSize 0", "skinparam linetype ortho", ""]
    for alias in sorted(selected, key=lambda a: model.classes[a].name):
        cdef = model.classes[alias]
        out.append(f'class "{cdef.name}" <<{alias}>> {{')
        out.extend(cdef.props)
        out.extend(cdef.desc)
        out.append("}")
        if cdef.note:
            out.extend([f'note right of "{cdef.name}"', cdef.note, "end note"])
        out.append("")
    for child, parent in sorted(inherit):
        out.append(f'"{child}" "1" --> "1" "{parent}" : rdfs_subClassOf <<rdfs_subClassOf>>')
    if inherit:
        out.append("")
    for rel in relations:
        key = rel.alias.casefold().replace("_", "")
        if key == "rdfssubclassof":
            src_card, dst_card = "0..*", "0..*"
        elif key in {"hasdevelopmentstage"}:
            src_card, dst_card = "1", "0..1"
        else:
            src_card, dst_card = "1", "0..*"
        sc = f'"{src_card}" '
        tc = f'"{dst_card}" '
        out.append(f'"{model.classes[rel.src].name}" {sc}--> {tc}"{model.classes[rel.dst].name}" : {rel.alias} <<{rel.alias}>>')
        if rel.note:
            out.extend(["note on link", rel.note, "end note"])
    out.append("")
    out.append("@enduml")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 tests/ontology.puml 生成按域 benchmark 本体")
    parser.add_argument("--source", default=str(Path(__file__).resolve().parent.parent / "tests" / "ontology.puml"))
    parser.add_argument("--benchmark", default=str(Path(__file__).resolve().parent.parent / "benchmark"))
    parser.add_argument("--backup-root", default=None)
    parser.add_argument("--groups", nargs="+", default=["standard", "official", "rule", "simple"])
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    source_path = Path(args.source)
    text = source_path.read_text(encoding="utf-8", errors="replace")
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    model = parse_frontend(text)
    benchmark = Path(args.benchmark)
    backup_root = Path(args.backup_root) if args.backup_root else source_path.parents[1] / "backups" / f"frontend_A_{datetime.now():%Y%m%d_%H%M%S}"
    plan = []
    for group in args.groups:
        group_dir = benchmark / group
        seed_classes: set[str] = set()
        seed_relations: set[str] = set()
        case_dirs = []
        for ontology_dir in sorted(group_dir.glob("*/ontology")):
            case_dir = ontology_dir.parent
            if not (case_dir / "docs").is_dir() and not (case_dir / "extract").is_dir():
                continue
            case_dirs.append(case_dir)
            c, r, _ = seeds_from_dir(ontology_dir)
            seed_classes |= c
            seed_relations |= r
        if group in {"rule", "simple"}:
            ontology_dir = group_dir / "ontology"
            if ontology_dir.is_dir():
                case_dirs.append(group_dir)
                c, r, _ = seeds_from_dir(ontology_dir)
                seed_classes |= c
                seed_relations |= r
        selected, relations, inherit, missing = select_subgraph(model, seed_classes, seed_relations)
        if group == "standard" and "Section" in selected and "Subclause" in selected:
            if not any(r.src == "Section" and r.dst == "Subclause" and r.alias == "hasSubClause" for r in relations):
                relations.append(RelationDef(src="Section", dst="Subclause", label="hasSubClause", alias="hasSubClause",
                                             note="语义说明：表示章包含条（本 benchmark 对本体 hasSubClause 域的补充）"))
        front_aliases = {rel.alias for rel in model.relations}
        missing_rels = sorted(seed_relations - front_aliases)
        plan.append((group, case_dirs, selected, relations, inherit, missing, missing_rels))
        if missing or missing_rels:
            print(f"[{group}] 前端缺少种子类={missing} 关系={missing_rels}，该域保持原文件不变")
        print(f"[{group}] seeds classes={len(seed_classes)} relations={len(seed_relations)} -> selected classes={len(selected)} relations={len(relations)} inherit={len(inherit)}")
    if not args.write:
        print("dry-run only; add --write to replace benchmark ontology dirs")
        return 0
    backup_root.mkdir(parents=True, exist_ok=True)
    manifest = {"source": str(source_path), "source_sha256_16": source_hash, "generated_at": datetime.now().isoformat(), "groups": {}}
    for group, case_dirs, selected, relations, inherit, missing, missing_rels in plan:
        if missing or missing_rels:
            manifest["groups"].setdefault(group, {"cases": [str(case_dir.name) for case_dir in case_dirs], "skipped": "missing frontend seeds", "missing_seeds": missing, "missing_relations": missing_rels})
            print(f"  [{group}] skipped, original ontology kept")
            continue
        content = emit(model, selected, relations, inherit, group=group, source_hash=source_hash)
        for case_dir in case_dirs:
            ontology_dir = case_dir / "ontology"
            backup_dir = backup_root / group / (case_dir.name if (case_dir / "docs").is_dir() else case_dir.name)
            if ontology_dir.exists():
                if backup_dir.exists():
                    shutil.rmtree(backup_dir)
                shutil.copytree(ontology_dir, backup_dir)
                for old in ontology_dir.glob("*"):
                    if old.is_file():
                        old.unlink()
            else:
                ontology_dir.mkdir(parents=True, exist_ok=True)
            target = ontology_dir / f"{group.capitalize()}_Ontology.puml"
            target.write_text(content, encoding="utf-8")
            manifest["groups"].setdefault(group, {"cases": [], "classes": len(selected), "relations": len(relations), "inherit": len(inherit), "missing_seeds": missing})
            manifest["groups"][group]["cases"].append(case_dir.name)
            print(f"  wrote {target}")
    (backup_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"backup/manifest: {backup_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())