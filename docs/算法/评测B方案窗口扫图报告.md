# 评测 B 方案窗口扫图报告

- 生成时间: 2026-10-08
- 状态: **抽样未跑完即停止**（`standard_02` 未出分；其余 5 case 已完成）
- 方案来源: `eval-scripts-20261008-164454`（纯 B：双侧窗口扫图 + NFKC 自动候选 + 类型硬过滤 + 最大匹配）
- 抽取复用: `tmp/eval/runs/eval-v2-extracts-20261003`
- golden: `test_data/eval-v2`（原 golden）
- 计划抽样: 每类最多 2 个 case
- 输出批次: `tmp/eval/runs/rejudge-eval-v2-B-window/`
- Judge: 本地 `Qwen/Qwen3-8B` @ `http://127.0.0.1:9999/v1`；窗口 `golden×predicted = 12×8`，`max_tokens=1024`

## 实现核对

当前仓库 `eval/run.py` 相对脚本包保留 B 核心能力，且无 A 短名单残留：

| 能力 | 脚本包 | 当前 `eval/` |
| --- | --- | --- |
| `_judge_windows` 双侧窗口 | 有 | 有 |
| `_auto_candidate_pairs` / `_maximum_matching` | 有 | 有 |
| 关系端点上下文 / `aligned_relations` | 有 | 有 |
| `rejected_type_pairs` / `full_sweep` | 有 | 有 |
| `eval/matching.py` / shortlist | 无 | 无 |

本地 8192 上下文下的运行补丁（不改变 B 匹配协议）：`/v1` 补全、关 thinking、`max_tokens` 按剩余预算收缩、输入超长自动拆窗、越界 pair 丢弃并记诊断。

## 已完成 case（均为 `mode=llm`）

| case | 实体 F1 | 实体 对上/多抽/漏抽 | 关系 F1 | 关系 对上/多抽/漏抽 |
| --- | ---: | --- | ---: | --- |
| official/official_01 | 0.151 | 4/15/30 | 0.071 | 3/7/72 |
| official/official_02 | 0.080 | 1/1/22 | 0.000 | 0/1/42 |
| rule/故障记录test | 1.000 | 6/0/0 | 0.000 | 0/0/0 |
| simple/Simple_故障规则的诊断 | 0.818 | 27/12/0 | 0.000 | 0/1/1 |
| standard/standard_01 | 0.520 | 98/157/24 | 0.368 | 69/163/74 |
| standard/standard_02 | — | 未完成 | — | 未完成 |

### 已完成 5 case 微平均

- 实体 F1 **0.510**（P=0.424 / R=0.642，对上/多抽/漏抽=136/185/76）
- 关系 F1 **0.285**（P=0.295 / R=0.276，对上/多抽/漏抽=72/172/189）
- 无 `identity_fallback`

## 产物

- 每完成 case: `scores.json`、`match_details.json`、`judge_view.md`
- 运行日志: `tmp/eval/runs/rejudge-eval-v2-B-window.log`
- 算法说明: [LLM-judge 评测优化说明](./LLM-judge评测优化说明.md)

## 说明

本报告只记录纯 B 窗口扫图在本抽样上的中途结果；`standard_02` 因主动停止未评分。续跑可：

```bash
python3 -m eval.rejudge_run \
  --workspace test_data/eval-v2 \
  --from-run tmp/eval/runs/eval-v2-extracts-20261003 \
  --match-mode llm \
  --include standard/standard_02 \
  --out-run rejudge-eval-v2-B-window-std02
```
