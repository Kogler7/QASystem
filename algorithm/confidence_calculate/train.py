import torch
from torch.utils.data import DataLoader
import pytorch_lightning as pl
from pytorch_lightning.callbacks import ModelCheckpoint, EarlyStopping

from models.dynamic_hybrid import DynamicHybridModel
from utils.config import Config
from utils.data_utils import ConfidenceDataset


def main():
    # 加载配置
    config = Config('config.yaml')
    
    # 准备数据
    train_dataset = ConfidenceDataset('data/train.json', config)
    val_dataset = ConfidenceDataset('data/val.json', config)
    
    # 获取batch_size，确保至少为2
    batch_size = config.get('batch_size', 32)
    if batch_size < 2:
        print(f"警告: batch_size={batch_size} 过小，自动设置为2")
        batch_size = 2
    
    print(f"训练配置: batch_size={batch_size}")
    
    train_loader = DataLoader(
        train_dataset,
        batch_size=batch_size,
        shuffle=True,
        drop_last=True,  # 丢弃最后一个不完整的批次
        collate_fn=ConfidenceDataset.collate_fn
    )
    
    val_loader = DataLoader(
        val_dataset,
        batch_size=batch_size,
        shuffle=False,  # 验证集不需要shuffle
        drop_last=True,  # 丢弃最后一个不完整的批次，避免BatchNorm问题
        collate_fn=ConfidenceDataset.collate_fn
    )
    
    # 初始化模型
    model = DynamicHybridModel(config)
    
    # 回调函数
    checkpoint = ModelCheckpoint(
        monitor='train_loss',
        mode='min',
        save_top_k=1,
        filename='best-{epoch:02d}-{train_loss:.3f}'
    )
    early_stopping = EarlyStopping(
        monitor='train_loss',  
        patience=10,
        mode='min'
    )
    
    # 训练器
    trainer = pl.Trainer(
        max_epochs=config.get('epochs', 100),
        callbacks=[checkpoint, early_stopping],
        accelerator='auto',
        devices=1,
        log_every_n_steps=1  # 每步都记录日志，便于调试
    )
    
    # 训练
    trainer.fit(model, train_loader, val_loader)
    
    # 保存最终模型
    torch.save(model.state_dict(), 'final_model.pt')
    print("训练完成，模型已保存到 final_model.pt")


if __name__ == '__main__':
    main()