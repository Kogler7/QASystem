# QASystem - IntentGPT with vLLM API

这是一个意图识别系统,基于 IntentGPT 项目,使用 vLLM API 替代本地 Llama 模型,实现更高效的推理。

## 项目概述

- **原项目**: IntentGPT-inte (使用本地 Llama-3-8b-Instruct-bnb-4bit)
- **当前版本**: 使用 vLLM API 调用远程或本地 vLLM 服务
- **模型**: Qwen/Qwen3-8B (可配置)
- **数据集**: Banking77 (可扩展)

## 主要优势

1. **更高效的推理**: vLLM 提供优化的推理性能
2. **无需本地 GPU**: 可以调用远程 vLLM 服务
3. **更简单的部署**: 不需要加载大型模型到内存
4. **易于扩展**: 可以轻松切换不同的模型

## 快速开始

### 1. 环境准备

```bash
# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或 venv\Scripts\activate  # Windows

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env 文件,设置 vLLM API 配置
nano .env
```

在 `.env` 文件中设置:
```bash
VLLM_API_URL=http://127.0.0.1:8000/v1/completions
VLLM_MODEL=Qwen/Qwen3-8B
VLLM_API_KEY=  # 如果需要
```

### 3. 启动 vLLM 服务

**选项 A: 本地启动 vLLM 服务**

```bash
# 安装 vLLM (如果还没有安装)
pip install vllm

# 启动 vLLM API 服务器
python -m vllm.entrypoints.api_server \
    --model Qwen/Qwen3-8B \
    --host 127.0.0.1 \
    --port 8000
```

**选项 B: 使用远程 vLLM 服务**

如果你已经有运行中的 vLLM 服务,只需在 `.env` 文件中配置正确的 API URL。

### 4. 测试 vLLM 连接

```bash
# 使用测试脚本验证 API 连接
python llm_test.py
```

### 5. 运行意图识别

```bash
# 使用默认配置运行
python main.py config=configs/banking_vllm.yaml

# 或使用其他配置
python main.py config=configs/banking_vllm.yaml temperature=0.1
```

## 项目结构

```
qasystem-wzc-dev/
├── intentgpt/              # 核心代码
│   ├── core/
│   │   ├── intent_gpt.py   # 主要意图识别逻辑
│   │   └── embeddings.py   # 嵌入处理
│   ├── data/
│   │   ├── dataloaders.py  # 数据加载
│   │   └── processors.py   # 数据处理
│   └── utils/
│       ├── vllm_client.py  # vLLM API 客户端 [核心]
│       ├── metrics.py      # 评估指标
│       └── prompt_utils.py # 提示词工具
├── configs/                # 配置文件
│   └── banking_vllm.yaml   # Banking77 vLLM 配置
├── data/                   # 数据目录
├── prompts/                # 提示词模板
├── scripts/                # 辅助脚本
├── main.py                 # 主入口
├── llm_test.py            # API 测试工具
├── requirements.txt        # Python 依赖
├── .env.example           # 环境变量模板
├── MIGRATION_PLAN.md      # 迁移方案文档
└── README.md              # 本文件
```

## 核心功能

### 1. vLLM API 客户端

[`intentgpt/utils/vllm_client.py`](intentgpt/utils/vllm_client.py:1) 提供了与 vLLM API 交互的完整功能:

- LangChain LLM 接口兼容
- 自动重试机制
- 详细的错误处理
- 环境变量配置

**使用示例**:
```python
from intentgpt.utils.vllm_client import load_vllm_client

# 创建客户端
client = load_vllm_client(
    api_url="http://127.0.0.1:8000/v1/completions",
    model="Qwen/Qwen3-8B",
    temperature=0.15
)

# 调用模型
response = client("你好,请介绍一下你自己")
print(response)
```

### 2. 意图识别

系统支持多种意图识别策略:

- **Few-shot learning**: 使用少量示例进行学习
- **Semantic feedback**: 语义反馈机制
- **Hard prompt**: 困难样本提示
- **Dynamic discovery**: 动态意图发现

### 3. 评估指标

系统自动计算以下指标:

- **NMI** (Normalized Mutual Information): 标准化互信息
- **ARI** (Adjusted Rand Index): 调整兰德指数
- **ACC** (Accuracy): 准确率

