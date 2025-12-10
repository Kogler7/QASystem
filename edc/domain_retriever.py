from typing import List, Tuple
import os
import re
import jieba
import logging

from edc.utils.milvus_helper import MilvusHelper

logger = logging.getLogger(__name__)

class  DomainRetriever:
    def __init__(self, milvus_collection_name: str = "doc_chunks_cn_small") -> None:
        # TODO 初始化时增加参数
        # TODO 相似度召回改为倒排索引 + 相似度混合召回之后赋予权重
        self.milvus_help = MilvusHelper(collection_name=milvus_collection_name)
        self.search_top_k = 30 
        self.return_top_x = 10
        # 最小实体出现次数阈值：若一段合并后的文本中实体出现次数小于该值，则认为可能为截断片段并会被丢弃
        self.min_entity_occurrence = 1

    # TODO：领域语料库中查找相关，1.基于向量检索，2.基于关键词检索
    # TODO: 商量存储格式
    def get_text_by_entity(self, head_entity: str) -> str:
        # 使用向量相似度从 Milvus 获取候选
        candidate_results = self.milvus_help.search_text(head_entity, top_k=self.search_top_k)
        logger.debug(f"Candidate results for entity '{head_entity}': {candidate_results}")
        if not candidate_results:
            logger.warning(f"No results found for entity: {head_entity}")
            return ""

        # 对每个命中，取出其 filename 和 chunk_index，并检索前/后两个片段
        # 去重策略：维护已包含的 chunk id 集合（(filename, chunk_index)），如果某个候选的所有片段都已包含则跳过
        raw_results = []
        seen_chunk_ids = set()

        for item in candidate_results:
            filename = item.get("filename")
            center_idx = item.get("chunk_index")

            # 如果没有 chunk_index，可以尝试从 self_ref 解析数字尾号
            if center_idx is None:
                self_ref = item.get("self_ref")
                if isinstance(self_ref, str):
                    m = re.search(r"(\d+)$", self_ref)
                    if m:
                        try:
                            center_idx = int(m.group(1))
                        except Exception:
                            center_idx = None

            if filename is None or center_idx is None:
                # Fallback: 使用检索到的 text 本身
                text = item.get("text", "")
                count = len(re.findall(re.escape(head_entity), text))
                if count > 0:
                    raw_results.append({"center": None, "chunk_ids": [], "text": text, "count": count, "score": item.get("score")})
                continue

            # 查询邻居
            expr = f"filename == '{filename}' and chunk_index in [{center_idx-1},{center_idx},{center_idx+1}]"
            neighbors = self.milvus_help.query_by_expr(expr)

            # 把 neighbors 按 chunk_index 排序，确保相邻顺序
            neighbors_sorted = sorted([n for n in neighbors if n.get("chunk_index") is not None], key=lambda x: x.get("chunk_index"))

            # 构造 chunk id 列表并合并文本，保证相邻连着
            chunk_ids = []
            parts = []
            for n in neighbors_sorted:
                cid = (n.get("filename"), n.get("chunk_index"))
                chunk_ids.append(cid)
                parts.append(n.get("text", ""))

            # 去重：如果本候选的所有 chunk 都已在 seen_chunk_ids 中，则跳过
            if chunk_ids and all(cid in seen_chunk_ids for cid in chunk_ids):
                continue

            # 将本候选的 chunk_ids 添加到 seen 集合
            for cid in chunk_ids:
                seen_chunk_ids.add(cid)

            combined_text = "\n".join(parts) if parts else item.get("text", "")
            count = len(re.findall(re.escape(head_entity), combined_text))
            raw_results.append({"center": (filename, center_idx), "chunk_ids": chunk_ids, "text": combined_text, "count": count, "score": item.get("score")})

            # 早停：如果已收集到足够返回数目，可以继续收集更多，但最终会按 count 排序并取前 return_top_x
            if len(raw_results) >= self.search_top_k * 2:
                break

        # 过滤：丢弃实体出现次数少于阈值的候选
        filtered = [r for r in raw_results if r.get("count", 0) >= self.min_entity_occurrence]

        # 如果过滤后为空，回退到不过滤的 raw_results
        if not filtered:
            filtered = raw_results

        if not filtered:
            logger.warning(f"Entity '{head_entity}' not found in retrieved contexts")
            return ""

        # 按实体出现次数降序排序，出现次数相同可按向量相似度 score 降序
        filtered.sort(key=lambda x: (x.get("count", 0), -(x.get("score") or 0)), reverse=True)

        top_texts = [r["text"] for r in filtered[: self.return_top_x]]
        combined_text = "\n".join(top_texts)
        return combined_text