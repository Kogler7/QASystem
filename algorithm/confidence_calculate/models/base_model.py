import pytorch_lightning as pl
import torch
import torch.nn as nn
from torchmetrics.functional import accuracy
from typing import List, Any
import random
import numpy as np

random.seed(42)
np.random.seed(42)
torch.manual_seed(42)
torch.cuda.manual_seed_all(42)


class BaseConfidenceModel(pl.LightningModule):
    """
    置信度回归模型（输出0-1之间的连续值）
    """
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.name = 'BaseConfidenceModel'
        self.learning_rate = config.get('learning_rate', 0.001)
        self.l2_lambda = config.get('l2_lambda', 0.001)
        
        # 用于存储训练和验证步骤的输出
        self.training_step_outputs = []
        self.validation_step_outputs = []
        
    def configure_optimizers(self):
        """优化器配置"""
        return torch.optim.Adam(self.parameters(), lr=self.learning_rate)
    
    def loss_function(self, pred, target):
        """损失函数：均方误差（MSE）用于回归"""
        pred = pred.view(-1)
        return nn.MSELoss()(pred, target)
    
    def forward_triples(self, *args, **kwargs):
        """前向传播接口，子类必须实现"""
        raise NotImplementedError(f'Model {self.name} must implement forward_triples')
    
    def training_step(self, batch, batch_idx):
        """训练步骤"""
        triple_data, labels = batch
        
        predictions = self.forward_triples(triple_data)
        predictions = predictions.view(-1)
        loss = self.loss_function(predictions, labels)
        
        # L2 正则化
        l2_norm = sum(p.pow(2.0).sum() for p in self.parameters())
        loss = loss + self.l2_lambda * l2_norm
        
        # 对于回归任务，计算平均绝对误差作为指标
        mae = torch.abs(predictions - labels).mean()
        
        # 保存输出供 on_train_epoch_end 使用
        self.training_step_outputs.append({'loss': loss, 'mae': mae})
        
        return {'loss': loss, 'mae': mae}
    
    def on_train_epoch_end(self):
        """训练 epoch 结束（新版钩子）"""
        if self.training_step_outputs:
            avg_loss = torch.stack([x['loss'] for x in self.training_step_outputs]).mean()
            avg_mae = torch.stack([x['mae'] for x in self.training_step_outputs]).mean()
            self.log('train_loss', avg_loss, prog_bar=True)
            self.log('train_mae', avg_mae, prog_bar=True)
            
            # 清空缓存
            self.training_step_outputs.clear()
    
    def validation_step(self, batch, batch_idx):
        """验证步骤"""
        self.eval()
        
        with torch.no_grad():
            triple_data, labels = batch
            predictions = self.forward_triples(triple_data)
            predictions = predictions.view(-1)
            loss = self.loss_function(predictions, labels)
            mae = torch.abs(predictions - labels).mean()
        
        # 保存输出供 on_validation_epoch_end 使用
        self.validation_step_outputs.append({'val_loss': loss, 'val_mae': mae})
        
        return {'val_loss': loss, 'val_mae': mae}
    
    def on_validation_epoch_end(self):
        """验证 epoch 结束（新版钩子）"""
        if self.validation_step_outputs:
            avg_loss = torch.stack([x['val_loss'] for x in self.validation_step_outputs]).mean()
            avg_mae = torch.stack([x['val_mae'] for x in self.validation_step_outputs]).mean()
            self.log('val_loss', avg_loss, prog_bar=True)
            self.log('val_mae', avg_mae, prog_bar=True)
            
            # 清空缓存
            self.validation_step_outputs.clear()
    
    def test_step(self, batch, batch_idx):
        """测试步骤"""
        self.eval()
        with torch.no_grad():
            triple_data, labels = batch
            predictions = self.forward_triples(triple_data)
            predictions = predictions.view(-1)
            mae = torch.abs(predictions - labels).mean()
        return {'test_mae': mae}
    
    def on_test_epoch_end(self):
        """测试 epoch 结束（新版钩子）"""
        # 如果需要聚合测试结果，可以在这里实现
        pass