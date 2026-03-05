import requests
from typing import List, Dict, Any, Optional
import hashlib
from datetime import datetime
import json
import logging

logger = logging.getLogger(__name__)


class ESClient:
    def __init__(self, host: str = "localhost", port: int = 9200):
        """
        初始化Elasticsearch客户端，使用Requests库确保兼容性
        """
        self.base_url = f"http://{host}:{port}"
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        })
        self.timeout = 30

        # 测试连接
        try:
            response = self.session.get(f"{self.base_url}/", timeout=5)
            if response.status_code == 200:
                info = response.json()
                es_version = info.get('version', {}).get('number', 'unknown')
                logger.info(f"✓ Elasticsearch连接成功，版本: {es_version}")
            else:
                logger.error(f"Elasticsearch连接失败，状态码: {response.status_code}")
                self.session = None
        except Exception as e:
            logger.error(f"Elasticsearch连接失败: {e}")
            self.session = None

    def _make_request(self, method: str, endpoint: str, data: Any = None) -> Dict:
        """发送HTTP请求"""
        if not self.session:
            return {'error': '客户端未初始化'}

        url = f"{self.base_url}{endpoint}"
        try:
            if data is not None:
                response = self.session.request(
                    method, url, data=json.dumps(data), timeout=self.timeout
                )
            else:
                response = self.session.request(method, url, timeout=self.timeout)

            if response.status_code in [200, 201]:
                return response.json()
            else:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"请求失败 {endpoint}: {error_msg}")
                return {'error': error_msg}

        except Exception as e:
            logger.error(f"请求异常 {endpoint}: {e}")
            return {'error': str(e)}

    def create_index(self, index_name: str) -> bool:
        """
        创建索引 - 兼容Elasticsearch 7.10.2
        """
        # 首先检查索引是否存在
        check_result = self._make_request('HEAD', f"/{index_name}")
        if not isinstance(check_result, dict) or 'error' not in check_result:
            logger.info(f"索引 {index_name} 已存在")
            return True

        # 创建索引的映射
        mapping = {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
                "analysis": {
                    "analyzer": {
                        "default": {
                            "type": "standard"
                        }
                    }
                }
            },
            "mappings": {
                "properties": {
                    "doc_id": {"type": "keyword"},
                    "title": {"type": "text", "analyzer": "standard"},
                    "content": {"type": "text", "analyzer": "standard"},
                    "file_type": {"type": "keyword"},
                    "file_path": {"type": "keyword"},
                    "file_size": {"type": "long"},
                    "is_scanned": {"type": "boolean"},
                    "metadata": {"type": "object", "enabled": True},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"}
                }
            }
        }

        # 创建索引
        result = self._make_request('PUT', f"/{index_name}", mapping)

        if isinstance(result, dict):
            if result.get('acknowledged') is True:
                logger.info(f"索引 {index_name} 创建成功")
                return True
            elif 'error' in result:
                error_type = result.get('error', {}).get('type', '')
                if 'resource_already_exists_exception' in str(error_type):
                    logger.info(f"索引 {index_name} 已存在")
                    return True

        logger.error(f"创建索引 {index_name} 失败: {result}")
        return False

    def index_document(self, index_name: str, document: Dict[str, Any]) -> bool:
        """
        索引单个文档
        """
        # 生成文档ID
        doc_id = hashlib.md5(
            f"{document.get('file_path', '')}_{document.get('created_at', '')}".encode()
        ).hexdigest()

        # 准备要索引的文档
        es_document = {
            'doc_id': doc_id,
            'title': document.get('title', '未知文档'),
            'content': str(document.get('content', ''))[:50000],  # 限制长度
            'file_type': document.get('file_type', ''),
            'file_path': document.get('file_path', ''),
            'file_size': document.get('file_size', 0),
            'is_scanned': document.get('is_scanned', False),
            'metadata': document.get('metadata', {}),
            'created_at': document.get('created_at', datetime.now().isoformat()),
            'updated_at': datetime.now().isoformat()
        }

        # 索引文档
        result = self._make_request('PUT', f"/{index_name}/_doc/{doc_id}", es_document)

        if isinstance(result, dict):
            if result.get('result') in ['created', 'updated']:
                logger.info(f"文档索引成功: {es_document.get('title')}")
                return True
            elif 'error' in result:
                logger.error(f"文档索引失败: {result.get('error')}")
                return False

        logger.warning(f"文档索引返回未知结果: {result}")
        return False

    def search_documents(self, index_name: str, query: str,
                         size: int = 10, from_: int = 0) -> List[Dict[str, Any]]:
        """
        搜索文档
        """
        search_body = {
            "query": {
                "multi_match": {
                    "query": query,
                    "fields": ["title^2", "content"],
                    "type": "best_fields"
                }
            },
            "size": size,
            "from": from_
        }

        result = self._make_request('POST', f"/{index_name}/_search", search_body)

        if isinstance(result, dict) and 'hits' in result:
            hits = result['hits'].get('hits', [])
            documents = []
            for hit in hits:
                doc = hit.get('_source', {})
                doc['id'] = hit.get('_id')
                doc['score'] = hit.get('_score')
                documents.append(doc)

            logger.info(f"搜索找到 {len(documents)} 个结果")
            return documents

        logger.error(f"搜索失败: {result}")
        return []

    def get_document_count(self, index_name: str) -> int:
        """
        获取索引中的文档数量
        """
        result = self._make_request('GET', f"/{index_name}/_count")

        if isinstance(result, dict):
            return result.get('count', 0)

        return 0

    def health_check(self) -> Dict[str, Any]:
        """
        健康检查
        """
        if not self.session:
            return {'status': 'disconnected', 'error': '客户端未初始化'}

        try:
            response = self.session.get(f"{self.base_url}/_cluster/health", timeout=5)
            if response.status_code == 200:
                health = response.json()
                return {
                    'status': 'connected',
                    'cluster_name': health.get('cluster_name', 'unknown'),
                    'cluster_status': health.get('status', 'unknown'),
                    'number_of_nodes': health.get('number_of_nodes', 0)
                }
            else:
                return {'status': 'error', 'error': f"HTTP {response.status_code}"}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def delete_index(self, index_name: str) -> bool:
        """
        删除索引
        """
        result = self._make_request('DELETE', f"/{index_name}")

        if isinstance(result, dict):
            if result.get('acknowledged') is True:
                logger.info(f"索引 {index_name} 删除成功")
                return True

        logger.error(f"删除索引失败: {result}")
        return False

    def bulk_index(self, index_name: str, documents: List[Dict[str, Any]]) -> bool:
        """
        批量索引文档
        """
        if not documents:
            return True

        # 构建批量请求体
        bulk_body = []
        for doc in documents:
            doc_id = hashlib.md5(
                f"{doc.get('file_path', '')}_{doc.get('created_at', '')}".encode()
            ).hexdigest()

            doc['updated_at'] = datetime.now().isoformat()

            # 添加索引操作头
            bulk_body.append(json.dumps({"index": {"_index": index_name, "_id": doc_id}}))
            # 添加文档数据
            bulk_body.append(json.dumps({
                'doc_id': doc_id,
                'title': doc.get('title', '未知文档'),
                'content': str(doc.get('content', ''))[:50000],
                'file_type': doc.get('file_type', ''),
                'file_path': doc.get('file_path', ''),
                'file_size': doc.get('file_size', 0),
                'is_scanned': doc.get('is_scanned', False),
                'metadata': doc.get('metadata', {}),
                'created_at': doc.get('created_at', datetime.now().isoformat()),
                'updated_at': doc['updated_at']
            }))

        # 发送批量请求
        bulk_data = '\n'.join(bulk_body) + '\n'

        try:
            response = self.session.post(
                f"{self.base_url}/_bulk",
                data=bulk_data,
                headers={'Content-Type': 'application/x-ndjson'},
                timeout=self.timeout
            )

            if response.status_code == 200:
                result = response.json()
                if not result.get('errors', True):
                    logger.info(f"批量索引成功，处理 {len(documents)} 个文档")
                    return True
                else:
                    errors = result.get('items', [])
                    error_count = 0
                    for item in errors:
                        if 'error' in item.get('index', {}):
                            error_count += 1
                    logger.warning(f"批量索引部分失败，{error_count}/{len(documents)} 个文档失败")
                    return error_count == 0
            else:
                logger.error(f"批量索引失败，状态码: {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"批量索引异常: {e}")
            return False

    def close(self):
        """关闭连接"""
        if self.session:
            self.session.close()
            logger.info("Elasticsearch连接已关闭")