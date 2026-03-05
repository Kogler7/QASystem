# 意图识别模块

由后端进程内调用，不单独起服务。依赖 **Milvus**、**DeepSeek API**、**BGE-M3 模型** 等，通过环境变量配置。

## 后端如何加载

- 后端在 `backend/app/services/intent_recognition_service.py` 中：
  - 若存在 `algorithm/intent_recognition_model/.env`，会先加载该文件（不覆盖已有环境变量）
  - 将 `algorithm/intent_recognition_model` 加入 `sys.path`，再 `from intent_recognition.intent_app import IntentRecognition`
- 配置可放在两处之一（或同时使用，先加载的不被覆盖）：
  - **推荐**：`algorithm/intent_recognition_model/.env`
  - 或在 `backend/.env` / 系统环境中配置下列变量

## 环境变量说明

| 变量 | 必填 | 说明 | 默认或示例 |
|------|------|------|------------|
| **MILVUS_URI** | 是 | Milvus 服务地址 | `http://localhost:19530` |
| **COLLECTION_NAME** | 是 | Milvus 集合名 | `intent_rag` |
| **MODEL_PATH** | 是 | BGE-M3 模型目录（含模型文件） | `/mnt/data3/srv/qasystem/models` 或 `.../models/bge_m3` |
| **DEEPSEEK_API_KEY** | 是 | DeepSeek API Key（意图识别 LLM） | `sk-xxx` |
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

4. **DeepSeek**：在 [DeepSeek](https://platform.deepseek.com/) 获取 API Key，写入 `DEEPSEEK_API_KEY`。

5. **.env**：复制 `.env.example` 为 `.env` 并填写上述变量：
   ```bash
   cp algorithm/intent_recognition_model/.env.example algorithm/intent_recognition_model/.env
   # 编辑 .env 填写 MILVUS_URI、MODEL_PATH、DEEPSEEK_API_KEY、DATA_DIR 等
   ```

6. **健康检查**：启动后端后请求 `GET /api/component/intent-recognition/health`，返回 `"status":"ok"` 即表示模块可用。
