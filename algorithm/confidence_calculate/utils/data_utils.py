import torch
from torch.utils.data import Dataset
from typing import List, Dict, Tuple
import json


class ConfidenceDataset(Dataset):
    """置信度数据集"""
    def __init__(self, data_path: str, config=None):
        self.data = self._load_data(data_path)
        self.config = config
    
    def _load_data(self, path: str) -> List[Dict]:
        """加载数据"""
        with open(path, 'r') as f:
            return json.load(f)
    
    def __len__(self):
        return len(self.data)
    
    def __getitem__(self, idx):
        item = self.data[idx]
        triple = item['triple']
        label = torch.tensor(item['label'], dtype=torch.float)
        return triple, label
    
    @staticmethod
    def collate_fn(batch):
        """批处理"""
        triples = [item[0] for item in batch]
        labels = torch.stack([item[1] for item in batch])
        return triples, labels