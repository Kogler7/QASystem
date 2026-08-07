# Document Processing

该包只负责将 PDF、OFD、Word、PowerPoint、图片、Markdown、TXT、Excel、CSV 和 HTML 转换为 Markdown 或文本。

入口：

```python
from algorithm.preproc.preproc_src.main_processor import DocumentProcessor
```

Python 与系统依赖由 `backend/requirements.txt` 和根目录 `Dockerfile` 管理，应用源码由 Compose 在运行时只读挂载。
MinerU 模型通过 `/models` 外置挂载，LibreOffice 使用系统安装版本。OFD 先由镜像内置的 ofdrw 转换为 PDF，再复用 PDF 的自动解析路线；该流程不提供 OFD 电子签名验签能力。

解析引擎名称中，`vlm` 表示视觉语言模型解析，`mineru` 表示传统专用模型流水线解析。

完整的接口参数、自动选择和 fallback 规则见 [文档解析行为规范](../../docs/文档解析行为规范.md)。
镜像依赖、模型挂载和 GPU 配置见 [文档解析部署与依赖](../../docs/文档解析部署与依赖.md)。
