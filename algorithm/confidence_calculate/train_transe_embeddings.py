import torch
import torch.nn as nn
import torch.optim as optim
import json
import os
from collections import defaultdict
import random
import numpy as np

# 设置随机种子
torch.manual_seed(42)
np.random.seed(42)
random.seed(42)


class TransE(nn.Module):
    """TransE知识图谱嵌入模型"""
    
    def __init__(self, entity_count, relation_count, embedding_dim=100, margin=1.0, norm=2):
        super(TransE, self).__init__()
        self.entity_count = entity_count
        self.relation_count = relation_count
        self.embedding_dim = embedding_dim
        self.margin = margin
        self.norm = norm
        
        # 初始化实体和关系嵌入
        self.entity_embeddings = nn.Embedding(entity_count, embedding_dim)
        self.relation_embeddings = nn.Embedding(relation_count, embedding_dim)
        
        # Xavier初始化
        nn.init.xavier_uniform_(self.entity_embeddings.weight)
        nn.init.xavier_uniform_(self.relation_embeddings.weight)
        
        # L2归一化（TransE的常见做法）
        self._normalize_embeddings()
    
    def _normalize_embeddings(self):
        """对实体嵌入进行L2归一化"""
        self.entity_embeddings.weight.data = nn.functional.normalize(
            self.entity_embeddings.weight.data, p=2, dim=1
        )
    
    def forward(self, heads, relations, tails):
        """计算正样本得分（距离）"""
        h = self.entity_embeddings(heads)
        r = self.relation_embeddings(relations)
        t = self.entity_embeddings(tails)
        
        # h + r ≈ t，计算距离
        score = torch.norm(h + r - t, p=self.norm, dim=1)
        return score
    
    def loss_function(self, pos_scores, neg_scores):
        """基于margin的hinge损失"""
        # 正样本得分应该低，负样本得分应该高
        # loss = max(0, pos_score - neg_score + margin)
        losses = torch.relu(pos_scores - neg_scores + self.margin)
        return losses.mean()


