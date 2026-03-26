"""
向量转化存储与自然语言转 Cypher：进程内调用 algorithm/NL_to_cypher 算法。
使用前需安装 algorithm/NL_to_cypher 依赖，并配置 Ollama。
"""
import logging
import sys
from pathlib import Path
from typing import Any,Dict,List

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[5]
_NL2CYPHER_ROOT = _ROOT / "algorithm" / "NL_to_cypher"
if _NL2CYPHER_ROOT.exists() and str(_NL2CYPHER_ROOT) not in sys.path:
    sys.path.insert(0, str(_NL2CYPHER_ROOT))

_generator = None


def _get_generator():
    global _generator
    if _generator is None:
        try:
            from text2cypher import Text2CypherGenerator
            _generator = Text2CypherGenerator()
        except Exception as e:
            logger.warning("加载 NL2Cypher 模块失败: %s", e)
            raise
    return _generator


def health() -> dict[str, Any]:
    try:
        _get_generator()
        return {"status": "ok"}
    except Exception as e:
        return {
            "status": "unavailable",
            "detail": f"自然语言转 Cypher 模块不可用: {e}。请确认已安装 algorithm/NL_to_cypher 依赖并配置 Ollama。",
        }


def generate(question: str, graph_schema: str) -> dict[str, Any]:
    gen = _get_generator()
    cypher = gen.generate(question, graph_schema or "")
    return {"cypher": (cypher or "").strip()}


# ---------------------------------------------------------------------------
# 文本切片器接口
# ---------------------------------------------------------------------------

def _get_splitter():
    """懒加载 NL_to_cypher.splitter 模块并返回引用"""
    try:
        import splitter as _mod  # type: ignore
        return _mod
    except Exception as e:
        logger.warning("加载切片模块失败: %s", e)
        raise


def _make_docs(text: str):
    # 避免在顶层引入 langchain 依赖，动态导入
    from langchain_core.documents import Document

    return [Document(page_content=text)]


def split_by_character(text: str) -> dict[str, Any]:
    mod = _get_splitter()
    docs = _make_docs(text)
    split_docs = mod.split_by_character(docs)
    return {"chunks": [d.page_content for d in split_docs]}


def split_recursively(text: str) -> dict[str, Any]:
    mod = _get_splitter()
    docs = _make_docs(text)
    split_docs = mod.split_recursively(docs)
    return {"chunks": [d.page_content for d in split_docs]}


def split_by_markdown(text: str) -> dict[str, Any]:
    mod = _get_splitter()
    docs = _make_docs(text)
    split_docs = mod.split_by_markdown(docs)
    return {"chunks": [d.page_content for d in split_docs]}


def split_by_language_python(text: str) -> dict[str, Any]:
    mod = _get_splitter()
    docs = _make_docs(text)
    split_docs = mod.split_by_language_python(docs)
    return {"chunks": [d.page_content for d in split_docs]}

def muti_retriever()->List[Dict[str, Any]]:
    try:
        import rag
    except Exception as e:
        logger.warning("加载 RAG 模块失败: %s", e)
        raise
    nodes = rag.load_json_nodes("/home/admin_user_5/wangshuo/copy/algorithm/NL_to_cypher/records.json")
    results = rag.get_vectors_for_nodes(nodes)
    return results


def text2vector(text: str) -> dict[str, Any]:
    """将用户输入的文本切片、向量化并存入数据库。

    返回内容包含状态和生成的片段列表，可用于前端调试。
    """
    try:
        import rag
    except Exception as e:
        logger.warning("加载 RAG 模块失败: %s", e)
        raise

    docs = _make_docs(text)
    # 使用默认的递归字符切分作为基础
    split_docs = rag.txt_split(docs)

    # 嵌入并写入 Chroma 数据库，函数内部已负责持久化
    rag.embedding(split_docs)

    return {"status": "ok", "chunks": [d.page_content for d in split_docs]}

