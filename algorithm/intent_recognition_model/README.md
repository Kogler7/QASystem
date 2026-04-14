# 意图识别模块

由后端进程内调用，不单独起服务。依赖 **Milvus**、**DeepSeek API**、**BGE-M3 模型** 等，通过环境变量配置。

## 后端如何加载

- 后端在 `backend/app/modules/component/intent_recognition/service.py` 中将本目录加入 `sys.path`，再 `from intent_recognition.intent_app import IntentRecognition`。
- 环境变量由 `backend/app/config.py` 统一加载：**先** `backend/.env`，**再** 仓库根 `.env`（仅补缺）；与全项目其它组件一致。
- **推荐**：在 **`backend/.env`**（或 Docker 下仓库根 `.env`）中配置下表变量。
- **仅单独运行本目录脚本**（如 `init_database`）时，可复制 `.env.example` 为同级 `.env`；`ir_config/config.py` 会以 `override=False` 加载，且不覆盖已导出的环境变量。

## 环境变量说明

| 变量 | 必填 | 说明 | 默认或示例 |
|------|------|------|------------|
| **MILVUS_URI** | 是 | Milvus 服务地址 | `http://localhost:19530` |
| **COLLECTION_NAME** | 是 | Milvus 集合名 | `intent_rag` |
| **MODEL_PATH** | 是 | BGE-M3 模型目录（含模型文件） | `models/bge_m3_model` 或 `models/bge_m3` |
| `LLM_*` / `DEEPSEEK_API_KEY` | 是 | 意图识别 LLM（与 `algorithm/common` 一致） | 见 `backend/.env.example` |
| **DATA_DIR** | 建议 | 意图数据 JSON 路径 | `../data/intent.json` 或绝对路径 |
| **LOG_PATH** | 否 | 日志目录 | 不设则用项目根下 `logs` |
| MILVUS_CONSISTENCY_LEVEL | 否 | 一致性级别 | `Strong` |
| MAX_DROP_RETRIES / DROP_RETRY_DELAY | 否 | 建表重试 | `3` / `2` |

## 配置步骤

1. **安装依赖**（在含本模块的虚拟环境中）：
   ```bash
   pip install -r algorithm/intent_recognition_model/requirements.txt
   ```

2. **Milvus**：确保 Milvus 已启动，且 `MILVUS_URI` 可访问。

3. **BGE-M3 模型**：将模型放到 `MODEL_PATH` 所指目录（或设置 `MODEL_PATH` 指向已有目录）。

4. **LLM**：在 `backend/.env` 配置 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`（或 `DEEPSEEK_API_KEY` 等），与项目统一 LLM 变量一致。

5. **配置位置（走后端 API）**：在 **`backend/.env`** 中填写 `MILVUS_URI`、`MODEL_PATH` / `INTENT_MODEL_BASE_DIR`、`DATA_DIR` 等（参考 `backend/.env.example`）。仅跑本目录脚本时再使用同级 `.env`：
   ```bash
   cp algorithm/intent_recognition_model/.env.example algorithm/intent_recognition_model/.env
   ```

6. **健康检查**：启动后端后请求 `GET /api/component/intent-recognition/health`，返回 `"status":"ok"` 即表示模块可用。
