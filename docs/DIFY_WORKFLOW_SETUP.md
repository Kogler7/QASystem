# 智能问答接入 Dify 配置教程

本教程包含两种方式：**聊天助手**（对话型应用，配置简单）与**工作流**（Workflow，可自定义流程）。

---

## 方式一：使用 Dify 聊天助手（推荐，无需工作流）

若只想要「用户提问 → 模型回复」、且希望多轮对话带上下文，可直接用 Dify 的**对话型应用**（Chatflow），无需配置工作流。

### 步骤 1：在 Dify 中创建对话型应用

1. 登录 Dify，进入「工作室」。
2. 点击「创建应用」→ 选择 **「对话型」**（Chatflow），填写应用名称（如「铁路问答助手」）。
3. 在应用里配置「对话」：选择模型、写系统提示词等，保存。
4. 点击右上角「发布」。
5. 进入「API 访问」→ 复制 **API Key**（注意是**该对话型应用**的 Key，不是工作流的）。

### 步骤 2：后端 .env 配置

在项目 **`backend`** 目录的 `.env` 中：

```env
# 使用聊天助手时：关闭工作流
DIFY_USE_WORKFLOW=false

# Dify 接口地址（云版或自托管一致）
DIFY_API_URL=https://api.dify.ai/v1

# 必填：填写上面「对话型应用」的 API Key
DIFY_API_KEY=app-xxxxxxxxxxxxxxxxxxxxxxxx
```

**说明**：只要 `DIFY_USE_WORKFLOW` 为 `false` 或未配置 `DIFY_WORKFLOW_API_KEY`，智能问答就会走 `/chat-messages` 接口，使用 `DIFY_API_KEY` 对应的聊天助手。无需改任何代码，重启后端即可。

---

## 方式二：使用 Dify 工作流

下面说明如何在「智能问答」模块接入 Dify 的**工作流**（Workflow），使用基础流程：**输入 → LLM → 输出**。

---

## 一、项目中的智能问答与 Dify 关系

| 模块 | 说明 |
|------|------|
| 前端 | `src/views/ChatView.vue`：问答界面；`src/api/chat.js`：调用后端 `/api/chat` |
| 后端 | `backend/app/routers/chat.py`：处理 `/api/chat`，根据配置走「对话型」或「工作流」 |
| Dify 客户端 | `backend/app/services/dify_client.py`：`chat()` 为对话型，`workflow_run()` 为工作流 |

当 `DIFY_USE_WORKFLOW=true` 且配置了 `DIFY_WORKFLOW_API_KEY` 时，智能问答会调用 Dify 工作流接口；否则使用原来的对话型接口（`/chat-messages`）。

---

## 二、在 Dify 中创建「输入 → LLM → 输出」工作流

### 步骤 1：创建应用并选择工作流

1. 登录 Dify，进入「工作室」。
2. 点击「创建应用」→ 选择 **「工作流」**（Workflow），填写应用名称（如「铁路问答工作流」）。
3. 进入应用后，会看到画布上的 **开始** 节点。

### 步骤 2：配置「开始」节点（输入）

1. 点击画布中的 **「开始」** 节点。
2. 在右侧面板「变量」中新增一个输入变量，用于接收用户问题：
   - **变量名**：必须与后端配置的 `DIFY_WORKFLOW_INPUT_VAR` 一致，默认建议填 **`query`**。
   - **类型**：选「文本」或「段落」。
   - **必填**：建议勾选。
   - **标题/描述**：可填「用户问题」等，便于在画布上识别。
3. 保存。后续 API 调用时，会把用户输入以 `inputs[变量名]` 传入。

**参数小结（开始节点）**

| 配置项 | 建议值 | 说明 |
|--------|--------|------|
| 变量名 | `query` | 与后端 `.env` 中 `DIFY_WORKFLOW_INPUT_VAR` 一致 |
| 类型 | 文本/段落 | 按需选择 |
| 必填 | 是 | 避免空请求 |

### 步骤 3：添加并配置 LLM 节点

1. 从左侧节点列表拖入 **「LLM」** 节点（或「大语言模型」），连接到「开始」节点之后。
2. 在 LLM 节点配置中：
   - **模型**：选择要用的模型（如 GPT-3.5、本地模型等）。
   - **系统提示词**（若有）：可写「你是一个铁路客服助手」等。
   - **用户输入/提示词**：在变量引用处选择「开始」节点下的 **`query`**（或你设置的输入变量名），即 `{{#开始.query#}}`，这样用户问题会传入 LLM。
3. 如有「温度」「Max Tokens」等，按需设置后保存。

**参数小结（LLM 节点）**

| 配置项 | 说明 |
|--------|------|
| 模型 | 选择已接入的模型 |
| 提示词中的变量 | 必须引用开始节点的 `query`（或你的输入变量名） |
| 温度 / Max Tokens | 按需求调整 |

### 步骤 4：配置「结束」节点（输出）

1. 拖入 **「结束」** 节点，放在 LLM 节点之后，并把 LLM 的输出连到「结束」。
2. 点击「结束」节点，在「输出」中新增一个变量，用于返回给前端：
   - **变量名**：必须与后端配置的 `DIFY_WORKFLOW_OUTPUT_VAR` 一致，默认建议 **`text`**。
   - **类型**：一般选「文本」。
   - **值/引用**：选择 LLM 节点的输出，例如「LLM 的回复内容」或 `{{#LLM.text#}}`（具体名称以你画布上的 LLM 节点名为准）。
