import re
from datetime import datetime
from pathlib import Path
from typing import Any


UML_DIR = Path(__file__).resolve().parent / "UML"

_MAIN_OBJECT_FILE = {
    "Term": "Term.puml",
    "RuleType": "RuleType.puml",
    "SystemElement": "SystemElement.puml",
    "Glossary": "Term.puml",
}

_CLASS_DECL_RE = re.compile(r"^\s*(?:abstract\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)\b")


def _safe_stem(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_\-]", "_", (name or "").strip())
    return cleaned or "uploaded"


def _normalize_main_object(name: str) -> str:
    return _safe_stem(name).strip("_") or "MainObject"


def list_main_objects_from_uml() -> list[dict[str, str]]:
    UML_DIR.mkdir(parents=True, exist_ok=True)
    out: list[dict[str, str]] = []
    for path in sorted(UML_DIR.glob("*.puml"), key=lambda p: p.name.lower()):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        classes = []
        for line in text.splitlines():
            m = _CLASS_DECL_RE.match(line)
            if m:
                classes.append(m.group(1))
        stem = path.stem
        preferred = next((c for c in classes if c.lower() == stem.lower()), None)
        main_object = preferred or (classes[0] if classes else stem)
        out.append(
            {
                "main_object": main_object,
                "file_name": path.name,
            }
        )
    return out


def _extract_attr(line: str) -> tuple[str, str] | None:
    text = line.strip()
    if not text or text.startswith("'"):
        return None
    if ":" not in text:
        return None
    left, right = text.split(":", 1)
    left = left.strip()
    right = right.strip()
    if not left or not right:
        return None
    # 去掉 UML 可见性符号，保留属性名；属性值取冒号后所有内容。
    left = re.sub(r"^[+\-#~]\s*", "", left).strip()
    if not left:
        return None
    return left, right


def _relation_type_from_label_or_op(label: str | None, op: str) -> str:
    if label:
        return re.sub(r"[^A-Za-z0-9]+", "_", label.strip()).strip("_").upper() or "RELATED"
    if "<|--" in op or "--|>" in op:
        return "INHERITS"
    if "*--" in op or "--*" in op:
        return "COMPOSITION"
    if "o--" in op or "--o" in op:
        return "AGGREGATION"
    if ".." in op:
        return "DEPENDENCY"
    return "ASSOCIATED_WITH"


def _relation_direction(from_name: str, to_name: str, op: str) -> tuple[str, str]:
    # 箭头指向左：源是右侧；箭头指向右：源是左侧；无方向时默认左到右。
    if any(token in op for token in ("<|--", "<--", "<..", "<|..")):
        return to_name, from_name
    if any(token in op for token in ("-->", "..>", "--|>", "..|>")):
        return from_name, to_name
    return from_name, to_name


def parse_uml_to_graph(uml_text: str) -> dict[str, Any]:
    classes: dict[str, dict[str, Any]] = {}
    relationships: list[dict[str, str]] = []

    lines = uml_text.splitlines()
    i = 0
    relation_decl = re.compile(
        r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:"[^"]*")?\s*([<>\-|.*o]+)\s*(?:"[^"]*")?\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*(.+))?$'
    )

    while i < len(lines):
        line = lines[i]
        class_match = _CLASS_DECL_RE.match(line)
        if class_match:
            class_name = class_match.group(1)
            attrs: dict[str, str] = {}
            if "{" in line:
                i += 1
                while i < len(lines):
                    row = lines[i]
                    if "}" in row:
                        break
                    pair = _extract_attr(row)
                    if pair:
                        k, v = pair
                        attrs[k] = v
                    i += 1
            node_props: dict[str, Any] = {
                "name": class_name,
                "tags": ["Class", class_name],
            }
            # class 内属性直接作为节点属性，便于按字段查询
            for attr_name, attr_value in attrs.items():
                node_props[attr_name] = attr_value

            classes[class_name] = {
                "uid": f"Class::{class_name.lower()}",
                "label": class_name,
                "labels": ["Class", class_name],
                "properties": node_props,
            }
            i += 1
            continue

        rel_match = relation_decl.match(line)
        if rel_match:
            left = rel_match.group(1)
            op = rel_match.group(2) or "--"
            right = rel_match.group(3)
            rel_label = (rel_match.group(4) or "").strip() or None
            from_name, to_name = _relation_direction(left, right, op)
            relationships.append(
                {
                    "from_uid": f"Class::{from_name.lower()}",
                    "to_uid": f"Class::{to_name.lower()}",
                    "type": _relation_type_from_label_or_op(rel_label, op),
                }
            )
        i += 1

    return {
        "raw": {
            "classes": [
                {
                    "name": node["properties"]["name"],
                    "tags": node["properties"]["tags"],
                    "properties": node["properties"],
                }
                for node in classes.values()
            ],
            "relationships": relationships,
        },
        "graph": {
            "nodes": list(classes.values()),
            "relationships": relationships,
            "ontology_relations": [],
        },
    }


def _resolve_target_file_name(main_object: str) -> str:
    requested = (main_object or "").strip()
    if not requested:
        raise ValueError("主对象不能为空")
    for item in list_main_objects_from_uml():
        if item["main_object"].lower() == requested.lower():
            return item["file_name"]
    raise ValueError(f"未找到主对象: {requested}")


def save_uploaded_uml_and_parse(
    data: bytes,
    filename: str,
    main_object: str | None = None,
    new_main_object: str | None = None,
) -> dict[str, Any]:
    UML_DIR.mkdir(parents=True, exist_ok=True)
    text = data.decode("utf-8")
    stem = _safe_stem(Path(filename or "uploaded.puml").stem)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    saved_paths: list[str] = []

    archive_name = f"{stem}_{ts}.puml"
    archive_path = UML_DIR / archive_name
    archive_path.write_text(text, encoding="utf-8")
    saved_paths.append(str(Path("algorithm/uie/ie/UML") / archive_name).replace("\\", "/"))

    target_saved = None
    if (new_main_object or "").strip():
        normalized = _normalize_main_object(new_main_object or "")
        target_name = f"{normalized}.puml"
        target_path = UML_DIR / target_name
        target_path.write_text(text, encoding="utf-8")
        target_saved = str(Path("algorithm/uie/ie/UML") / target_name).replace("\\", "/")
    elif (main_object or "").strip():
        file_name = _resolve_target_file_name(main_object or "")
        target_path = UML_DIR / file_name
        target_path.write_text(text, encoding="utf-8")
        target_saved = str(Path("algorithm/uie/ie/UML") / file_name).replace("\\", "/")
    else:
        mapped = _MAIN_OBJECT_FILE.get((Path(filename).stem or "").strip())
        if mapped:
            target_path = UML_DIR / mapped
            target_path.write_text(text, encoding="utf-8")
            target_saved = str(Path("algorithm/uie/ie/UML") / mapped).replace("\\", "/")

    if target_saved:
        saved_paths.append(target_saved)

    parsed = parse_uml_to_graph(text)
    parsed["saved_files"] = saved_paths
    parsed["main_objects"] = list_main_objects_from_uml()
    return parsed