## 配置说明

### 主要配置参数

在 [`configs/banking_vllm.yaml`](configs/banking_vllm.yaml:1) 中可以配置:

```yaml
# 模型参数
gpt_model: Qwen/Qwen3-8B  # 模型名称
temperature: 0.12          # 采样温度

# Few-shot 参数
num_few_shot_examples: 15  # Few-shot 示例数量
semantic_few_shot: true    # 使用语义 few-shot

# 语义反馈
semantic_feedback_discovery: true
k_nearest_feedback: 15

# 数据参数
known_cls_ratio: 0.50      # 已知类别比例
labeled_ratio: 0.15        # 标注样本比例
```

### 环境变量

在 `.env` 文件中配置:

```bash
# vLLM API 配置
VLLM_API_URL=http://127.0.0.1:8000/v1/completions
VLLM_MODEL=Qwen/Qwen3-8B
VLLM_API_KEY=

# 可选配置
HF_HOME=~/.cache/huggingface
TORCH_HOME=~/.cache/torch
TOKENIZERS_PARALLELISM=true
```

## 故障排除

### 问题 1: 无法连接到 vLLM API

**错误信息**: `Failed to connect to vLLM API at http://127.0.0.1:8000/v1/completions`

**解决方案**:
1. 确认 vLLM 服务正在运行
2. 检查 API URL 配置是否正确
3. 测试 API 连接: `python llm_test.py`

### 问题 2: 模型响应格式错误

**错误信息**: `Unexpected API response format`

**解决方案**:
1. 检查使用的模型是否支持
2. 调整 temperature 参数
3. 查看 vLLM 服务日志

### 问题 3: 依赖冲突

**解决方案**:
```bash
# 使用虚拟环境
python -m venv venv
source venv/bin/activate

# 重新安装依赖
pip install -r requirements.txt
```

## 性能优化

### 1. 批处理优化

在配置文件中调整 batch_size:
```yaml
batch_size: 16  # 根据 GPU 内存调整
```

### 2. 提示词优化

- 使用 `hard_prompt` 模式提高困难样本识别
- 调整 `num_few_shot_examples` 平衡性能和准确率

### 3. API 调用优化

在 [`vllm_client.py`](intentgpt/utils/vllm_client.py:1) 中:
- 已实现自动重试机制
- 已添加连接池复用
- 已设置合理的超时时间

## 与原项目的区别

| 特性 | IntentGPT-inte (原) | qasystem-wzc-dev (新) |
|------|---------------------|------------------------|
| 模型加载 | 本地 Llama-3-8b (4-bit) | vLLM API (Qwen3-8B) |
| GPU 需求 | 需要本地 GPU | 可选 (远程调用) |
| 推理速度 | 较慢 | 更快 |
| 内存占用 | 高 (~6GB) | 低 (~1GB) |
| 部署复杂度 | 高 | 低 |
| 模型切换 | 需要重新加载 | 修改配置即可 |

## 进阶使用

### 自定义模型

修改 `.env` 文件:
```bash
VLLM_MODEL=meta-llama/Llama-3-8b-hf
```

或在运行时指定:
```bash
VLLM_MODEL=your-model python main.py config=configs/banking_vllm.yaml
```

### 添加新数据集

1. 在 `data/` 目录下添加数据集
2. 创建对应的配置文件 `configs/your_dataset_vllm.yaml`
3. 运行: `python main.py config=configs/your_dataset_vllm.yaml`

### 自定义提示词

编辑 `prompts/common_prompts/` 目录下的提示词模板。

## 贡献指南

欢迎提交 Issue 和 Pull Request!

## 许可证

见 [LICENSE.txt](LICENSE.txt)

## 联系方式

如有问题,请查看 [MIGRATION_PLAN.md](MIGRATION_PLAN.md) 获取详细的技术文档。

## 更新日志

### v2.0 (当前版本)
- [完成] 使用 vLLM API 替代本地模型加载
- [完成] 新增 VLLMClient 模块
- [完成] 优化依赖管理
- [完成] 添加环境变量配置
- [完成] 完善文档和示例

### v1.0 (IntentGPT-inte)
- 使用本地 Llama-3-8b-Instruct-bnb-4bit
- 基础意图识别功能
