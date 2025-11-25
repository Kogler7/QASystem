# qasystem-wzc-dev 路径重构计划

## 概述

本文档详细说明了将项目中的绝对路径改为相对路径,以及移除 [✓] 标记的修改方案。

## 修改范围

### 1. 需要修改的文件

| 文件 | 修改内容 | 优先级 |
|------|---------|--------|
| `demo_intent_simple.py` | 硬编码的 results_v3 路径 | 高 |
| `PREDICT_INTENT_README.md` | 绝对路径示例 + [✓] 标记 | 高 |
| `predict_intent.py` | [✓] 符号显示 | 高 |

### 2. 保留不变的文件

| 文件 | 原因 |
|------|------|
| `main.py` | `~/.cache` 是标准的用户缓存目录,符合 XDG 规范 |
| `scripts/generate_prompts.py` | 同上 |
| `intentgpt/core/embeddings.py` | 使用 `os.getcwd()` 获取当前目录,已经是相对路径 |

---

## 详细修改方案

### 修改 1: demo_intent_simple.py

**问题位置:** 第 28 行和第 293 行

**当前代码:**
```python
discovered_intents_path='results_v3/track/hard_prompt_15_shot_0.5_clsratio_feedback_temp_0.12_Qwen/Qwen3-8B_738796701982ca837ac0999a8ea782aa_2025-11-20_21-35-15/discovered_categories.txt'
```

**问题分析:**
- 硬编码了特定训练结果的路径
- 路径包含时间戳,不够灵活
- 用户需要手动修改才能使用不同的训练结果

**解决方案:**

#### 方案 A: 自动查找最新结果 (推荐)
```python
import glob
import os

def _find_latest_discovered_intents():
    """自动查找最新的 discovered_categories.txt 文件"""
    pattern = 'results_v3/track/*/discovered_categories.txt'
    files = glob.glob(pattern)
    if not files:
        return None
    # 按修改时间排序,返回最新的
    latest = max(files, key=os.path.getmtime)
    return latest

# 在 __init__ 中使用
discovered_intents_path = discovered_intents_path or _find_latest_discovered_intents()
```

#### 方案 B: 环境变量配置
```python
# 在 __init__ 中
default_path = os.getenv(
    'INTENT_CATEGORIES_FILE',
    'results_v3/track/latest/discovered_categories.txt'
)
discovered_intents_path = discovered_intents_path or default_path
```

#### 方案 C: 配置文件
在 `.env` 中添加:
```
DISCOVERED_INTENTS_PATH=results_v3/track/hard_prompt_15_shot_0.5_clsratio_feedback_temp_0.12_Qwen/Qwen3-8B_738796701982ca837ac0999a8ea782aa_2025-11-20_21-35-15/discovered_categories.txt
```

**推荐方案:** 方案 A (自动查找) + 方案 B (环境变量) 组合

---

### 修改 2: PREDICT_INTENT_README.md

#### 2.1 移除绝对路径 (第 53 行)

**当前内容:**
```bash
# 确保在项目目录
cd /mnt/data3/wzc/qasystem-wzc-dev
```

**修改为:**
```bash
# 确保在项目目录
cd qasystem-wzc-dev  # 或者您的项目所在目录
```

#### 2.2 移除 [✓] 标记 (第 126-129 行)

**当前内容:**
```
预测结果汇总
======================================================================
 1. [✓] 参数标准   | 轴承径向游隙的标准范围是多少?
 2. [✓] 故障处理   | 制动缸体内表面出现拉伤应如何处理?
 3. [✓] 试验要求   | 绝缘介电试验合格的标准是什么?
 4. [✓] 通用知识   | 柴油机的工作原理是什么?
======================================================================
```

**修改为:**
```
预测结果汇总
======================================================================
 1. [HIGH] 参数标准   | 轴承径向游隙的标准范围是多少?
 2. [HIGH] 故障处理   | 制动缸体内表面出现拉伤应如何处理?
 3. [HIGH] 试验要求   | 绝缘介电试验合格的标准是什么?
 4. [HIGH] 通用知识   | 柴油机的工作原理是什么?
======================================================================
```

