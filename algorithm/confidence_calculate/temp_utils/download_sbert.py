import os
os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'  # 设置镜像
from sentence_transformers import SentenceTransformer

model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
model.save_pretrained('./sbert_model')