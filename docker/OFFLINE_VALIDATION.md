# 封闭网络验收

该验收环境用于模拟目标服务器的内网部署。它使用 `internal: true` Docker 网络，Backend 无法访问互联网或宿主机网络；Mock LLM 仅在该内部网络提供 OpenAI 兼容接口。

准备好最终镜像和 `models/` 后，在仓库根目录运行：

```powershell
.\run-offline-validation.ps1 -ImageName qasystem-runtime-v1 -ModelsDir .\deploy\models
```

验收使用 `test_artifacts/parser/解析测试数据` 中的 PDF、扫描 PDF、DOC、XLSX、Markdown 和 PNG 样本，并验证：

- `GET /health?full=true` 的本地运行依赖与模型目录；
- Docling、MinerU VLM、Pandas 和文本解析路由；
- 512 维 `bge-small-zh-v1.5` 向量；
- 本地 Mock LLM 的 OpenAI 流式知识抽取；
- 完整知识沉淀流水线。

验收结束后容器与内部网络会自动删除。正式服务仍使用根目录的 `docker-compose.yml`。
