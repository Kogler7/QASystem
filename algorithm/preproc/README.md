# 文档预处理组件 (algorithm/preproc)

将 PDF、DOCX、Excel、HTML 等文档转换为 Markdown/文本，供后续检索与知识抽取使用。支持 MinerU（PDF）、Docling（DOCX）、pandas/openpyxl（Excel）等。

## 依赖安装

在项目根目录或本目录下执行（建议在虚拟环境中）：

```bash
# 方式一：项目根目录下安装
cd /path/to/qasystem
pip install -r algorithm/preproc/requirements.txt

# 方式二：仅当前用户（若遇权限错误）
pip install --user -r algorithm/preproc/requirements.txt

# 方式三：使用虚拟环境（推荐）
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# 或 .venv\Scripts\activate  # Windows
pip install -r algorithm/preproc/requirements.txt
```

依赖较多时（含 torch、mineru、docling）安装时间可能较长。若仅需 DOCX/Excel 转换、不处理 PDF，可注释掉 `requirements.txt` 中的 `mineru` 再安装以减小体积。

## 可选配置

- **MinerU**：PDF 解析依赖 MinerU，需配置文件与模型。环境变量：
  - `MINERU_CONFIG_PATH`：配置文件路径（如 `mineru.json`）
  - `MINERU_MODEL_SOURCE`：`local` / `modelscope` / `huggingface`
- **数据库**：当前后端对接未启用 ES/MySQL（`enable_db_storage=False`），若需存储可改算法或服务层配置。

## 后端对接

后端通过 `backend/app/modules/component/document_preproc/service.py` 进程内调用本模块，接口见 `GET /api/component/document-preproc/health`、`POST /api/component/document-preproc/convert`（文件上传）。