**说明:** 使用 `[HIGH]` 表示高置信度,`[MEDIUM]` 表示中等置信度,`[LOW]` 表示低置信度

---

### 修改 3: predict_intent.py

**问题位置:** 第 347 行

**当前代码:**
```python
status = "✓" if result['confidence'] == "HIGH" else "?"
print(f"{i:2d}. [{status}] {result['intent']:8s} | {result['question']}")
```

**修改为:**
```python
status_map = {
    "HIGH": "H",
    "MEDIUM": "M", 
    "LOW": "L"
}
status = status_map.get(result['confidence'], "?")
print(f"{i:2d}. [{status}] {result['intent']:8s} | {result['question']}")
```

**输出效果:**
```
预测结果汇总
======================================================================
 1. [H] 参数标准   | 轴承径向游隙的标准范围是多少?
 2. [H] 故障处理   | 制动缸体内表面出现拉伤应如何处理?
 3. [M] 试验要求   | 绝缘介电试验合格的标准是什么?
 4. [H] 通用知识   | 柴油机的工作原理是什么?
======================================================================
```

---

## 实施步骤

### 阶段 1: 准备工作
1. ✅ 完成项目分析
2. ✅ 创建修改方案文档
3. ⏳ 备份关键文件

### 阶段 2: 代码修改
1. 修改 `demo_intent_simple.py`
2. 修改 `predict_intent.py`
3. 更新 `PREDICT_INTENT_README.md`

### 阶段 3: 测试验证
1. 测试 `predict_intent.py` 交互模式
2. 测试批量预测功能
3. 测试 `demo_intent_simple.py`
4. 验证文档示例的准确性

### 阶段 4: 文档完善
1. 更新 `README.md`
2. 创建迁移指南
3. 更新 `.env.example`

---

## 兼容性说明

### 向后兼容
- 修改后的代码保持向后兼容
- 旧的硬编码路径仍可通过参数传入
- 默认行为更智能,自动查找最新结果

### 环境要求
- Python 3.7+
- 无新增依赖
- 所有修改使用标准库

---

## 测试计划

### 单元测试
```bash
# 测试 1: 交互式模式
python predict_intent.py

# 测试 2: 单次预测
python predict_intent.py -q "轴承径向游隙的标准范围是多少?"

# 测试 3: 批量预测
python predict_intent.py --batch test_questions.txt --output results.json

# 测试 4: demo 程序
python demo_intent_simple.py
```

### 预期结果
- ✅ 所有模式正常运行
- ✅ 输出格式统一使用 [H]/[M]/[L] 标记
- ✅ 自动查找最新的训练结果
- ✅ 文档示例与实际输出一致

---

## 风险评估

| 风险 | 可能性 | 影响 | 缓解措施 |
|------|--------|------|----------|
| 找不到训练结果文件 | 中 | 高 | 提供清晰的错误提示和回退方案 |
| 字符编码问题 | 低 | 低 | 使用 ASCII 字符替代特殊符号 |
| 用户习惯变化 | 低 | 低 | 在文档中说明修改原因 |

---

## 完成标准

- [x] 所有硬编码的绝对路径已改为相对路径或配置化
- [ ] 所有 [✓] 标记已替换为 ASCII 兼容字符
- [ ] 代码通过所有测试用例
- [ ] 文档更新完成且示例正确
- [ ] 创建了迁移指南

---

## 后续优化建议

1. **配置管理:** 考虑使用 `config.yaml` 统一管理所有路径配置
2. **日志系统:** 添加详细的日志记录,方便调试路径问题
3. **路径验证:** 在程序启动时验证所有必需的文件路径
4. **Docker化:** 创建 Docker 镜像,彻底解决路径问题

---

**创建时间:** 2025-11-25  
**作者:** Architecture Mode  
**版本:** 1.0