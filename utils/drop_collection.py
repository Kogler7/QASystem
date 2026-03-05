from pymilvus import MilvusClient, MilvusException
import time

# 初始化 Milvus 客户端（根据你的实际配置修改）
client = MilvusClient(
    uri="http://localhost:19530",
    # token="root:Milvus"
)

collection_name = "intent_rag"

def safe_drop_collection(client, collection_name, max_retries=3, retry_delay=2):
    """安全删除 Milvus 集合，带重试机制"""
    for attempt in range(max_retries):
        try:
            # 1. 先检查集合是否存在
            if client.has_collection(collection_name):
                # 2. 尝试删除集合
                client.drop_collection(collection_name=collection_name)
                print(f"成功删除collection: {collection_name}")
                return True
            else:
                print(f"集合 {collection_name} 不存在，无需删除")
                return True
        except MilvusException as e:
            if "node not match" in str(e) or "InvalidateCollectionMetaCache failed" in str(e):
                print(f"删除集合失败（尝试 {attempt+1}/{max_retries}）: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    # 重试前刷新客户端连接（关键）
                    client.close()
                    client = MilvusClient(uri="http://localhost:19530")
                else:
                    raise
            else:
                raise

# 执行安全删除
if __name__ == "__main__":
    safe_drop_collection(client, collection_name)