"""在无法执行 ``knowledge_extract/__init__.py`` 时加载本包。

目录名是 ``v2_2_v31``。平时直接 ``import algorithm.knowledge_extract.v2_2_v31``。
这个入口只在父包还没加载、又不想连带导入 v1 时使用。
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


ALIAS = "algorithm.knowledge_extract.v2_2_v31"


def install(alias: str = ALIAS):
    """注册包并执行 ``__init__.py``。重复调用返回已注册模块。"""
    existing = sys.modules.get(alias)
    if existing is not None and hasattr(existing, "extract_split"):
        return existing
    root = Path(__file__).resolve().parent
    repo_root = root.parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    import algorithm  # noqa: F401
    parent_name = "algorithm.knowledge_extract"
    parent = sys.modules.get(parent_name)
    if parent is None:
        # 不执行 knowledge_extract/__init__.py：那个入口会加载 v1，并要求 openai。
        # 只挂父包路径，相对导入仍然落到 v2_2_v31。
        parent = types.ModuleType(parent_name)
        parent.__path__ = [str(root.parent)]
        parent.__package__ = parent_name
        sys.modules[parent_name] = parent

    spec = importlib.util.spec_from_file_location(
        alias,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {root}")
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(root)]
    module.__package__ = alias
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    parent = sys.modules["algorithm.knowledge_extract"]
    short_name = alias.rsplit(".", 1)[-1]
    setattr(parent, short_name, module)
    return module


if __name__ == "__main__":
    loaded = install()
    print(loaded.__name__)
    print(loaded.__file__)
