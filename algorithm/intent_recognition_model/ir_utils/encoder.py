import os

# 先完整加载 torch 与 torchvision，再加载 milvus_model/transformers，避免循环导入导致 torchvision.extension 未就绪
import torch  # type: ignore[import-untyped]
import torchvision  # type: ignore[import-untyped]
from milvus_model.hybrid import BGEM3EmbeddingFunction  # type: ignore[import-untyped]

from algorithm.intent_recognition_model.ir_config.config import Config


class HybridEncoder:
    def __init__(self):
        #初始化配置类
        config = Config()

        # 本地模型路径
        local_model_path = config.model_path

        # 验证本地模型路径是否存在
        if not os.path.exists(local_model_path):
            raise FileNotFoundError(
                "本地 BGE 模型路径不存在: "
                f"{local_model_path}。请在根 .env 或 backend/.env 配置 MODEL_PATH "
                "(例如 /models/bge_m3_model)，并确保 docker-compose 挂载了 HOST_MODELS_DIR 到 /models。"
            )

        self.bge_m3 = BGEM3EmbeddingFunction(
            model_name=local_model_path,
            model_path=local_model_path,
            use_fp16=False,
            device="cuda" if torch.cuda.is_available() else "cpu",
        )

        # 维度
        self.dense_dim = self.bge_m3.dim['dense']
        self.sparse_dim = self.bge_m3.dim['sparse']
        print(f"BGE-M3 初始化成功 | Dense: {self.dense_dim}D | Sparse: {self.sparse_dim}D")

    def encode_text_hybrid(self,text_content:str)->dict:
        """使用BGE-M3编码文本，返回稀疏和密集向量"""

        text = text_content.strip()
        if not text:
            return {
                "sparse": {},                     # 空稀疏向量
                "dense": [0.0] * self.dense_dim,  # 零密集向量
                "is_empty": True,
                "text_length": 0
            }

        with torch.no_grad():
            embeddings = self.bge_m3([text])

        return {
            "sparse": embeddings["sparse"]._getrow(0),    # Dict[int, float]
            "dense": embeddings["dense"][0],      # List[float], len=dense_dim
            "is_empty": False,
            "text_length": len(text)
        }

    def encode_only_dense(self,text_content:str):
        if len(text_content.strip()) == 0:
            return [0.0] * self.dense_dim
        with torch.no_grad():
            embeddings = self.bge_m3([text_content])
            return embeddings["dense"][0]
