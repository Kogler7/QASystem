# 置信度计算模块 - 故障排除

## Error while deserializing header: incomplete metadata, file not fully covered

该错误表示**某个模型/权重文件不完整或损坏**（例如下载中断、磁盘写满、进程被杀死）。按下面步骤排查并修复。

### 1. 确定是哪个文件出错

- **SBERT 模型**：加载时在日志中会看到「加载SBERT模型从: …」，若在此之后报错，多半是 `sbert_model/` 下的 `model.safetensors` 不完整。
- **实体/关系嵌入**：若在创建 KGE 证据组件时报错，可能是 `embeddings/entities.pt` 或 `embeddings/relations.pt` 损坏。
- **最终模型**：若在「加载模型」且 SBERT/嵌入已加载成功后报错，则是 `final_model.pt` 不完整。

### 2. 修复方法

**SBERT 模型不完整：**

```bash
cd /home/ycheng/ZhCHRAG/qasystem/algorithm/confidence_calculate
# 删除旧的不完整模型目录（请先备份如需）
rm -rf sbert_model
# 重新下载（会从 HuggingFace 镜像拉取并保存到 ./sbert_model）
python temp_utils/download_sbert.py
```

确保 `config.yaml` 中 `text.sbert_model_path` 指向上述 `sbert_model` 的绝对路径或相对本目录的正确路径。

**实体/关系嵌入（entities.pt、relations.pt）不完整：**

```bash
cd /home/ycheng/ZhCHRAG/qasystem/algorithm/confidence_calculate
# 使用你的知识图谱数据重新训练 TransE 嵌入，会生成 embeddings/entities.pt 和 embeddings/relations.pt
python train_transe_embeddings.py
```

需在 `train_transe_embeddings.py` 或对应配置里指定正确的数据路径。

**final_model.pt 不完整：**

需要重新训练置信度模型，生成新的 `final_model.pt`（参见本模块的训练脚本与 README）。

### 3. 预防

- 下载 SBERT 时保证网络稳定，或使用镜像（如 `HF_ENDPOINT=https://hf-mirror.com`）。
- 避免在写入 `.pt` / `model.safetensors` 时中断进程或断磁盘空间。
