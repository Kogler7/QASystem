# 统一 LLM 调用接口

供 NL_to_cypher、ans、rag、uie 等组件复用的大模型调用层。

## 默认行为

**默认使用本地 vLLM**：`LLM_BASE_URL=http://localhost:8888`，走 OpenAI 兼容的 chat 接口，无需 API Key。若 vLLM 只加载了一个模型，可不设 `LLM_MODEL`（会发 `default`）。

## 环境变量（任选其一，统一优先）

| 变量 | 说明 | 默认 |
|------|------|------|
| `LLM_BACKEND` | 后端：`ollama` / `openai` / `zhipu` | `openai` |
| `LLM_BASE_URL` / `VLLM_BASE_URL` | 请求地址 | `http://localhost:8888` |
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

## 已接入的组件

- **NL_to_cypher**：`llm_client.call_llm` 委托给本模块的 `complete`
- **rag**：通过 `llm_client` 使用 `call_llm`、`extract_final_response`

其他组件（如 ans/QA、uie）可将原有 `run_llm` / OpenAI 调用逐步替换为上述 `complete` / `chat` 或 `LLMClient`。