class KGDataLoader:
    """知识图谱数据加载器"""
    
    def __init__(self, data_path, batch_size=32):
        self.batch_size = batch_size
        self.triples, self.entity2id, self.relation2id = self._load_data(data_path)
        self.entity_list = list(self.entity2id.keys())
        self.relation_list = list(self.relation2id.keys())
        self.n_entity = len(self.entity2id)
        self.n_relation = len(self.relation2id)
        
        print(f"加载了 {len(self.triples)} 个三元组")
        print(f"实体数量: {self.n_entity}")
        print(f"关系数量: {self.n_relation}")
    
    def _load_data(self, data_path):
        """从JSON文件加载数据"""
        with open(data_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 收集所有实体和关系
        entities = set()
        relations = set()
        triples = []
        
        for item in data:
            triple = item['triple']
            h, r, t = triple['subject'], triple['predicate'], triple['object']
            entities.add(h)
            entities.add(t)
            relations.add(r)
            triples.append((h, r, t))
        
        # 创建实体和关系的ID映射
        entity2id = {ent: idx for idx, ent in enumerate(sorted(entities))}
        relation2id = {rel: idx for idx, rel in enumerate(sorted(relations))}
        
        return triples, entity2id, relation2id
    
    def _generate_negative_sample(self, h, r, t):
        """生成负样本（随机替换头实体或尾实体）"""
        if random.random() > 0.5:
            # 替换头实体
            neg_h = random.choice(self.entity_list)
            while neg_h == h:
                neg_h = random.choice(self.entity_list)
            return (neg_h, r, t)
        else:
            # 替换尾实体
            neg_t = random.choice(self.entity_list)
            while neg_t == t:
                neg_t = random.choice(self.entity_list)
            return (h, r, neg_t)
    
    def get_batch(self):
        """获取一个批次的数据（包含正负样本）"""
        # 随机选择batch_size个正样本
        batch_triples = random.sample(self.triples, min(self.batch_size, len(self.triples)))
        
        # 生成对应的负样本
        pos_heads = []
        pos_rels = []
        pos_tails = []
        neg_heads = []
        neg_rels = []
        neg_tails = []
        
        for h, r, t in batch_triples:
            # 正样本
            pos_heads.append(self.entity2id[h])
            pos_rels.append(self.relation2id[r])
            pos_tails.append(self.entity2id[t])
            
            # 负样本
            neg_h, neg_r, neg_t = self._generate_negative_sample(h, r, t)
            neg_heads.append(self.entity2id[neg_h])
            neg_rels.append(self.relation2id[neg_r])
            neg_tails.append(self.entity2id[neg_t])
        
        return (
            torch.tensor(pos_heads, dtype=torch.long),
            torch.tensor(pos_rels, dtype=torch.long),
            torch.tensor(pos_tails, dtype=torch.long),
            torch.tensor(neg_heads, dtype=torch.long),
            torch.tensor(neg_rels, dtype=torch.long),
            torch.tensor(neg_tails, dtype=torch.long)
        )
    
    def get_all_triples_tensor(self):
        """获取所有三元组的tensor形式（用于最终保存）"""
        heads = []
        rels = []
        tails = []
        for h, r, t in self.triples:
            heads.append(self.entity2id[h])
            rels.append(self.relation2id[r])
            tails.append(self.entity2id[t])
        
        return (
            torch.tensor(heads, dtype=torch.long),
            torch.tensor(rels, dtype=torch.long),
            torch.tensor(tails, dtype=torch.long)
        )


def train_transe(data_path='data/train.json', embedding_dim=100, epochs=500, batch_size=32, margin=1.0, lr=0.01):
    """训练TransE模型并保存嵌入"""
    
    # 创建保存目录
    os.makedirs('embeddings', exist_ok=True)
    
    # 加载数据
    loader = KGDataLoader(data_path, batch_size)
    
    # 初始化模型
    model = TransE(
        entity_count=loader.n_entity,
        relation_count=loader.n_relation,
        embedding_dim=embedding_dim,
        margin=margin
    )
    
    # 优化器
    optimizer = optim.Adam(model.parameters(), lr=lr)
    
    # 训练循环
    print("开始训练TransE模型...")
    for epoch in range(epochs):
        # 获取一个批次的数据
        pos_h, pos_r, pos_t, neg_h, neg_r, neg_t = loader.get_batch()
        
        # 前向传播
        pos_scores = model(pos_h, pos_r, pos_t)
        neg_scores = model(neg_h, neg_r, neg_t)
        
        # 计算损失
        loss = model.loss_function(pos_scores, neg_scores)
        
        # 反向传播
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        
        # 归一化实体嵌入（TransE需要）
        model._normalize_embeddings()
        
        # 打印训练信息
        if (epoch + 1) % 50 == 0:
            print(f"Epoch {epoch+1}/{epochs}, Loss: {loss.item():.4f}")
    
    print("训练完成！")
    
    # 保存嵌入文件
    save_embeddings(model, loader)
    
    return model, loader


def save_embeddings(model, loader):
    """保存实体和关系嵌入文件"""
    
    # 获取嵌入矩阵
    entity_emb = model.entity_embeddings.weight.detach()
    relation_emb = model.relation_embeddings.weight.detach()
    
    # 保存实体嵌入（包含实体名称映射）
    torch.save({
        'embeddings': entity_emb,
        'entities': loader.entity_list,
        'entity2id': loader.entity2id,
        'dim': entity_emb.shape[1]
    }, 'embeddings/entities.pt')
    
    # 保存关系嵌入（包含关系名称映射）
    torch.save({
        'embeddings': relation_emb,
        'relations': loader.relation_list,
        'relation2id': loader.relation2id,
        'dim': relation_emb.shape[1]
    }, 'embeddings/relations.pt')
    
    # 同时保存为CSV格式，方便查看
    save_as_csv(entity_emb, loader.entity_list, 'embeddings/entities.csv')
    save_as_csv(relation_emb, loader.relation_list, 'embeddings/relations.csv')
    
    print(f"\n嵌入文件已保存到 embeddings/ 目录：")
    print(f"  - entities.pt: {entity_emb.shape[0]}个实体，维度{entity_emb.shape[1]}")
    print(f"  - relations.pt: {relation_emb.shape[0]}个关系，维度{relation_emb.shape[1]}")
    print(f"  - entities.csv (文本格式)")
    print(f"  - relations.csv (文本格式)")


def save_as_csv(embeddings, names, filename):
    """保存为CSV格式"""
    import pandas as pd
    
    # 创建DataFrame
    emb_np = embeddings.numpy()
    columns = [f"dim_{i}" for i in range(emb_np.shape[1])]
    df = pd.DataFrame(emb_np, index=names, columns=columns)
    df.to_csv(filename, index=True, index_label='entity')
    print(f"  - {filename}")


def test_embeddings():
    """测试加载嵌入文件"""
    print("\n" + "="*50)
    print("测试加载嵌入文件...")
    
    # 加载实体嵌入
    entity_data = torch.load('embeddings/entities.pt')
    print(f"实体嵌入形状: {entity_data['embeddings'].shape}")
    print(f"前5个实体: {entity_data['entities'][:5]}")
    
    # 加载关系嵌入
    relation_data = torch.load('embeddings/relations.pt')
    print(f"关系嵌入形状: {relation_data['embeddings'].shape}")
    print(f"所有关系: {relation_data['relations']}")
    
    # 测试一个简单的相似度计算
    if len(entity_data['entities']) >= 2:
        emb1 = entity_data['embeddings'][0]
        emb2 = entity_data['embeddings'][1]
        sim = torch.cosine_similarity(emb1.unsqueeze(0), emb2.unsqueeze(0))
        print(f"\n实体 '{entity_data['entities'][0]}' 和 '{entity_data['entities'][1]}' 的余弦相似度: {sim.item():.4f}")
    
    print("="*50)


if __name__ == "__main__":
    # 训练参数配置
    config = {
        'data_path': 'data/train.json',  # 训练数据路径
        'embedding_dim': 100,            # 嵌入维度
        'epochs': 100,                   # 训练轮数
        'batch_size': 32,                 # 批次大小
        'margin': 1.0,                    # Hinge损失margin
        'lr': 0.01                         # 学习率
    }
    
    print("TransE嵌入训练脚本")
    print("=" * 50)
    print(f"配置参数:")
    for k, v in config.items():
        print(f"  {k}: {v}")
    print("=" * 50)
    
    # 检查数据文件是否存在
    if not os.path.exists(config['data_path']):
        print(f"错误: 数据文件 {config['data_path']} 不存在！")
        print("请先准备训练数据文件。")
        exit(1)
    
    # 训练模型
    model, loader = train_transe(**config)
    
    # 测试加载
    test_embeddings()
    
    print("\n✅ TransE嵌入训练完成！现在可以运行 train.py 训练置信度模型了。")