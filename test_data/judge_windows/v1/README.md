# 真实多候选 Judge 对比数据

`windows.jsonl` 是来自88历史预测/golden快照的16个自然窗口、5份文档，沿用正式40个golden×20个predicted分窗。6个窗口为满40×20，其余是自然尾窗，原顺序、属性、关系端点上下文保留。仅16个已有审核匹配点有标签，其余候选未知，不能把未标注边当成负例或宣称全窗口F1正确率。

`small_reviewed.jsonl` 是从同文档真实图谱选出的6个4×4子图，实体和关系各3个，完整代理复核96格。85个同类型语义判断存入labels，11个程序类型约束负例单列contract_labels，完整可匹配边为expected_pairs。这些标签不是API输出，不是独立人工真值，也不是随机代表性业务样本。

清单包含来源hash、原图索引、标签范围及限制。没有修改原始实体/关系来凑正确答案。小窗口的评分参照由expected_pairs做最大一对一匹配后计算；大窗口只报告已标注点上的表现及模型评分漂移，不提供全图真值。

`eval/tools/judge_window_compare.py` 调用正式 `_judge_collection`，包括自动匹配、冲突项补判、类型过滤和最大匹配。每窗口3次同输入，再反转两侧顺序一次并逆映射索引。纯chat LLM，无SystemOne/Jev调用；不触发解析、切片或抽取。正式auto响应格式、temperature=0、关闭thinking、输出预算8192，窗口并发2。API实际HTTP尝试数有硬预算，失败不得当成稳定或正确。

示例（凭据仅环境变量）：

```bash
python3 -m eval.tools.judge_window_compare --dataset test_data/judge_windows/v1/windows.jsonl --output tmp/eval/judge-windows-new/api --model deepseek-flash --url https://api.deepseek.com/v1 --key-env DEEPSEEK_API_KEY --budget 110
python3 -m eval.tools.judge_window_compare --dataset test_data/judge_windows/v1/small_reviewed.jsonl --output tmp/eval/judge-small-new/api --model deepseek-flash --url https://api.deepseek.com/v1 --key-env DEEPSEEK_API_KEY --budget 40
python3 -m eval.tools.judge_window_report --dataset test_data/judge_windows/v1/windows.jsonl --results tmp/eval/judge-windows-new
```

详见 `docs/算法/LLM多候选Judge对比20261012.md`。数据和脚本不进入运行时源码交付包；输出只写tmp。
