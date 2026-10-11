# Judge 裁判校准数据集

第一版位于 `test_data/judge/v1/`，用于验证裁判能否客观判断预测与给定 golden 的匹配关系。golden 是题目给定参照，本基准不要求证明其业务真值、完整性或抽取质量。判定规则沿用 `eval/run.py` 的 `_build_judge_messages`。

## 第一版内容

- 131 道真实配对题：87 实体、44 关系；82 匹配、49 不匹配。
- 覆盖 17 个历史 case；素材扫描了 253 份抽取文件，去重后有 221 个预测与参照组合。
- 80 道语义题经 Codex 分段阅读完整输入并复核；51 道按确定性契约确认的格式/类型题单列。没有把历史 judge 的意见、多数投票或之前的构造数值题当答案。
- 4 道完整 2×2 窗口题，每格均有标签，检查输出候选的完整性及类型约束。这部分主要是协议检查，尚不足以覆盖大窗口中的语义遗漏。
- 8 道争议题与 24 道未复核候选留在 `review_queue.jsonl`，不参与准确率计算。
- 34 道校准题、97 道测试题按文档隔离；旧、新故障规则 case 保守合并为一个文档族。不能用测试题的模型结果反复修改提示词后，再声称独立测试。

第一版是**代理复核的初始校准集**，尚未经过独立人工复核。三位复核代理分段工作，没有对每道题重复标注，因此这不是三票一致或双人复核集。细微分类是否互斥、通知与附件身份不明等题已暂缓。版本固定的标签、理由、来源和文件哈希便于后续纠错；改变标签须更新版本、清单及相关测试。

## 规则与样例

实体需是同一本体类型和同一现实对象；别名、简称及不改变指代的同义表达可匹配。双方都填写的共享属性只在语义互相排斥时拒绝；单侧缺失、空值、无矛盾的详略差异不惩罚。分类词不同不自动等于冲突，例如“管理”与“执行”可能兼容。关系需同类型、同方向，端点结合各自实体上下文确认同指代；端点上下文用于身份判断，不机械要求端点所有属性逐字相同。证据字段不参与该匹配契约。

真实样例包含：集团与集团办公室、集团与各一级子公司；A类开发计划与B类采购计划；同一放电容量约束最小值95%与80%的冲突；标准现行与即将实施的状态冲突；公文附件名、规则与条款的同义/详略表达；关系短标签与端点上下文。

`pairs.jsonl` 每题保留 `golden`、`predicted`、`expected_match`、能力分类、文档族、split、判定理由及复核来源；`sources.json` 保留来源路径、SHA256和原集合索引对应的图快照。历史没有 manifest 的结果可以显式重新指定参照，其来源标记为 `explicit_case_reference_not_original_snapshot`，不冒充当年的原始 golden。

## 使用

先校验文件哈希、标注、来源与隔离，不发网络请求：

```powershell
.venv/Scripts/python.exe eval/tools/judge_benchmark.py --dry-run
```

Qwen 本地裁判：

```sh
python3 eval/tools/judge_benchmark.py --base-url http://127.0.0.1:9999/v1 --model Qwen/Qwen3-8B --adapter chat --split test --repeats 2
```

模型服务 URL 使用其实际可访问地址；SystemOne 适配器支持 nanojev 和 Clef：

```sh
python3 eval/tools/judge_benchmark.py --base-url http://nanojev:8189 --model nanojev --adapter systemone --key-env NANOJEV_SERVICE_API_KEY
python3 eval/tools/judge_benchmark.py --base-url http://clef:8188 --model clef-flash --adapter systemone --key-env CLEF_SERVICE_API_KEY
```

这些容器名只在相应 Docker 网络内可解析。宿主机运行时需换成实际服务地址。Chat API 的凭据默认读取 `QASYSTEM_JUDGE_API_KEY`，也可通过 `--key-env` 选择现有变量；凭据不写入结果。各模型使用相同 split、track 和标签来源；正式计时串行运行不同模型，记录当时服务上限与主机负载。服务长度限制产生的失败属于部署可用性，不自动当成不匹配。

`--track windows` 跑窗口题；`--label-source agent_review` 单独看语义题，`contract_rule` 单独看契约题。所有题都会送给裁判，不使用本地自动匹配跳过容易题。SystemOne 与 Chat 共用完整输入和判定规则，但输出协议不同；窗口测试在 SystemOne 中每格对应一道选择题，因此不能把调用速度解释为纯模型公平吞吐。

结果写入独立 `tmp/eval/judge/<时间>/`，含逐题输出、失败、耗时、语义/契约分组、能力分组、文档分组、文档宏平均、重复稳定性。分别报告有效判断准确率、包含失败的端到端准确率及覆盖率。还报告正例召回、负例识别率和 balanced accuracy，防止“全部判匹配”在不平衡数据上获得虚高分。重复轮次不作为独立样本累加；目前不自动宣称统计显著性。

扩充素材使用 `eval/tools/harvest_judge_material.py`，输出到 `tmp/`；明确的独立标注经 `eval/tools/build_judge_dataset.py` 发布。基准数据全部留在 `test_data/`，工具在 `eval/`，不进入应用交付包。