3. 保存。

**参数小结（结束节点）**

| 配置项 | 建议值 | 说明 |
|--------|--------|------|
| 输出变量名 | `text` | 与后端 `.env` 中 `DIFY_WORKFLOW_OUTPUT_VAR` 一致 |
| 值 | 引用 LLM 输出 | 确保是 LLM 的文本回复 |

### 步骤 5：发布并获取 API Key

1. 点击右上角「发布」或「发布应用」，使工作流可被 API 调用。
2. 进入「API 访问」或「应用凭证」：
   - 复制 **API Key**，填入后端 `.env` 的 `DIFY_WORKFLOW_API_KEY`。
   - 若 Dify 自托管，确认 **API 基础 URL**（如 `https://api.dify.ai/v1` 或你的域名），与后端 `DIFY_API_URL` 一致。

---

## 三、后端环境变量配置（每步参数说明）

在项目 **`backend`** 目录下配置 `.env`（可参考 `.env.example`）。

### 1. 是否启用工作流

```env
DIFY_USE_WORKFLOW=true
```

- `true`：智能问答使用工作流接口 `POST /workflows/run`。
- `false`：使用原有对话型接口 `POST /chat-messages`。

### 2. 工作流 API 地址（通常与对话型共用）

```env
DIFY_API_URL=https://api.dify.ai/v1
```

- 若 Dify 自托管，改为你的地址，例如：`https://your-dify.com/v1`。
- 工作流与对话型共用同一 `DIFY_API_URL`，只是路径为 `/workflows/run`。

### 3. 工作流 API Key（必填）

```env
DIFY_WORKFLOW_API_KEY=app-xxxxxxxxxxxx
```

- 在 Dify 工作流应用的「API 访问」中复制。
- 仅当 `DIFY_USE_WORKFLOW=true` 时使用；未配置时不会走工作流。

### 4. 工作流输入变量名

```env
DIFY_WORKFLOW_INPUT_VAR=query
```

- 必须与 Dify 工作流「开始」节点里你配置的**输入变量名**一致（如 `query`）。
- 后端会把用户消息以 `inputs[DIFY_WORKFLOW_INPUT_VAR]` 传给工作流。

### 5. 工作流输出变量名

```env
DIFY_WORKFLOW_OUTPUT_VAR=text
```

- 必须与 Dify 工作流「结束」节点里你配置的**输出变量名**一致（如 `text`）。
- 后端从 `data.outputs[DIFY_WORKFLOW_OUTPUT_VAR]` 取回复文本返回给前端。

### 完整示例（.env 片段）

```env
DIFY_API_URL=https://api.dify.ai/v1
DIFY_USE_WORKFLOW=true
DIFY_WORKFLOW_API_KEY=app-xxxxxxxxxxxxxxxxxxxxxxxx
DIFY_WORKFLOW_INPUT_VAR=query
DIFY_WORKFLOW_OUTPUT_VAR=text
```

---

## 四、请求与响应对应关系

- **前端**：用户发送一条消息 → `POST /api/chat`，body 含 `message`、`history` 等。
- **后端**：若启用工作流，则构造：
  - `inputs = { DIFY_WORKFLOW_INPUT_VAR: message }`
  - `user = userId 或 "default"`
  - `response_mode = "blocking"`
- **Dify**：执行工作流后返回 `data.outputs`，后端从中取 `DIFY_WORKFLOW_OUTPUT_VAR` 的值作为 `reply` 返回前端。

工作流为无状态，每次请求独立执行，不依赖 `conversation_id`；多轮对话如需上下文，可在工作流内自行用「历史」节点或在下游扩展。

---

## 五、可选：获取工作流参数接口（核对变量名）

若不确定开始/结束节点的变量名，可调用 Dify「获取应用参数」接口查看：

- 文档：<https://docs.dify.ai/api-reference/应用配置-workflow/获取应用参数-workflow>
- 请求：`GET /parameters`，Header：`Authorization: Bearer {DIFY_WORKFLOW_API_KEY}`。
- 返回中会包含工作流所需的输入（inputs）与输出（outputs）结构，便于核对 `DIFY_WORKFLOW_INPUT_VAR` 与 `DIFY_WORKFLOW_OUTPUT_VAR`。

---

## 六、检查清单

| 步骤 | 检查项 |
|------|--------|
| Dify 工作流 | 开始节点有输入变量（如 `query`），结束节点有输出变量（如 `text`），且 LLM 引用输入、结束引用 LLM 输出 |
| Dify 工作流 | 已发布应用，并复制了该工作流的 API Key |
| 后端 .env | `DIFY_USE_WORKFLOW=true`，`DIFY_WORKFLOW_API_KEY` 已填 |
| 后端 .env | `DIFY_WORKFLOW_INPUT_VAR`、`DIFY_WORKFLOW_OUTPUT_VAR` 与工作流内变量名一致 |
| 运行 | 重启后端服务，在前端智能问答中发一条消息，应收到工作流返回的回复 |

按上述步骤配置后，智能问答模块即可稳定接入「输入 → LLM → 输出」的 Dify 工作流。
