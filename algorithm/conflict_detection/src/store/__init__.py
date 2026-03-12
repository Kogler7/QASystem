"""Convenience façade for Neo4j access.

* ``Neo4jClient`` – class for ad‑hoc connections
* ``neo4j_db``    – shared singleton injected by ``config.py``

Typing note
-----------
Python 3.9 does **not** support the ``A | B`` union operator, so the
public ``neo4j_db`` attribute is annotated via ``typing.Union`` only when
`TYPE_CHECKING` is *on*.  At runtime we simply overwrite the placeholder
instance.
"""

from typing import TYPE_CHECKING, Union

# 👉 核心替换：从引入 virtuoso_client 变成引入 neo4j_client
from .neo4j_client import Neo4jClient


class _NotInitialised:
    """Placeholder that errors on any attribute access."""

    def __getattr__(self, item):  # pragma: no cover
        # 报错提示也全面更新为 Neo4j
        raise RuntimeError(
            "store.neo4j_db accessed before src.config created the singleton"
        )

    def __repr__(self):  # pragma: no cover
        return "<neo4j_db :: not initialised>"


# ── public singleton placeholder ─────────────────────────────────────────
if TYPE_CHECKING:
    neo4j_db: Union[Neo4jClient, _NotInitialised]
else:
    neo4j_db = _NotInitialised()  # type: ignore[assignment]


# 暴露给外部的模块只剩下 Neo4j 相关的对象
__all__ = ["Neo4jClient", "neo4j_db"]