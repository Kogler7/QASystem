---
noteId: "e165d0e7c5fd11f192edfbc308186266"
tags: []

---

# Judge 诊断集 v1

`diagnostic-v1.json` 冻结 24 对来自 2026-10-11 v3/DeepSeek 四模型实验的预测与 golden 候选：10 个 provisional 正例、12 个难负例、2 个粒度争议。审阅来源是 `docs/算法/评测Judge人工抽查20261011.md`；不是人类 ground truth，不验证原文事实支持，不能用来估计总体准确率。

保留对应 official_01、trouble_mini、rule_01 的完整实体/关系数组与属性，以真实周边条目构造 40×20 和 4×4 窗口。golden、预测不改写。两个粒度题共用一个预测条目，独立判对不能解决一对一最大匹配的口径问题，因此不进入正负准确率。

运行隔离工具：`python -m eval.tools.judge_exploration --dataset test_data/judge_exploration/diagnostic-v1.json --output ./tmp/judge-exploration-<时间>`。需要 existing container 网络中 Clef 及 `DEEPSEEK_API_KEY`，凭据不写入数据或结果。工具只调用 judge，不调用解析、切片或抽取。

比较 API 原规模窗口、小窗、单对找边、单对 yes/no 与 Clef 单对 yes/no；每条件 3 次。单对 API 与 Clef 使用完全相同的 state、question、criteria，HTTP 输出协议不同。原窗与单对的周边条目不同，因此它们是方案对照；单对找边与单对 yes/no 才隔离任务协议变量。

`large_plus_focus_union` 对所有诊断题补判，是人为选中疑难集合的探索上限，不等于已实现生产触发器，更不代表其召回率。正例没有多数投票确认，专家可推翻 provisional 标签。

执行后的属性复核发现 q01（同名 OfficialDocument）有来文单位、联系电话、密级、领导批示差异，涉及身份与属性冲突政策。保留初始标签供审计；`docs/算法/评测Judge稳定性探索20261011.md` 和运行输出同时报告排除该题的敏感性口径，不能把任何模型在初始标签上的全对宣称为人类准确率。

## 根因诊断与完整单题扫描

`diagnostic-root-v2.json` 包含19题：已知反例、独立真实候选和合成标量控制，全部保留 agent provisional 标签及争议。`full-five-v1.json` 冻结旧五份图谱；只按类型/签名排除不兼容候选，完整3392对。工具 `python -m eval.tools.judge_root_cause --dataset <上述文件> --output tmp/<新目录> --task diagnostic|full --workers 2` 支持缓存断点，回答失败不回退；完整 case 覆盖才能评分。

新增 `eval/judge_protocols.py` 保留原始评分，同时单列标量/端点约束后的 guarded 评分，属于不同保守契约；不改生产默认。`python -m eval.tools.judge_freeze_predictions` 只冻结已保存最终预测，不触发抽取。根因证据、调用量、时间、失败与局限详见 `docs/算法/评测Judge根因与修复20261011.md`，不可把诊断标签上的改善当总体正确率。
