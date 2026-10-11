# Document Processing

该包只负责将 PDF、OFD、Word、PowerPoint、图片、Markdown、TXT、Excel、CSV 和 HTML 转换为 Markdown 或文本。

入口：

```python
from algorithm.preproc.preproc_src.main_processor import DocumentProcessor
```

Python 与系统依赖由 `backend/requirements.txt` 和根目录 `Dockerfile` 管理，应用源码由 Compose 在运行时只读挂载。
MinerU 模型通过 `/models` 外置挂载，LibreOffice 使用系统安装版本。OFD 先由镜像内置的 ofdrw 转换为 PDF，再复用 PDF 的自动解析路线；该流程不提供 OFD 电子签名验签能力。

解析引擎名称中，`vlm` 与 `mineru` 都由 MinerU 4.x 适配层承接（2.x 已退役，不再有回退路径）；`mineru4` 是显式的 4.x 入口。三者输出格式一致。

表格输出分两种：普通文件的表转 `{"columns", "rows"}` JSON；表单类文件（文件名含 `表单`）的表按阅读顺序还原为文本，不转 JSON。判定见 [文档解析行为规范](../../docs/算法/文档解析行为规范.md) §10。

完整的接口参数、自动选择和 fallback 规则见 [文档解析行为规范](../../docs/算法/文档解析行为规范.md)。
镜像依赖、模型挂载和 GPU 配置见 [文档解析部署与依赖](../../docs/部署/文档解析部署与依赖.md)。
