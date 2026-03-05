## UIE
 
将自然语言文本（如规则说明、系统说明、术语表等）抽取为结构化 JSON，形成可查询的知识图谱。- 抽取结果可直接写入 Neo4j 图库，或仅返回节点与边的图结构供后续使用。

## 说明
需开启Neo4j服务： `bolt://localhost:7687`，用户名 `neo4j`，密码 `neo4j2025`。
微调后模型路径：/mnt/data3/srr/saves/rule_v4 
(可在88服务器上运行/home/admin_user_2/shirongrong/uie/llm_train目录下的deploy_rule_v4.py部署该模型   需要使用环境train_env)
需要安装的包：
openai
neo4j
llamafactory 
fastapi       # 如果需要跑 fastapi_app.py
uvicorn       # 如果需要跑 fastapi_app.py

## 主要文件说明

- **主要文件说明**
  - `README.md`：本说明文档。
- **`uie/`**：统一信息抽取（Unified Information Extraction）子模块
  - `fastapi_app.py`：对外的 FastAPI 服务入口，提供 HTTP 抽取接口。
  - `ie/`：真正的“信息抽取引擎”实现
    - `extraction_api.py`：**抽取流程与对外 Python 函数入口**（面向内部调用）。其中推荐主接口为：
      - `extract_with_templates_only(main_object, text, backend=None, use_local=True, high_confidence=0.9, low_confidence=0.6)`：
        - 在对应 UML 本体上解析示例模板；
        - 先用正则模板在原 文中做句式匹配，构造提示信息；
        - 调用 `LLMExtractor` 抽取结构化结果，并在顶层对象注入置信度字段；
        - 返回值为 `{"raw": ..., "graph": ...}`，只做抽取与图结构构建，不写入 Neo4j，适合作为上层系统/服务的主要调用入口。

**使用示例**

在 `uie` 目录下运行，确保 `ie` 包可被导入。

- 使用本地微调模型（`use_local=True`，默认）：
```python
from ie.extraction_api import extract_with_templates_only

text = "如果轮对轮径小于772mm，那么更新车轮、更换轴箱轴承。车轮必须满足轮缘厚度要求。"
result = extract_with_templates_only(
    main_object="RuleType",
    text=text,
    use_local=True,
)
# result["raw"] 为抽取的 JSON 列表
# result["graph"]["nodes"] / result["graph"]["relationships"] / result["graph"]["ontology_relations"] 为图结构
```

- 使用外部 API（`use_local=False`，需事先启动 OpenAI 兼容服务）：
```python
from ie.extraction_api import extract_with_templates_only
from ie.utils import ApiLLM

# 可选：自定义 API 地址和模型
backend = ApiLLM(
    api_key="EMPTY",
    base_url="http://127.0.0.1:8000/v1",
    model="Qwen/Qwen3-8B",
)

text = "如果轮对轮径小于772mm，那么更新车轮、更换轴箱轴承。"
result = extract_with_templates_only(
    main_object="RuleType",
    text=text,
    backend=backend,
    use_local=False,
)
```
