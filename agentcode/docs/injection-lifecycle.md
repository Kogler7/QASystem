# Skill、MCP、Profile、Config 的注入时序与机制

本文梳理从进程启动到单次会话结束，**宿主环境配置**、**Profile**、**SessionConfig**、**Skill**、**MCP** 各自在何时、以何种方式进入系统。实现以本仓库 `agentcode` 与 `backend/app` 中的当前代码为准。

---

## 术语约定

| 名称 | 含义 |
|------|------|
| **宿主 config** | `backend/app/config.py`：`.env` 加载、`Settings`（Pydantic）、以及为算法兼容而写的 `os.environ` 别名。 |
| **Profile** | 如 `agentcode/profiles/time_agent.py` 中的 `build_beijing_time_config()`：用 Python **拼出**一次会话用的 `SessionConfig`，本身不是独立进程。 |
| **SessionConfig** | `agentcode/models.py`：单次智能体会话的数据对象（`mcp_servers`、`skills`、`tool_policy`、`budget`、`delivery` 等）。 |
| **Skill** | `skillsets/` 下的 Markdown，通过 `SkillRef` 绑定；由 `SkillLoader` 读入并写入**消息**的 system/developer/task 层。 |
| **MCP** | 独立 HTTP 服务（或将来 stdio）；`McpClient` 通过 JSON-RPC 调用 `tools/list`、`tools/call`。 |

---

## 阶段 A：进程启动 / 首次 import（环境层）

**触发**：例如 `backend/app/db.py` 执行 `from .config import settings`。

1. **`load_dotenv`**  
   按 `config.py` 中约定加载根目录或 `backend/.env`，写入 **`os.environ`**。

2. **`_set_algorithm_compat_env()`**  
   将统一 LLM / Neo4j 变量名同步到历史环境变量名（如 `OPENAI_BASE_URL`、`LLM_TYPE` 等），供仍使用 `os.getenv` 的子模块使用。

3. **`Settings()`（Pydantic）**  
   从 **`os.environ`** 读取应用级字段（如 Dify、JWT、`DATABASE_URL`）；`get_settings()` 带 `@lru_cache`。

**此阶段尚未涉及**：Skill 正文、MCP 网络请求、`SessionConfig` 构造。仅准备**进程级环境**。

---

## 阶段 B：HTTP 请求进入 agent 入口（组装 SessionConfig）

**触发**：例如 `POST /api/agent-template/raw`，进入 `agentcode/integrations/fastapi/agent_template.py` 的 `_run_agent_core(query)`。

1. **读取宿主环境**  
   使用 `os.getenv("AGENT_MCP_TIME_URL")`、`MCP_BEIJING_TIME_URL`、`LLM_*` 等（值来自阶段 A 已加载的 `.env` / 系统环境）。

2. **Profile 执行（若有 MCP URL）**  
   调用 `build_beijing_time_config(...)`（`profiles/time_agent.py`），在内存中构造 **`SessionConfig`**：  
   `mcp_servers`、`skills`（仅 **`SkillRef`**：id、path、`inject_layer`，**不读文件**）、`tool_policy`、`budget`、`delivery` 等。

3. **无 MCP URL 时的分支**  
   在同文件中手写另一套 **`SessionConfig`**（演示路径），同样只是 Python 对象。

4. **`McpClient(config)` 构造**  
   遍历 `config.mcp_servers`，为每个 HTTP MCP 创建 `HttpToolAdapter(url)`，登记到客户端。  
   **此时仍不调用** `tools/list`（无网络拉表）。

5. **`AgentRuntime(..., mcp_client=..., ...)`**  
   保存 `llm_backend`、`mcp_client`、`trace_sink` 等，等待 `run_session`。

---

## 阶段 C：`run_session` 内部（agentcode `runtime.py`）

### C1 — Skill 注入（首轮消息）

**位置**：`_build_initial_messages(config, user_message)`。

1. **`skill_loader.resolve(config.skills)`**  
   对每个 `SkillRef.path` **打开 Markdown 文件**，按 `inject_layer` 归入 `system` / `developer` / `task`。

2. **拼装 `messages`**  
   固定 system 前缀 + 各层 skill 文本 + **user** 的 `user_message`。  
   Skill 以 **文本块** 形式进入对话上下文，后续每轮 LLM 调用均可见（除非实现上截断，当前未截断）。

### C2 — MCP 工具表注入（进入主循环后）

**位置**：`_run_loop` 起始处，`tools = self.mcp_client.list_tools()`。

1. 对每个已注册的 HTTP MCP 发 **`tools/list`**（JSON-RPC POST）。

2. 将返回的工具合并为 **`{server_name}__{tool_local_name}`**，供本轮 **`LLMBackend.complete(messages, tools)`** 使用（FastAPI 示例中再映射为 OpenAI `tools` 格式）。

### C3 — 主循环（工具执行与 delivery）

1. 模型返回 `tool_use` 时，runtime 按 **`tool_policy`** 校验后调用 **`McpClient.call_tool`** → **`tools/call`**。

2. 无工具调用且需终稿时，按 **`delivery.json_schema`** 校验模型输出；失败则注入 developer 提示并重试 LLM（可能增加 `spent.turns`）。

---

## 对照总表

| 概念 | 典型代码位置 | 注入时机 | 注入机制 |
|------|----------------|----------|----------|
| 宿主 config | `backend/app/config.py` | import `app.config` 时 | `load_dotenv`、`BaseSettings` → `os.environ` / `settings` |
| Profile | `agentcode/profiles/*.py` | 每个请求内调用 `build_*_config` | 返回 **`SessionConfig`** |
| SessionConfig | 同上 + `models.py` | Profile 或手写构造瞬间 | Python 对象，传入 `McpClient`、`run_session` |
| Skill | `skillsets/**/*.md` | `run_session` → `_build_initial_messages` | 读盘 → 写入 **messages** 的 system/developer/task |
| MCP 工具定义 | 各 `mcp_servers/*/server.py` 等 | `_run_loop` 首次 `list_tools` | HTTP **tools/list** |
| MCP 工具执行 | 同上 | 模型产生 `tool_use` 且通过白名单后 | HTTP **tools/call** |

---

## 与 `backend` 集成的注意点

- **必须先加载宿主 `config`**（当前由 `db.py` 在 import 链上较早触发），`.env` 才会进入 `os.environ`，agent 侧 `os.getenv` 才能命中。
- **`Settings` 中的字段**与 **LLM / MCP URL** 不必一一对应：`agent_template` 的 LLM 与 MCP 仍主要读 **`os.getenv`**；若希望统一进 Pydantic，需在 `Settings` 中增加字段并在路由或 Profile 中读取后传入。

---

## 一句话串起来

**import 时宿主 `config` 灌环境 → 请求到达时用 Profile（或手写）得到 `SessionConfig` → `McpClient` 只登记 MCP URL → `run_session` 里先读 Skill 进消息、再向 MCP 拉 `tools/list` 交给 LLM → 循环内按需 `tools/call` 并校验 `delivery`。**

扩展新能力时，优先新增 **`mcp_servers/<domain>/`**、**`skillsets/<scene>/`**、**`profiles/<name>.py`**，避免在 `runtime.py` 中写死业务分支（与 `agentcode/README.md` 设计边界一致）。
