# GitHub 同步

主仓库仍为 `origin`：`git@10.126.62.88:qasystem/qasystem.git`。GitHub 副本为 `github`：`git@github.com:Kogler7/QASystem.git`。开发、合并和发布继续在主仓库进行，GitHub 接收清理过的已发布历史，不读取工作区未提交修改，也不把 GitHub 改动合回主仓库。

## 为什么不能直接推原历史

2026-10-14 审计发现旧提交包含部署 `.env`、`.claude/settings.local.json` 中的凭据，模型权重、训练 checkpoint、Chroma 数据库和误提交的 Python 虚拟环境。凭据值不在报告中展示。最大的 checkpoint 约 1.43 GB，超过 [GitHub 普通 Git 文件的 100 MiB 上限](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)。当前 `origin/develop` 的源码树已经没有这些产物，但删除当前文件不能清除历史。

用户已授权修改 Git 历史。清理只在本地独立 bare 副本执行，保留其余提交、空提交和合并拓扑，不修改主仓库 refs、不强推主仓库、不切换或暂存开发工作区。

## 过滤策略 v1

完整规则在 `tools/sync_github.py` 的 `EXCLUDED` 和路径正则中维护：

| 分类 | 历史路径或范围 |
|---|---|
| 部署及个人凭据 | 任意目录的 `.env`；`.claude/settings.local.json` |
| 模型与大二进制 | `algorithm/NL_to_cypher/bge-small-zh-v1.5/`；`algorithm/confidence_calculate/sbert_model/`、`embeddings/`、`final_model.pt`；`algorithm/preproc/LibreOffice-fresh.full-x86_64.AppImage` |
| 训练、数据库产物 | 任意目录的 `chroma_db/`、`lightning_logs/`；`.sqlite`、`.sqlite3`、`.db` 文件；包括历史中文目录下的 Chroma 数据库和顶层 `embeddings/` SQLite 文件 |
| 权重扩展名 | `.safetensors`、`.ckpt`、`.pt`、`.bin` |
| 误提交环境 | `frontend/frontend/` |
| 运行和生成文件 | `tmp/`、`runtime-config/`、`deploy/runtime/` |
| GitHub 文件限制 | 大于 100 MiB 的 blob |

保留 `.env.example` 等示例、业务源码、测试、golden 数据和文档。过滤后再次扫描全部可达 blob，检查敏感路径、文件大小、私钥及常见 API 凭据形态；出现新疑似凭据或未覆盖产物会停止发布，仅输出路径与原因。此扫描是门禁，不宣称可以自动识别所有种类的秘密。新发现需要核查并更新策略，不能绕过扫描。

历史清理改变相关提交 hash。每次输出的 `source-map.json` 记录策略版本、源仓库、源 refs 和清理后 refs；git-filter-repo 同时保存逐提交映射。当前策略不裁剪空提交，也不折叠合并，因此保留功能分支拓扑。若未来同步签名标签，Git 历史重写将使原签名失效；当前源仓库没有标签。

## 操作命令

使用 Python 3.10+、Git，以及开发工具 `git-filter-repo==2.47.0`。它不是应用运行依赖，不需要重建环境镜像。

```powershell
.venv/Scripts/python.exe -m pip install git-filter-repo==2.47.0
# 仅生成、审计独立清理副本；不推送
.venv/Scripts/python.exe tools/sync_github.py
# 审计通过后同步 GitHub，普通 atomic push，不强推
.venv/Scripts/python.exe tools/sync_github.py --publish
```

Linux 可将 Python 命令替换为安装了上述工具的 `python3`。首次及后续使用同一入口。脚本先 fetch 主仓库的全部分支和标签，读取 `origin/*` 等已发布 refs，生成新的隔离清理副本，再向 GitHub 同名分支、标签一次性推送。不会更新本地开发分支，不会删除 GitHub 独有分支，不会自动 stash、checkout 或批准业务配置。

默认缓存为 `tmp/github-sync-v1/`，其中含清理副本和来源映射。最近两个完成的副本保留在缓存，来源和逐提交映射集中保存于 mappings；源refs不变时复用副本并重新审计。缓存不进入 Git、不进入交付源码包。过滤过程中临时副本可能通过只读 Git alternates 访问原始对象，因此缓存也应仅保存在当前受控开发机；最终推送的只是清理后 refs 的可达对象。

固定策略对相同源历史产生相同的清理 hash。源分支正常前进后，GitHub 对应清理分支也可以普通快进更新。策略版本改变、源分支被重写、GitHub 分支被独立修改，都会需要重新审议；脚本不自动 force，不自动删除目标 refs。原子推送被拒绝时整批失败，退出码为 1；SSH、源连接或审计失败也返回 1，不能记作已同步。

后续周期任务执行同一 `--publish` 命令；只有更新、失败或需要人工处理时通知。任务建立和首次实际推送结果由主线程确认，SSH 连接成功只证明权限可用，不能据此认定 GitHub 仓库是私有或公开。

## 检查依据

第一轮清理基于 `origin/develop=bb49f38`：16 个分支、0 标签；可达 blob 版本由 6144 降至 5398，最大 blob 为 20,950,679 字节。当前 develop 的 1244 个文件、36,342,170 字节原样保留。独立 clone 的配置声明门禁通过。最终推送前必须再次 fetch，以包含本轮配置整理的新提交。

相关单测使用临时本地仓库验证确定性过滤、连续同步、非快进拒绝、凭据阻断、源连接失败和策略变更阻断；不会用 GitHub 做破坏性测试。
