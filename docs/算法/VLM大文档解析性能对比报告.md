# VLM 大文档解析性能对比报告

测试日期：2026-08-06（Asia/Shanghai）

## 1. 结论

在 `tangshan88` 的 4 张 RTX 3090 上，使用同一份 33 页 PDF 进行端到端测试：

- 重构前 MinerU 进程内 VLM 基线为 `663.897s`，吞吐 `0.0497 页/s`。
- 最终单容器架构采用 12 个动态页段时为 `100.933s`，吞吐 `0.3270 页/s`，单文档延迟提升 `6.58x`。
- 自动策略采用约 `1.5 x 存活 VLM worker 数` 的页段 backlog，额外页段在共享队列等待，不占用额外模型副本。

最终调度不是前端控制的静态 GPU 绑定。入口只创建文档任务；规划层确定实际解析路线，VLM PDF 自动展开为页段原子任务；绑定具体 GPU 的 worker 从共享 `gpu.vlm` 队列领取任务后，才依据实时显存、资源预算、worker slot、GPU 租约和其他算法占用申请执行容量。单个长文档和多个并发请求复用同一套 work-stealing 调度。

## 2. 测试环境

| 项目 | 重构前基线 | 最终单容器架构 |
| --- | --- | --- |
| 主机 | `tangshan88`，8 x RTX 3090 24GB | 同一主机，隔离使用 GPU `0,1,3,6` |
| CPU | 32 个逻辑 CPU | 同左 |
| VLM | MinerU 进程内推理 | 每卡一个常驻 vLLM engine，共 4 个 |
| Worker | 单进程调用链 | 8 个 VLM dispatch worker，共享能力队列 |
| vLLM | 不适用 | `v0.11.2`，显存比例 `0.82`，`max-num-seqs=32` |
| 测试 PDF | `GBT 4549.1—2004 铁道车辆词汇 第1部分：基本词汇` | 同左 |
| 文件 | 6,907,256 bytes，33 页 | 同左 |

88 服务器是 RTX 3090 测试机，不是生产 A40。生产目标机为 8 张 A40 中只使用后四卡，应设置 `GPU_DEVICE_IDS=4,5,6,7`。A40 的实际性能和最优并发度需在部署后用相同方法复验，本报告不把 3090 结果外推为 A40 保证值。

## 3. 端到端结果

| 场景 | 端到端耗时 | 页吞吐 | 相对旧基线 | 说明 |
| --- | ---: | ---: | ---: | --- |
| 重构前，整份 PDF | 663.897s | 0.0497 页/s | 1.00x | 进程内 MinerU VLM |
| 重构后，单文档 | 100.933s | 0.3270 页/s | 6.58x | 12 页段，4 GPU/8 worker，0 重试 |

12 页段最终作业 `4c8a025ebef44da7b51f22297e521691` 使用 GPU `0,1,3,6` 和全部 8 个 VLM worker，返回 Markdown 26,074 字符，最终引擎为 VLM。各页段执行时间为 `4.897s` 到 `100.179s`，说明页面复杂度差异很大，不能只按连续页数静态四等分。

性能测试请求显式将 `max_page_shards` 限制为 12，以固定最终候选参数。交付代码中的 `EXECUTION_VLM_SHARD_COUNT=auto` 按 `ceil(存活 VLM worker 数 x 1.5)` 计算；测试拓扑有 8 个 VLM worker，因此不传分片参数时同样生成 12 个页段。这里报告的是最终 12 页段行为，不把此前其他页段数量的调参结果计入重构后性能。

## 4. Warmup 与可观测性

隔离容器重启后，四个 engine 均完成真实多模态 warmup：

```text
vLLM GPU 0 ready and warm on port 8100
vLLM GPU 3 ready and warm on port 8102
vLLM GPU 6 ready and warm on port 8103
vLLM GPU 1 ready and warm on port 8101
runtime ready: gpus=['0', '1', '3', '6'] cpu_workers=8 vlm_workers_per_gpu=2 auxiliary_workers_per_gpu=1
```

启动轮询期间的预期 connection-refused 不再逐次打印；只有最终启动失败才作为错误报告。容器增加 runtime-ready 标记，只有全部 vLLM engine 完成 warmup 且 GPU worker 已启动后，`/health` 才返回 200，避免流量入口把首个请求送入冷启动实例。

每个原子任务通过 SSE 和顶层 `execution` 暴露：页范围、worker/进程实例、GPU、endpoint、租约、尝试次数、排队/调度/开始/结束时间及各阶段耗时。最终结果仍位于原响应 envelope 的 `data`，现有业务调用默认同步行为不变；只有显式 `Prefer: respond-async` 或 Jobs API 返回任务句柄。

## 5. A40 部署配置

A40 目标机不需要手工指定分片数。建议配置：

```env
GPU_DEVICE_IDS=4,5,6,7
EXECUTION_VLM_SHARD_COUNT=auto
EXECUTION_VLM_WORKERS_PER_GPU=auto
VLLM_GPU_MEMORY_UTILIZATION=0.82
VLLM_MAX_NUM_SEQS=32
```

`max_page_shards=16` 是请求级默认保护上限，不表示固定切成 16 份。服务按存活 VLM worker 自动生成约 1.5 轮页段；实时显存余量、GPU 利用率、worker slot、资源类显存预算、GPU 租约和其他算法占用决定此刻允许执行多少任务，其余任务继续排队。首次上线应保持以上默认值，通过 `/api/v1/resources/gpus`、SSE 和 `execution` 观察实际负载后再调整 vLLM 显存比例或并发序列上限，不需要调整前端调用。

## 6. 结果边界

- 12 页段结果来自同一 PDF 的多轮热态测试，包含图编译、模型和多模态缓存收益；它代表稳定运行后的热态性能，不代表首次启动性能。
- 单次耗时会受页面图像、表格、公式和输出 token 数影响。自动分片减少慢页拖尾，但无法保证不同文档线性加速。
- 当前报告已验证 4 x RTX 3090。A40 48GB 可容纳更大的 KV cache 和更多并发序列，但仍需以生产机的显存余量、GPU 利用率、vLLM running/waiting 和其他算法占用为准动态放行。
- 性能验收应持续记录单文档 p50/p95、并发吞吐、页级耗时分布、GPU 利用率、vLLM 队列深度和失败页重试率，不能只看一次最短耗时。
