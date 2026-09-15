# `external_services/` 说明

`external_services/` 是系统与**外部服务**之间的统一适配层：一边是应用团队的**图谱/向量/文件**接口，
一边是**模型服务**（Embedding 等）。所有外部调用都从这里出入，其它模块不直接发 HTTP。

核心思想：**能力注册制**——每个外部能力在注册表里声明，未开通的能力在**调用时**才报错，不影响其它功能。

---

## 1. 文件

| 文件 | 作用 |
| --- | --- |
| `application.py` | 应用团队统一接口客户端：能力注册表、鉴权上下文透传、Cypher 查询、文件上传、向量写入 |
| `models.py` | 模型服务客户端（当前为远程 Embedding 客户端）与错误类型 |

---

## 2. `application.py` —— 应用团队统一接口

### 2.1 能力注册表 `ENDPOINTS`

| 能力标识 | 默认路径 | 说明 |
| --- | --- | --- |
| `graph.cypher.query` | `/third/cypher/query` | 图谱 Cypher 查询（图谱检索的唯一出口） |
| `graph.write` | 未开通 | 图谱写入（未开通时调用即抛“能力不可用”） |
| `vectors.store` | `/third/vectors/store` | 向量写入 |
| `files.upload` | `/third/files/upload` | 文件上传 |
| `confidence.calculate` | 未开通 | 置信度计算 |
| `documents.evidence.search` | 未开通 | 文档证据检索 |

- 能力路径可用环境变量覆盖（注册表中每项都带对应的环境变量名）；
- 未配置路径的能力调用时抛 `ExternalCapabilityUnavailable`，由上层转换为明确的错误码。

### 2.2 鉴权上下文

- 请求携带的用户上下文头（`USER_CONTEXT_HEADERS`：授权、用户、租户、角色等）由后端捕获后放入上下文变量；
- `bind_user_headers()` / `capture_user_headers()` / `get_user_headers()` 负责透传；
- 所有外部请求自动带上这些头，**图谱查询因此天然带权限过滤**。

### 2.3 客户端 `ApplicationServiceClient`

| 方法 | 作用 |
| --- | --- |
| `request(capability, ...)` | 统一请求入口：拼路径、带头、状态码与业务码校验 |
| `query_cypher(statement, parameters)` | 执行 Cypher，返回原始数据对象 |
| `execute_cypher(statement, parameters)` | 执行 Cypher 并规整为**行字典列表**（列名 → 值） |
| `upload_file(...)` | 上传文件，返回 id/名称/路径/URL |
| `store_vectors(items, ...)` | 写入向量（`{vector, text}` 列表 + 知识空间/文件标识） |

错误类型：

| 异常 | 含义 |
| --- | --- |
| `ExternalCapabilityUnavailable` | 能力未注册或未开通（调用时才报） |
| `ApplicationServiceError` | 调用失败（网络、HTTP 状态、业务码非 0、返回体异常） |

---

## 3. `models.py` —— 模型服务

| 内容 | 作用 |
| --- | --- |
| `RemoteEmbeddingClient` | 远程 Embedding 客户端（OpenAI 兼容 `/v1/embeddings`）：批量分段、鉴权头、超时 |
| `encode(texts)` | 输入文本列表 → 向量列表 |
| `ModelServiceError` | 远程模型服务调用失败 |

被 `algorithm/embedding_runtime.py` 使用：本地模型不可用时自动改走该远程客户端。

---

## 4. 调用关系

```text
algorithm/（业务实现）
   └──▶ external_services
           ├── application.ApplicationServiceClient ──▶ 应用团队接口（图谱/向量/文件）
           └── models.RemoteEmbeddingClient         ──▶ 模型服务（Embedding）
```

约定：

1. **所有第三方调用统一走本层**，其它模块不直接发 HTTP；
2. **图谱查询统一走 `graph.cypher.query`**，自动带用户鉴权上下文；
3. 未开通能力**只在其被调用时报错**，不阻塞其它能力；
4. 外部返回的行数据统一规整为“列名 → 值”的字典，便于上层直接使用。
