# Intent Recognition MCP — 工具清单与实现说明

## 一、服务概览

| 属性 | 值 |
|------|-----|
| 服务名 | Intent Recognition MCP |
| 版本 | 1.0.0 |
| 框架 | FastMCP |
| 传输协议 | HTTP（JSON-RPC） |
| 默认端口 | `8891` |
| 源码路径 | `algorithm/intent_recognition_model/mcp/intent/mcp_server.py` |
| 执行器路径 | `algorithm/intent_recognition_model/mcp/intent/executor.py` |
| 核心依赖 | `algorithm.intent_recognition_model.intent_recognition.agent_intent.intent_recognition` |

**启动方式**：

```
python -m algorithm.intent_recognition_model.mcp.intent.mcp_server
# → 启动 HTTP 服务在 0.0.0.0:8891
```

---

## 二、工具清单

### 2.1 `recognize_intent` — 单条意图识别

**功能**：根据用户输入的问题模型，识别对应的业务意图（领域、场景、目的等）。

**参数**（`IntentRecognitionRequest`）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `problem_model` | `str` 或 `dict` | ✅ | 问题模型。字符串示例：`"最近12个月的采购金额月度走势是怎样的？"`；字典示例：`{"problem_id": "QKB-001", "description": "..."}` |

**返回** `Dict[str, Any]`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `understanding` | `str` | 意图理解的文字描述，如 `"最终意图识别成功"` |
| `pass` | `bool` | 识别是否成功 |
| `intent` | `dict` | 意图详情，包含：`intent_id`、`domain`（业务域）、`scenario`（场景）、`object`（分析对象）、`intent_name`（意图名称）、`user_goal`（用户目标） |
| `detail` | `dict` | 补充细节 |
| `execution_time_ms` | `float` | 执行耗时（毫秒） |

**返回示例**：

```json
{
  "understanding": "最终意图识别成功",
  "pass": true,
  "intent": {
    "intent_id": "INT20260211001",
    "domain": "采购管理域",
    "scenario": "采购结构分析",
    "object": "采购金额结构",
    "intent_name": "看构成",
    "user_goal": "分析公开与非公开采购金额构成"
  },
  "detail": {},
  "execution_time_ms": 8636.75
}
```

**注解**：`readOnlyHint: true, idempotentHint: true, destructiveHint: false`（只读、幂等、无副作用）

---

### 2.2 `batch_recognize_intent` — 批量意图识别

**功能**：一次请求识别多个问题模型的意图，支持遇错即停模式。

**参数**（`BatchIntentRecognitionRequest`）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `requests` | `List[IntentRecognitionRequest]` | ✅ | 批量识别请求列表 |
| `stop_on_failure` | `bool` | 否 | 默认 `False`。为 `True` 时遇到失败立即停止后续请求 |

**返回** `Dict[str, Any]`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `results` | `list[dict]` | 每条意图识别结果的列表 |
| `all_successful` | `bool` | 是否所有请求都成功 |
| `total_time_ms` | `float` | 总耗时（毫秒） |

**注解**：同 2.1（只读、幂等、无副作用）

---

## 三、实现说明

### 3.1 整体架构

```
MCP Client (AgentRuntime)
    │ JSON-RPC over HTTP
    ▼
FastMCP HTTP Server (mcp_server.py)
    │ @app.tool 装饰器注册
    ├─ recognize_intent()
    └─ batch_recognize_intent()
    │
    ▼
IntentRecognitionExecutor (executor.py)
    │ 封装调用逻辑
    ├─ 计时
    ├─ 异常兜底
    │
    ▼
intent_recognition() (agent_intent.py)
    │ 核心意图识别逻辑
    └─ 返回 understanding / pass / intent / detail
```

### 3.2 核心类：`IntentRecognitionExecutor`

```python
class IntentRecognitionExecutor:
    def __init__(self):
        if intent_recognition is None:
            raise ImportError("无法加载 intent_recognition 模块")

    def recognize(self, problem_model: Union[str, dict]) -> dict:
        start_time = time.monotonic()
        result = intent_recognition(problem_model)   # 调用核心引擎
        result["execution_time_ms"] = ...            # 附加耗时
        return result
```

**设计要点**：

1. **薄封装**：执行器不关心意图识别内部逻辑，只做调用封装、计时、异常兜底。
2. **外部导入**：`intent_recognition` 函数在 `__init__.py` 中通过 sys.path 动态导入，路径从 `executor.py` 向上两级目录（到 `intent_recognition_model/`）定位。
3. **灵活输入**：支持字符串（自然语言问题）和字典（结构化问题模型）两种输入格式。

### 3.3 错误处理策略

```python
# mcp_server.py 层 — MCP 工具入口的兜底
def recognize_intent(request):
    try:
        executor = IntentRecognitionExecutor()
        return executor.recognize(request.problem_model)
    except Exception:
        return {"pass": False, "intent": {}, "error_message": str(e), ...}

# executor.py 层 — 核心引擎的异常捕获
def recognize(self, problem_model):
    try:
        result = intent_recognition(problem_model)
        ...
    except Exception:
        return {"pass": False, "intent": {}, "error_message": str(e), ...}
```

**两层保护**：如果 `intent_recognition` 内部出错，executor 层兜底并返回结构化错误；如果 executor 初始化失败，mcp_server 层再兜底。确保任何情况下调用方都拿到的都是合法 JSON。

### 3.4 模块导入机制

```python
# executor/__init__.py L10-25
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "..", ".."))
sys.path.insert(0, project_root)

from algorithm.intent_recognition_model.intent_recognition.agent_intent import intent_recognition
```

路径解析链：`mcp/intent/executor.py` → `..` = `mcp/intent/` → `..` = `mcp/` → `..` = `intent_recognition_model/`（即 `project_root`），再拼接 `algorithm/intent_recognition_model/intent_recognition/agent_intent.py`。

有 `try/except ImportError` 回退逻辑，允许模块层级不同的部署环境也能加载。

---

## 四、数据流示意

```
用户输入: "公开采购和非公开采购的金额构成是什么"
    │
    ▼
recognize_intent(problem_model="公开采购...")
    │
    ▼
intent_recognition("公开采购...")
    │
    ▼
{
  "understanding": "最终意图识别成功",
  "pass": true,
  "intent": {
    "intent_id": "INT20260211001",
    "domain": "采购管理域",
    "scenario": "采购结构分析",
    "object": "采购金额结构",
    "intent_name": "看构成",
    "user_goal": "分析公开与非公开采购金额构成"
  },
  "execution_time_ms": 8636.75
}
```
