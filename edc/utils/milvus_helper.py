import json
import logging
from pathlib import Path
from typing import List
import numpy as np

from sentence_transformers import SentenceTransformer
from pymilvus import Collection, connections, utility, FieldSchema, CollectionSchema, DataType

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger(__name__)


class MilvusHelper:
    def __init__(
        self,
        collection_name: str,
        milvus_host: str = "milvus-standalone",
        milvus_port: int = 19530,
        embed_model_name: str = "BAAI/bge-small-zh",
        dim: int = 768,  # 向量维度
    ):
        # Milvus 连接
        connections.connect("default", host=milvus_host, port=milvus_port)
        self.collection_name = collection_name
        self.dim = dim

        # 检查 Collection 是否存在
        if utility.has_collection(collection_name):
            _log.info(f"Collection '{collection_name}' exists")
            self.collection = Collection(collection_name)
        else:
            _log.info(f"Collection '{collection_name}' does not exist, creating...")
            self._create_collection()

        # 向量化模型
        self.embed_model = SentenceTransformer(embed_model_name)

        self._ensure_collection_loaded()

    def _create_collection(self):
        # 定义 schema
        fields = [
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=self.dim, description="embedding vector"),
            FieldSchema(name="filename", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="binary_hash", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=4096),
            FieldSchema(name="meta", dtype=DataType.VARCHAR, max_length=8192),
        ]
        schema = CollectionSchema(fields, description="Doc chunks collection")
        self.collection = Collection(self.collection_name, schema=schema)

    def embed_text(self, text: str):
        return self.embed_model.encode(text, normalize_embeddings=True)

    def insert_chunk(self, text, filename, binary_hash, meta):
        embedding = self.embed_text(text)
        self.collection.insert([
            [embedding],
            [filename],
            [binary_hash],
            [text],
            [meta]
        ])

    def process_json_file(self, json_path: Path):
        """
        处理单个 JSON 文件
        """
        with json_path.open("r", encoding="utf-8") as f:
            chunks = json.load(f)

        inserted = 0
        for i, chunk in enumerate(chunks):
            text = chunk.get("text", "")
            meta = chunk.get("meta", {})
            origin = meta.get("origin", {})

            filename = origin.get("filename", json_path.stem)
            binary_hash = str(origin.get("binary_hash", "unknown"))
            meta = json.dumps(meta, ensure_ascii=False)

            self.insert_chunk(text, filename, binary_hash, meta)
            inserted += 1
            _log.info(f"Inserted chunk {i+1} from {filename}: {text[:30]}...")

        # 插入后自动创建 HNSW 索引并加载
        self.create_index()
        self.load_collection()
        return inserted

    def process_json_dir(self, dir_path: Path):
        """
        处理整个目录下的 JSON 文件
        """
        if not dir_path.is_dir():
            raise ValueError(f"{dir_path} is not a directory")

        json_files = list(dir_path.glob("*.json"))
        _log.info(f"Found {len(json_files)} JSON files in {dir_path}")

        results = []
        for json_file in json_files:
            try:
                count = self.process_json_file(json_file)
                results.append((json_file, count))
                _log.info(f"Processed {json_file}: {count} chunks inserted")
            except Exception as e:
                _log.warning(f"Failed to process {json_file}: {e}")

        return results

    # TODO 分布式？是怎么加载的？意义
    def _ensure_collection_loaded(self):
        """只在必要时加载 Collection（避免重复加载）"""
        if not hasattr(self, "collection"):
            self.collection = Collection(self.collection_name)

        if not utility.has_collection(self.collection_name):
            raise RuntimeError(f"Collection '{self.collection_name}' does not exist")

        self.collection.load()
        _log.info(f"Collection '{self.collection_name}' loaded into memory")


    def create_index(self):
        """
        为 embedding 字段创建 HNSW 索引，如果已有索引不会重复创建
        """
        if not self.collection.has_index():
            index_params = {
                "index_type": "HNSW",
                "metric_type": "COSINE",
                "params": {"M": 16, "efConstruction": 200},
            }
            self.collection.create_index("embedding", index_params=index_params)
            _log.info("HNSW 索引已创建")
        else:
            _log.info("索引已存在，无需重复创建")

    def load_collection(self):
        """
        加载 Collection，使新插入的向量可被搜索
        """
        self.collection.load()
        _log.info(f"集合已加载，共有 {self.collection.num_entities} 条数据")

    
    def search_text(
        self,
        query: str,
        top_k: int = 5,
        metric_type: str = "COSINE",
        ef: int = 128,
        output_fields: List[str] = ["text", "filename", "meta", "self_ref", "chunk_index"]
    ):
        """
        基于文本语义相似度的 Milvus 检索
        """
        if not hasattr(self, "collection"):
            self.collection = Collection(self.collection_name)

        # 生成查询向量
        query_vec = self.embed_text(query)
        if isinstance(query_vec, np.ndarray):
            query_vec = query_vec.tolist()

        search_params = {
            "metric_type": metric_type,
            "params": {"ef": ef}
        }

        self.collection.load()
        results = self.collection.search(
            data=[query_vec],
            anns_field="embedding",
            param=search_params,
            limit=top_k,
            output_fields=output_fields,
        )

        hits = []
        for hit in results[0]:
            meta = {}
            try:
                meta = json.loads(hit.entity.get("meta", "{}"))
            except Exception:
                pass
            hits.append({
                "score": getattr(hit, 'distance', None),
                "text": hit.entity.get("text"),
                "filename": hit.entity.get("filename"),
                "meta": meta,
                "self_ref": hit.entity.get("self_ref"),
                "chunk_index": hit.entity.get("chunk_index"),
            })

        return hits

    def query_by_expr(self, expr: str, output_fields: List[str] = ["text", "filename", "meta", "self_ref", "chunk_index"]):
        """
        使用 Milvus 的 expr 做精确查询（非向量检索），返回匹配的记录。
        expr 示例: "filename == 'sample_8_95.md' and chunk_index in [3,4,5]"
        """
        if not hasattr(self, "collection"):
            self.collection = Collection(self.collection_name)

        # 确保 collection 已加载
        try:
            self.collection.load()
        except Exception:
            pass

        try:
            res = self.collection.query(expr=expr, output_fields=output_fields)
        except Exception as e:
            _log.warning(f"query_by_expr failed: {e}")
            return []

        out = []
        for r in res:
            m = r.get("meta")
            try:
                m = json.loads(m) if isinstance(m, str) else m
            except Exception:
                pass
            out.append({
                "text": r.get("text"),
                "filename": r.get("filename"),
                "meta": m,
                "self_ref": r.get("self_ref"),
                "chunk_index": r.get("chunk_index"),
            })

        return out

if __name__ == "__main__":
    # 测试
    vector_store = MilvusHelper(collection_name="doc_chunks_cn_small")

    # 单个文件测试
    test_file = Path("/workspace/llmkg/chunked_json/test.json")
    vector_store.process_json_file(test_file)

    # 目录测试
    # test_dir = Path("/workspace/llmkg/chunked_json")
    # vector_store.process_json_dir(test_dir)
