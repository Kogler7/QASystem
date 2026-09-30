---
noteId: "362a8fb1b29b11f195eb8f32b5f7472f"
tags: []

---

# 应用服务 API 索引

> 来源：[知识地图服务 Swagger UI](https://zhongche.zhouhao.work/docs) 与 [OpenAPI JSON](https://zhongche.zhouhao.work/openapi.json)，读取时间：2026-09-17。以下是当时的路径索引（78 条路径、120 个方法）；请求和响应字段以在线 OpenAPI 为准，不将此快照当作永久契约。

服务根地址：<https://zhongche.zhouhao.work>。本索引记录**应用团队服务**，QASystem 自身的 `/api/v1` 契约另见 [算法接口协议](算法接口协议.md)。服务暴露某个路径并不表示本仓库已接入该能力；已接入范围见 [外部应用服务与鉴权说明](外部应用服务与鉴权说明.md)。

## 知识沉淀接口契约

| 环节 | 方法与路径 | 请求重点 |
| --- | --- | --- |
| 上传文件资源 | `POST /knowledge-map/file-resources/upload` | `multipart/form-data`：`file`、`purpose`；单文件上限 20 MiB。返回的资源 ID 用于预览。 |
| 预览 | `POST /knowledge-map/ingestions/preview` | JSON：必填 `ingestion_type`；可传 `resource_ids`、`resource_types`、`parser_model`、`mineru_options`、`chunk_type`、`chunk_size`、`similarity_threshold` 等。`resource_types` 是元类 UID 列表，在 `structured` + `single` 算法预览时必填。 |
| 创建日志会话 | `POST /knowledge-map/ingestions/log-sessions` | 返回 `log_session_id`，可供确认入库与日志订阅使用。 |
| 确认入库 | `POST /knowledge-map/ingestions/confirm` | JSON：必填 `knowledge_space_id` 和 `graph`；可传 `log_session_id`。这是写入操作，测试时应使用专用知识空间。 |
| 订阅日志 | `GET /knowledge-map/ingestions/log-sessions/{log_session_id}/events` | 查看确认入库过程。 |

预览接口的 `chunk_type`：`0` 语义、`1` 符号、`2` 递归、`3` Markdown。该编号由应用服务 OpenAPI 定义，**不能**直接当作 QASystem 内部 `chunk_type` 编号；后者的 `0` 是递归、`4` 才是语义。插件当前发送的也是应用服务编号。预览接口不会直接接收 PlantUML 本体全文或预切好的 `chunks`；上游元类到本体、文档到切片的实际转换，需要在应用服务实现处验证。

## 全部路径目录

下面按 OpenAPI 标签分组；“未分组”沿用 OpenAPI 原始状态。查询具体字段、响应码和鉴权要求时，点击[在线接口文档](https://zhongche.zhouhao.work/docs)中的同名路径。`/mcp` 与 `/api/v1` 中标为 Mock 的接口是应用服务文档的模拟入口，不代表 QASystem 生产算法入口。

### 健康检查（3 个方法）

| 方法 | 路径 | OpenAPI 摘要 |
| --- | --- | --- |
| `GET` | `/health/live` | 应用存活检查 |
| `GET` | `/` | 应用存活检查 |
| `GET` | `/health/ready` | 应用就绪检查 |

### MCP Mock（1 个方法）

| 方法 | 路径 | OpenAPI 摘要 |
| --- | --- | --- |
| `POST` | `/mcp` | 模拟算法 MCP Streamable HTTP 接口 |

### 未分组（32 个方法）

| 方法 | 路径 | OpenAPI 摘要 |
| --- | --- | --- |
| `POST` | `/api/v1/pipelines/knowledge-ingestion` | Mock Knowledge Ingestion Pipeline |
| `POST` | `/api/v1/embeddings/texts` | Mock Text Embeddings |
| `GET` | `/knowledge-map/space-tree` | 查询业务知识库混合树 |
| `POST` | `/api/auth/login` | Login |
| `GET` | `/api/users` | List Users |
| `POST` | `/api/users` | Create User |
| `GET` | `/api/users/{user_id}` | Get User |
| `PUT` | `/api/users/{user_id}` | Update User |
| `PUT` | `/api/users/batch-disable` | Batch Disable Users |
| `PUT` | `/api/users/{user_id}/password` | Change User Password |
| `PUT` | `/api/users/{user_id}/enable` | Enable User |
| `PUT` | `/api/users/{user_id}/disable` | Disable User |
| `GET` | `/api/auth/current-permissions` | Get Current Permissions |
| `GET` | `/api/permissions/tree` | Get Permission Tree |
| `GET` | `/api/menus` | List Menus |
| `POST` | `/api/menus` | Create Menu |
| `GET` | `/api/menus/{menu_id}` | Get Menu |
| `PUT` | `/api/menus/{menu_id}` | Update Menu |
| `DELETE` | `/api/menus/{menu_id}` | Delete Menu |
| `GET` | `/api/stakeholders` | List Stakeholders |
| `POST` | `/api/stakeholders` | Create Stakeholder |
| `GET` | `/api/stakeholders/{stakeholder_id}` | Get Stakeholder |
| `PUT` | `/api/stakeholders/{stakeholder_id}` | Update Stakeholder |
| `DELETE` | `/api/stakeholders/{stakeholder_id}` | Delete Stakeholder |
| `DELETE` | `/api/stakeholders/batch` | Batch Delete Stakeholders |
| `GET` | `/api/roles` | List Roles |
| `POST` | `/api/roles` | Create Role |
| `GET` | `/api/roles/permission-tree` | Get Role Permission Tree |
| `GET` | `/api/roles/{role_id}` | Get Role |
| `PUT` | `/api/roles/{role_id}` | Update Role |
| `DELETE` | `/api/roles/{role_id}` | Delete Role |
| `DELETE` | `/api/roles/batch` | Batch Delete Roles |

### 第三方存储（8 个方法）

| 方法 | 路径 | OpenAPI 摘要 |
| --- | --- | --- |
| `POST` | `/third/mcp/proxy` | 代理算法 MCP 请求 |
| `GET` | `/third/auth/current-stakeholders` | Get Current Stakeholders |
| `POST` | `/third/files/upload` | Upload File |
| `POST` | `/third/files/batch-upload` | Batch Upload Files |
| `POST` | `/third/vectors/store` | Store Vectors |
| `POST` | `/third/knowledge/precipitation` | Preview Knowledge Precipitation |
| `POST` | `/third/embeddings/texts` | Embed Texts |
| `POST` | `/third/component/kg-update/calculate-confidence` | Component Calculate Confidence |

### 第三方查询（1 个方法）

| 方法 | 路径 | OpenAPI 摘要 |
| --- | --- | --- |
| `POST` | `/third/cypher/query` | Query Cypher |

### 临时 Neo4j 浏览器（1 个方法）

| 方法 | 路径 | OpenAPI 摘要 |
| --- | --- | --- |
| `POST` | `/temp/neo4j/query` | Query Neo4J |

### 元模型管理（27 个方法）

| 方法 | 路径 | OpenAPI 摘要 |
| --- | --- | --- |
| `GET` | `/knowledge-map/meta/packages` | List Meta Packages |
| `POST` | `/knowledge-map/meta/packages` | Create Meta Package |
| `GET` | `/knowledge-map/meta/packages/{uid}` | Get Meta Package |
| `PUT` | `/knowledge-map/meta/packages/{uid}` | Update Meta Package |
| `DELETE` | `/knowledge-map/meta/packages/{uid}` | Delete Meta Package |
| `GET` | `/knowledge-map/meta/package-tree` | Get Meta Package Tree |
| `GET` | `/knowledge-map/meta/attributes` | List Meta Attributes |
| `POST` | `/knowledge-map/meta/attributes` | Create Meta Attribute |
| `GET` | `/knowledge-map/meta/attributes/{uid}` | Get Meta Attribute |
| `PUT` | `/knowledge-map/meta/attributes/{uid}` | Update Meta Attribute |
| `DELETE` | `/knowledge-map/meta/attributes/{uid}` | Delete Meta Attribute |
| `GET` | `/knowledge-map/meta/associations` | List Meta Associations |
| `POST` | `/knowledge-map/meta/associations` | Create Meta Association |
| `GET` | `/knowledge-map/meta/associations/{uid}` | Get Meta Association |
| `PUT` | `/knowledge-map/meta/associations/{uid}` | Update Meta Association |
| `DELETE` | `/knowledge-map/meta/associations/{uid}` | Delete Meta Association |
| `GET` | `/knowledge-map/meta/constraints` | List Meta Constraints |
| `POST` | `/knowledge-map/meta/constraints` | Create Meta Constraint |
| `GET` | `/knowledge-map/meta/constraints/{uid}` | Get Meta Constraint |
| `PUT` | `/knowledge-map/meta/constraints/{uid}` | Update Meta Constraint |
| `DELETE` | `/knowledge-map/meta/constraints/{uid}` | Delete Meta Constraint |
| `GET` | `/knowledge-map/meta/classes` | List Meta Classes |
| `POST` | `/knowledge-map/meta/classes` | Create Meta Class |
| `GET` | `/knowledge-map/meta/class-options` | List Meta Class Options |
| `GET` | `/knowledge-map/meta/classes/{uid}` | Get Meta Class |
| `PUT` | `/knowledge-map/meta/classes/{uid}` | Update Meta Class |
| `DELETE` | `/knowledge-map/meta/classes/{uid}` | Delete Meta Class |

### 知识应用（2 个方法）

| 方法 | 路径 | OpenAPI 摘要 |
| --- | --- | --- |
| `POST` | `/knowledge-map/apps/initialize` | 初始化知识应用 |
| `GET` | `/knowledge-map/apps/{app_id}/space-tree` | 查询应用知识空间树 |

### 知识看板（4 个方法）

| 方法 | 路径 | OpenAPI 摘要 |
| --- | --- | --- |
| `GET` | `/knowledge-map/dashboard/spaces/tree` | 查询看板知识空间树 |
| `GET` | `/knowledge-map/dashboard/knowledge-units/resource-type-distribution` | 查询知识单元资源类型分布 |
| `GET` | `/knowledge-map/dashboard/knowledge-units/daily-created` | 查询知识单元每日新增数量 |
| `GET` | `/knowledge-map/dashboard/knowledge-units/summary` | 查询知识沉淀汇总指标 |

### 维度刻度（10 个方法）

| 方法 | 路径 | OpenAPI 摘要 |
| --- | --- | --- |
| `GET` | `/knowledge-map/dimensions` | 查询维度列表 |
| `POST` | `/knowledge-map/dimensions` | 创建维度 |
| `GET` | `/knowledge-map/dimensions/{dimension_id}` | 查询维度详情 |
| `PUT` | `/knowledge-map/dimensions/{dimension_id}` | 更新维度 |
| `DELETE` | `/knowledge-map/dimensions/{dimension_id}` | 删除维度 |
| `POST` | `/knowledge-map/scales` | 创建刻度 |
| `GET` | `/knowledge-map/scales` | 查询刻度列表 |
| `GET` | `/knowledge-map/scales/{scale_id}` | 查询刻度详情 |
| `PUT` | `/knowledge-map/scales/{scale_id}` | 更新刻度 |
| `DELETE` | `/knowledge-map/scales/{scale_id}` | 删除刻度 |

### 知识空间（9 个方法）

| 方法 | 路径 | OpenAPI 摘要 |
| --- | --- | --- |
| `GET` | `/knowledge-map/spaces/{space_id}/dimension-scales/recommendation` | 获取推荐维度刻度组合 |
| `POST` | `/knowledge-map/spaces` | 创建知识空间 |
| `GET` | `/knowledge-map/spaces` | 查询知识空间列表 |
| `GET` | `/knowledge-map/spaces/permission-tree` | 查询知识空间权限状态树 |
| `GET` | `/knowledge-map/spaces/{space_id}` | 查询知识空间详情 |
| `PUT` | `/knowledge-map/spaces/{space_id}` | 更新知识空间 |
| `DELETE` | `/knowledge-map/spaces/{space_id}` | 删除知识空间 |
| `PUT` | `/knowledge-map/spaces/{space_id}/units/{unit_id}` | 在知识空间下更新知识单元 |
| `DELETE` | `/knowledge-map/spaces/{space_id}/units/{unit_id}` | 从知识空间删除知识单元 |

### 文件资源（4 个方法）

| 方法 | 路径 | OpenAPI 摘要 |
| --- | --- | --- |
| `POST` | `/knowledge-map/file-resources` | 登记文件资源 |
| `POST` | `/knowledge-map/file-resources/upload` | 上传并登记文件资源 |
| `GET` | `/knowledge-map/file-resources/{resource_id}` | 查询文件资源 |
| `POST` | `/knowledge-map/file-resources/{resource_id}/access-url` | 生成文件资源访问地址 |

### 知识沉淀（4 个方法）

| 方法 | 路径 | OpenAPI 摘要 |
| --- | --- | --- |
| `POST` | `/knowledge-map/ingestions/preview` | 知识沉淀预览 |
| `POST` | `/knowledge-map/ingestions/log-sessions` | 创建知识沉淀入库日志会话 |
| `GET` | `/knowledge-map/ingestions/log-sessions/{log_session_id}/events` | 订阅知识沉淀入库日志 |
| `POST` | `/knowledge-map/ingestions/confirm` | 确认沉淀结果入库 |

### 知识单元（8 个方法）

| 方法 | 路径 | OpenAPI 摘要 |
| --- | --- | --- |
| `POST` | `/knowledge-map/units` | Create Unit |
| `GET` | `/knowledge-map/units` | List Units |
| `GET` | `/knowledge-map/units/{unit_id}` | Get Unit |
| `PUT` | `/knowledge-map/units/{unit_id}` | Update Unit |
| `DELETE` | `/knowledge-map/units/{unit_id}` | Delete Unit |
| `GET` | `/knowledge-map/units/{unit_id}/graph` | Get Unit Graph |
| `PUT` | `/knowledge-map/units/{unit_id}/chunks` | Update Unit Chunks |
| `POST` | `/knowledge-map/units/maintain` | 维护知识单元 |

### 知识关系（6 个方法）

| 方法 | 路径 | OpenAPI 摘要 |
| --- | --- | --- |
| `POST` | `/knowledge-map/relations` | Create Relation |
| `GET` | `/knowledge-map/relations` | List Relations |
| `GET` | `/knowledge-map/relations/{relation_id}` | Get Relation |
| `PUT` | `/knowledge-map/relations/{relation_id}` | Update Relation |
| `DELETE` | `/knowledge-map/relations/{relation_id}` | Delete Relation |
| `POST` | `/knowledge-map/relations/maintain` | 维护知识关系 |