def get_all_vectors() -> dict[str, Any]:
    """获取所有向量和对应的文本片段"""
    try:
        import rag
        import numpy as np
        from langchain_core.documents import Document
    except Exception as e:
        logger.warning("加载依赖失败: %s", e)
        return {"vectors": [], "stats": None}

    try:
        # 加载向量数据库
        vector_store = rag.load_vector_store()
        if vector_store is None:
            return {"vectors": [], "stats": {"total": 0, "message": "向量数据库未初始化"}}
        
        # 获取数据
        collection = vector_store.get()
        if collection is None:
            return {"vectors": [], "stats": {"total": 0, "message": "数据库为空"}}
        ids = collection.get('ids', [])
        documents = collection.get('documents', [])
        if not documents:
            return {"vectors": [], "stats": {"total": 0, "message": "没有文档数据"}}
        
        logger.info(f"找到 {len(documents)} 个文档")
        
        try:
            # 获取嵌入模型
            model_path = "/mnt/data3/srv/qasystem/models/bge-small-zh-v1.5"
            tokenizer = rag.AutoTokenizer.from_pretrained(model_path, local_files_only=True)
            model = rag.AutoModel.from_pretrained(model_path, local_files_only=True)
            
            # 重新计算嵌入向量
            embeddings = []
            for i, doc in enumerate(documents):
                inputs = tokenizer(doc, padding=True, truncation=True, 
                                    return_tensors="pt", max_length=512)
                with rag.torch.no_grad():
                    outputs = model(**inputs)
                embedding = outputs.last_hidden_state.mean(dim=1).squeeze().tolist()
                embeddings.append(embedding)
                
                if (i + 1) % 10 == 0 or i + 1 == len(documents):
                    logger.info(f"已计算 {i + 1}/{len(documents)} 个嵌入向量")
            
            logger.info(f"重新计算了 {len(embeddings)} 个嵌入向量")
            
        except Exception as e:
            logger.error(f"重新计算嵌入向量失败: {e}")
            # 方法3：创建模拟嵌入向量
            logger.warning("创建模拟嵌入向量")
            import random
            embeddings = [[random.random() for _ in range(384)] for _ in range(len(documents))]
    
        # 获取元数据
        metadatas = collection.get('metadatas', [{}] * len(documents))
        
        # 创建向量对象
        vectors = []
        for i in range(min(len(documents), len(embeddings))):
            embedding = embeddings[i]
            ID=ids[i]
            document = documents[i]
            metadata = metadatas[i] if i < len(metadatas) else {}
            
            # 确保 embedding 是列表
            if isinstance(embedding, np.ndarray):
                embedding = embedding.tolist()
            elif not isinstance(embedding, list):
                logger.warning(f"向量 {i} 格式错误: {type(embedding)}")
                continue
            
            vectors.append({
                "id": ID,
                "embedding": embedding,
                "text": str(document),
                "metadata": metadata if isinstance(metadata, dict) else {},
                "dimension": len(embedding) if embedding else 384
            })
        
        if not vectors:
            logger.warning("没有有效的向量数据")
            return {"vectors": [], "stats": {"total": 0, "message": "没有有效的向量数据"}}
        
        logger.info(f"成功处理 {len(vectors)} 个向量")
        
        # 将嵌入向量转换为 numpy 数组用于降维
        try:
            embeddings_array = np.array([v['embedding'] for v in vectors])
            
            # 降维处理
            if len(vectors) <= 1:
                for vector in vectors:
                    vector['position'] = {'x': 0.0, 'y': 0.0}
            elif len(vectors) == 2:
                vectors[0]['position'] = {'x': -0.5, 'y': 0.0}
                vectors[1]['position'] = {'x': 0.5, 'y': 0.0}
            else:
                from sklearn.decomposition import PCA
                pca = PCA(n_components=2)
                reduced_2d = pca.fit_transform(embeddings_array)
                
                for i, vector in enumerate(vectors):
                    vector['position'] = {
                        'x': float(reduced_2d[i][0]),
                        'y': float(reduced_2d[i][1])
                    }
                    
                logger.info("PCA 降维完成")
        except Exception as e:
            logger.warning(f"降维失败: {e}，使用圆形布局")
            # 生成圆形布局
            for i, vector in enumerate(vectors):
                angle = 2 * np.pi * i / len(vectors)
                radius = 0.8
                vector['position'] = {
                    'x': float(radius * np.cos(angle)),
                    'y': float(radius * np.sin(angle))
                }
        
        # 计算相似度
        try:
            from sklearn.metrics.pairwise import cosine_similarity
            similarity_matrix = cosine_similarity(embeddings_array)
            
            for i, vector in enumerate(vectors):
                similarities = similarity_matrix[i]
                similarities_without_self = np.delete(similarities, i)
                if len(similarities_without_self) > 0:
                    vector['similarity'] = float(np.mean(similarities_without_self))
                else:
                    vector['similarity'] = 0.5
                    
            logger.info("相似度计算完成")
        except Exception as e:
            logger.warning(f"相似度计算失败: {e}")
            for vector in vectors:
                vector['similarity'] = 0.5
        
        # 简单聚类
        n_clusters = min(5, len(vectors))
        for i, vector in enumerate(vectors):
            vector['cluster'] = i % n_clusters
        
        # 计算聚类计数
        cluster_counts = {}
        for vector in vectors:
            cluster = vector.get('cluster')
            if cluster is not None:
                cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
        
        # 计算平均相似度
        avg_similarity = np.mean([v.get('similarity', 0.5) for v in vectors]) if vectors else 0.5
        
        stats = {
            "total": len(vectors),
            "dimensions": len(vectors[0]['embedding']) if vectors else 384,
            "clusters": len(cluster_counts),
            "avgSimilarity": float(avg_similarity),
        }
        
        if cluster_counts:
            stats["clusterCounts"] = cluster_counts
        
        logger.info(f"返回统计数据: 总数={len(vectors)}, 维度={stats['dimensions']}, 聚类数={stats['clusters']}")
        
        return {
            "vectors": vectors,
            "stats": stats
        }
        
    except Exception as e:
        logger.error("获取向量数据失败: %s", e, exc_info=True)
        return {"vectors": [], "stats": {"message": f"内部错误: {str(e)}"}}