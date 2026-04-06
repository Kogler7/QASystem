# 产品 Backlog（可编辑）

> 说明：此文档由自动导出生成，包含每项的验收条件（AC）、估时与依赖，便于导入 Jira/Excel。

| ID | 标题 | 描述 | 验收标准 (AC) | 估时 (story points) | 优先级 | 目标迭代 | 依赖 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| BL-001 | 多源数据解析支持 | 支持 PDF/Word/Excel/HTML 等常见文档格式的解析与输出 Markdown | 上传 .pdf/.docx/.xlsx/.csv，均能返回 200 且生成 Markdown 文件；解析错误有友好提示 | 5 | 1-Highest | sprint1 | MinerU / DocxProcessor |
| BL-002 | 文件上传与解析预览 | 前端实现上传后在页面显示原始文件预览（或 PDF）与 Markdown 渲染的双栏预览 | 上传文件后可在 5s 内显示预览；用户可下载原始文件 | 2 | 1-Highest | sprint1 | PDF/Docx 转换工具 |
| BL-003 | 后端健康检查与模型列表接口 | 后端提供健康检查与可用文档处理模型查询接口 | /api/component/document-preproc/health 返回 ok；/api/available_models 列出可选模型 | 1 | 1-Highest | sprint1 | - |
| BL-004 | 解析结果保存为 Markdown 文件 | 解析后的内容保存为 repo 配置目录下的 Markdown 文件 | 解析成功后在 `backend/data/preproc_output` 生成 `.md`，并能通过 UI 下载 | 1 | 1-Highest | sprint1 | 文件系统权限 |
| BL-005 | 部署 ES 与 MySQL 持久化 | 后端部署 Elasticsearch 与 MySQL，并能把解析后数据索引/入库 | ES 可索引文档；MySQL 可存储 metadata 与结构化表 | 2 | 2-High | sprint2 | DB/ES 环境 |
| BL-006 | Excel/CSV -> MySQL 结构化存储 | 将 Excel 的每个 sheet 建表并入库，保留原始文本（不改变日期格式） | 解析结果在 MySQL 创建 `excel_*` 安全表，插入样例行；document_metadata 有记录且 mysql_stored=true | 2 | 2-High | sprint2 | MySQLClient, 表名 sanitize |
| BL-007 | 解析后 MD 存入 ES 数据库 | 解析生成的 Markdown 片段存入 Elasticsearch 索引 | 能在 ES 中查询到已索引文档片段 | 2 | 2-High | sprint2 | ESClient |
| BL-008 | 集成 MinerU 与 LibreOffice 依赖 | 集成外部处理引擎（MinerU、LibreOffice）以支持更稳定的 PDF/DOC 解析 | Doc -> PDF 转换和 MinerU 解析在服务器上可用 | 2 | 2-High | sprint3 | 系统级依赖、镜像 |
| BL-009 | 支持插件式 OCR 识别 | 支持 OCR 插件对扫描图像进行文字识别并纳入解析流程 | 上传扫描 PDF/图片，OCR 文本能够出现在 Markdown 输出中 | 2 | 2-High | sprint3 | OCR 引擎 |
| BL-010 | 模型选择机制（前端+后端） | 前端显示可选模型列表并允许用户选择模型，后端按选择路由 | 前端下拉可选模型并生效，后端使用选定模型处理 | 3 | 2-High | sprint3 | BL-003 |
| BL-011 | 批量上传/解析功能（含 ZIP 解压） | 前端支持批量上传，支持上传 .zip（客户端解压并过滤） | 上传 zip 自动解压并将合法文件入队；批量过程中可查看单文件状态与重试 | 2 | 2-High | sprint4 | JSZip 前端实现 |
| BL-012 | 优化文件解析前后对比预览 | 左侧展示原文件预览，右侧展示 Markdown 实时渲染（仅影响展示，不更改文件） | 预览页面显示左右两栏，公式通过 KaTeX 清理后展示一致 | 3 | 3-Medium | sprint4 | KaTeX, 前端渲染 |
| BL-013 | 支持压缩包上传（.zip）与自动解压 | 前端支持 drag&drop 上传 zip，过滤 __MACOSX 和隐藏文件 | ZIP 解压后入队文件数与 UI 提示一致；限制单次最多 N 个文件 | 2 | 3-Medium | sprint4 | BL-011 |
| BL-014 | 前端体验优化 | 滚动区域、加载状态、错误提示和单条失败重试 | 所有列表操作显示加载态，失败条目可单独重试 | 2 | 3-Medium | sprint4 | 前端框架（Element Plus） |

---

如需调整：我可以把每一项拆成更小的子任务并生成 CSV（含字段：ID, Title, Description, AC, Estimate, Priority, Sprint, Dependencies）。

*** End Patch