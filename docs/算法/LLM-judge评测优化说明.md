# LLM-judge 评测优化说明

面向知识抽取评测（`eval/`）的匹配与判分：**全权采用方案 B**（批次 `eval-scripts-20261008-164454`）：双侧窗口扫全图 + NFKC 自动候选 + 类型硬过滤 + 最大基数一对一匹配。方案 A（embedding 短名单 / 封闭判别）已弃用，不再作为默认或对照路径。

## 问题回顾

旧 judge 让模型在大批量条目上「发明」匹配索引，易出现：

- 索引幻觉（越界、重复、自造 pair）
- 单侧窗口把全部 golden 塞进 prompt，standard 类 case 易空数组静默漏判
- 互指（mutual nearest）在多候选下会少算 TP；应使用最大基数一对一匹配
- 容器内抽取占用 `KG_EXTRACT_LLM_*` 时，judge 与抽取抢同一上游

## B 方案行为

| 能力 | 行为 |
| --- | --- |
| NFKC 自动候选 | 类型硬一致 + 属性无冲突才免费用边；缺失属性不算冲突 |
| 类型硬过滤 | 本体 type / rel_type 不可语义同义推断；LLM 回边后再机检剔除 |
| 双侧窗口 | golden × predicted 窗口扫全图；本地匹配饱和可跳过 judge |
| 最大匹配 | Kuhn/BFS 增广；自动边与 LLM 边合并后统一分配 |
| 关系端点上下文 | judge 可见 endpoint context；对齐关系为诊断指标 |
| 门禁 / provenance | `identity_fallback` 使质量门失败；manifest 记录 judge 配置 |
| Judge 配置隔离 | `QASYSTEM_JUDGE_*` > `params.eval` > `KG_EXTRACT_LLM_*` |
| 本地 vLLM 兼容 | 自动补 `/v1`；关 thinking（`chat_template_kwargs`）；`max_tokens` 默认 2048、上限 4096 |

流程：

1. 本地自动候选 → 最大匹配；饱和则跳过 LLM  
2. 未饱和：双侧窗口扫图，模型只回报窗口内 pair 索引  
3. 类型硬过滤 → 与自动边合并 → 最大匹配  

## 代码位置

| 模块 | 职责 |
| --- | --- |
| `eval/run.py` | JudgeClient、窗口扫图、最大匹配、门禁与评分入口 |
| `eval/quality.py` / `provenance.py` / `report.py` | 质量门、清单、报告 |
| `eval/rejudge_run.py` | 复用 `backend_extract.json` 只重跑 judge（含 `judge_view.md`） |
| `eval/params.yaml` | 窗口大小、超时、并发、`judge_max_tokens` 等 |

## 配置要点

```yaml
eval:
  match_mode: llm
  judge_max_tokens: 2048
  judge_batch_size: 20
  judge_golden_batch_size: 40
  judge_max_concurrency: 4
  judge_disable_thinking: true
```

环境变量：

- `QASYSTEM_JUDGE_BASE_URL` / `QASYSTEM_JUDGE_MODEL` / `QASYSTEM_JUDGE_API_KEY`
- `QASYSTEM_JUDGE_MAX_TOKENS`（可选，覆盖 params）

## 使用

```bash
# 全量评测（线上链路）
python3 eval/run.py --match-mode llm --fail-on-empty --min-f1 0.5

# 只重跑 judge（不抽取）；每类取 2 个 case
python3 -m eval.rejudge_run \
  --workspace test_data/eval-v2 \
  --from-run tmp/eval/runs/eval-v2-extracts-20261003 \
  --match-mode llm \
  --cases-per-type 2 \
  --out-run rejudge-eval-v2-B-window
```

单测：`python3 -m pytest tests/eval -q`。

## 共享部署注意

Judge 与抽取应使用独立上游（本地 Qwen / 独立 API），避免与 `KG_EXTRACT_LLM_*` 抢配额。详见 [评测四档效果对比](./评测四档效果对比.md)。
