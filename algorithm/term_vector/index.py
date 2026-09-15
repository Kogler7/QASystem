"""进程内 Term 语义向量索引（numpy 余弦检索）。

数据形态：单一 JSON 文件，内含 records（Term 元数据，顺序即矩阵行序）
与 vectors（N x D 的 float32 二维数组）。量级小（几百~几万条）时在内存里
做暴力余弦检索即可，无需引入外部向量库。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np


class TermVectorIndex:
    """Term 本地向量索引：加载后对查询向量做余弦 top-K。"""

    def __init__(self, records: list[dict[str, Any]], vectors: np.ndarray) -> None:
        if len(records) != vectors.shape[0]:
            raise ValueError("records 与 vectors 行数不一致")
        self.records = records
        self.vectors = vectors.astype(np.float32, copy=False)
        norms = np.linalg.norm(self.vectors, axis=1)
        norms[norms == 0] = 1.0
        self._norms = norms

    @classmethod
    def from_json(cls, path: str | Path) -> "TermVectorIndex":
        path = Path(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        vectors = np.asarray(payload["vectors"], dtype=np.float32)
        records = payload.get("records") or []
        if not isinstance(records, list) or not isinstance(vectors, np.ndarray):
            raise ValueError("索引文件结构不合法（需要 records + vectors）")
        return cls(records, vectors)

    def to_json(self, path: str | Path) -> None:
        path = Path(path)
        payload = {
            "records": self.records,
            "vectors": self.vectors.tolist(),
            "dim": int(self.vectors.shape[1]),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def search(
        self,
        query_vector: list[float] | np.ndarray,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        """返回 [{record 字段, term_id, score}, ...]，按余弦相似度降序。"""
        query = np.asarray(query_vector, dtype=np.float32).reshape(1, -1)
        query_norm = float(np.linalg.norm(query)) or 1.0
        scores = (self.vectors @ query.T).ravel() / (self._norms * query_norm)

        order = np.argsort(-scores)[: max(top_k, 0)]
        results: list[dict[str, Any]] = []
        for index in order:
            score = float(scores[index])
            if score < min_score:
                break  # scores 已降序，后续只会更低
            record = dict(self.records[int(index)])
            record["score"] = round(score, 4)
            record["term_id"] = record.get("term_id") or record.get("id")
            results.append(record)
        return results
