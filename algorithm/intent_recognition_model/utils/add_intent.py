import logging
import os
import sys
import time

from pymilvus import MilvusClient, MilvusException, Collection, connections

from ir_config.config import Config
from utils.encoder import HybridEncoder

# 配置日志
config = Config()
log_path = config.log_path

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler(filename=os.path.join(log_path,"add_intent.log"),encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

class AddIntent:
    def __init__(self):
        self.config = Config()
        self.data_dir = self.config.data_dir
        self.collection_name = self.config.collection_name
        self.milvus_uri = self.config.milvus_uri

        logger.info("正在初始化编码器...")
        self.encoder = HybridEncoder()
        self.dense_dim = self.encoder.dense_dim
        self.sparse_dim = self.encoder.sparse_dim

        logger.info(f"正在连接到 Milvus: {self.milvus_uri}")
        connections.connect(hosts=self.milvus_uri)
        self.milvus_client = self._init_milvus_client()
        self.collection = Collection(name=self.collection_name)

    def _init_milvus_client(self)-> MilvusClient|None:
        """初始化客户端MilvusClient"""
        max_retries = 3
        retry_delay = 2
        for attempt in range(max_retries):
            try:
                client = MilvusClient(uri=self.milvus_uri)

                # 测试链接
                client.list_collections()
                logger.info("Milvus连接成功")
                return client
            except MilvusException as e:
                logger.warning(f"Milvus 连接失败（尝试 {attempt + 1}/{max_retries}）: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    logger.error("Milvus 连接重试次数耗尽，初始化失败")
                    raise
            except Exception as e:
                logger.error(f"Milvus 客户端初始化异常: {e}")
                raise
        return None


    def add_intent(self,intent):
        """添加意图"""
        pass


