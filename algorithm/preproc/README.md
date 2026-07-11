# Document Processing

该包只负责将 PDF、Word、Excel、CSV 和 HTML 转换为 Markdown 或文本。

入口：

```python
from algorithm.preproc.preproc_src.main_processor import DocumentProcessor
```

Python 与系统依赖统一由 `backend/requirements.txt` 和 `docker/backend.Dockerfile` 管理。
MinerU 模型通过 `/models` 外置挂载，LibreOffice 使用系统安装版本。
