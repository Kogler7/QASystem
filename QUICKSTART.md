# 快速启动指南 - 机车检修问答意图预测系统

## 🚀 5分钟快速开始

### 1️⃣ 确认环境

```bash
cd /mnt/data3/wzc/qasystem-wzc-dev
conda activate wzcdev
```

### 2️⃣ 启动 vLLM 服务 (如果尚未运行)

```bash
# 方式1: 前台运行 (推荐测试时使用)
python -m vllm.entrypoints.openai.api_server \
    --model Qwen/Qwen3-8B \
    --host 127.0.0.1 \
    --port 8000

# 方式2: 后台运行 (推荐长期使用)
nohup python -m vllm.entrypoints.openai.api_server \
    --model Qwen/Qwen3-8B \
    --host 127.0.0.1 \
    --port 8000 > vllm.log 2>&1 &

# 检查服务状态
curl http://127.0.0.1:8000/v1/models
```

### 3️⃣ 运行意图预测

#### ✨ 交互式演示 (推荐)

```bash
python predict_intent.py
```

**输入示例问题:**
```
轴承径向游隙的标准范围是多少?
```

#### ⚡ 单次预测

```bash
python predict_intent.py -q "制动缸体内表面出现拉伤应如何处理?"
```

#### 📊 批量预测

```bash
python predict_intent.py --batch test_questions.txt
```

---

## 📋 8个意图类别

| 编号 | 类别 | 说明 |
|------|------|------|
| 1 | 参数标准 | 数值、范围、标准 |
| 2 | 部件检查 | 检查方法、状态 |
| 3 | 故障处理 | 问题解决、修复 |
| 4 | 操作规程 | 步骤、程序 |
| 5 | 试验要求 | 测试、验证 |
| 6 | 通用知识 | 原理、概念 |
| 7 | 维修限度 | 磨损、报废标准 |
| 8 | 安装要求 | 装配、固定 |

---

## 🎯 测试问题库

**test_questions.txt** 已包含8个标准测试问题(每个类别一个):

1. **参数标准**: 轴承径向游隙的标准范围是多少?
2. **部件检查**: 检查空气干燥器筒时,对上盖和筒体焊缝有何要求?
3. **故障处理**: 制动缸体内表面出现拉伤应如何处理?
4. **操作规程**: 柴油机主要紧固件的紧固力矩应遵循哪个表格的规定?
5. **试验要求**: 绝缘介电试验合格的标准是什么?
6. **通用知识**: 柴油机的工作原理是什么?
7. **维修限度**: 端盖轴承孔的圆柱度中修限度与禁用限度有何区别?
8. **安装要求**: 万向轴组装时两端叉头安装十字轴孔的中心线应在什么位置?

---

## 🔧 常见问题排查

### ❌ 问题: "CONNECTION_ERROR"

**原因**: vLLM 未运行

**解决**:
```bash
# 检查进程
ps aux | grep vllm

# 启动服务
python -m vllm.entrypoints.openai.api_server --model Qwen/Qwen3-8B --port 8000
```

### ❌ 问题: "Module not found"

**原因**: 环境未激活

**解决**:
```bash
conda activate wzcdev
pip install requests
```

### ❌ 问题: 预测结果不准确

**建议**:
- 确保问题表述清晰
- 问题应属于机车检修领域
- 可以调整 `--temperature` 参数 (更低 = 更确定)

---

## 📚 更多信息

- **详细使用说明**: 查看 [PREDICT_INTENT_README.md](PREDICT_INTENT_README.md)
- **项目配置**: [configs/track.yaml](configs/track.yaml)
- **训练数据**: [data/track/](data/track/)
- **训练结果**: [results_v3/track/](results_v3/track/)

---

## 💡 快速命令参考

```bash
# 查看帮助
python predict_intent.py --help

# 交互式
python predict_intent.py

# 单次预测
python predict_intent.py -q "你的问题"

# 批量预测
python predict_intent.py -b input.txt -o output.json

# 自定义API地址
python predict_intent.py --api-url http://localhost:8000/v1/completions

# 调整温度参数
python predict_intent.py -q "问题" --temperature 0.1
```

---

**版本**: 1.0  
**项目**: qasystem-wzc-dev  
**数据集**: 中文机车检修问答 (510训练 + 220测试)