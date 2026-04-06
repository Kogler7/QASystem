# 统一 LLM 调用接口

供 NL_to_cypher、ans、rag、uie 等组件复用的大模型调用层。

## 默认行为

**默认使用本地 vLLM**：`LLM_BASE_URL=http://localhost:9999`，走 OpenAI 兼容的 chat 接口，无需 API Key。若 vLLM 只加载了一个模型，可不设 `LLM_MODEL`（会发 `default`）。

## 环境变量（任选其一，统一优先）

| 变量 | 说明 | 默认 |
|------|------|------|
| `LLM_BACKEND` | 后端：`ollama` / `openai` / `zhipu` | `openai` |
| `LLM_BASE_URL` / `VLLM_BASE_URL` | 请求地址 | `http://localhost:9999` |
| `LLM_MODEL` / `VLLM_MODEL` | 模型名（vLLM 加载的模型） | 空则发 `default` |
| `LLM_API_KEY` | API Key（本地 vLLM 可不设） | 空 |

## 在代码中调用

**方式一：函数**

```python
import sys
from pathlib import Path
# 若当前工作目录不是 algorithm，需把 algorithm 加入 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 或项目根

from common.llm_client import complete, chat

# 单轮
text = complete("你好", timeout=120)

# 单轮 + 系统提示 + 参数（仅 openai/zhipu 生效）
text = complete("总结下文", system_prompt="你是助手", temperature=0.7, max_tokens=2048)

# 多轮（OpenAI messages 格式）
text = chat([
    {"role": "system", "content": "你是助手"},
    {"role": "user", "content": "第一轮"},
    {"role": "assistant", "content": "回复"},
    {"role": "user", "content": "第二轮"},
])
```

**方式二：LLMClient 实例（固定配置）**

```python
from common.llm_client import LLMClient

client = LLMClient(
    backend="openai",
    base_url="https://api.xxx.com",
    api_key="sk-xxx",
    model="Qwen/Qwen3-8B",
)
reply = client.complete("你好")
reply = client.chat([{"role": "user", "content": "你好"}])
```

**方式三：run(system_prompt, text) 适配器（供 UIE/知识抽取等）**

需要「系统提示 + 用户文本」、返回单次回复字符串的组件，可使用 `LLMRunAdapter` 或 `create_run_adapter`，与 UIE 的 `BaseLLM.run(system_prompt, text)` 接口一致：

```python
from common.llm_client import create_run_adapter, LLMClient, LLMRunAdapter

# 从环境变量创建
adapter = create_run_adapter()
text = adapter.run("你是抽取专家", "待抽取的原文...")

# 或自定义配置
client = LLMClient(base_url="http://localhost:9999", model="Qwen/Qwen3-8B")
adapter = LLMRunAdapter(client)
text = adapter.run("系统提示", "用户输入")
```

## 已接入的组件（统一接口）

| 组件 | 用法 |
|------|------|
| **NL_to_cypher** | `complete` 作为 `call_llm` |
| **rag** | `complete`、`extract_final_response` |
| **ans/QA** | `utils.run_llm` 内部委托 `complete`（backend 按 engine 推断 openai/zhipu） |
| **uie（知识抽取）** | `ApiLLM` 委托 `LLMClient.run(system_prompt, text)`；`LocalLLM` 仍为本地微调模型 |
| **intent_recognition_model** | 配置与 common 统一：从 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY` 等读取，LangChain ChatOpenAI 使用同一套环境变量 |
