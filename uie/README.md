## UIE
 
将自然语言文本（如规则说明、系统说明、术语表等）抽取为结构化 JSON，并可按需写入 Neo4j 图数据库，形成可查询的知识图谱。  

## 说明
需开启Neo4j服务： `bolt://localhost:7687`，用户名 `neo4j`，密码 `neo4j2025`。
微调后模型路径：/mnt/data3/srr/saves/rule_v4
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
        - 先用正则模板在原文中做句式匹配，构造提示信息；
        - 调用 `LLMExtractor` 抽取结构化结果，并在顶层对象注入置信度字段；
        - 返回值为 `{"raw": ..., "graph": ...}`，只做抽取与图结构构建，不写入 Neo4j，适合作为上层系统/服务的主要调用入口。


## 运行与调用（简要说明）

- **启动 HTTP 服务**
  - 在 `uie` 目录下执行（需要已安装 `fastapi`、`uvicorn` 及相关依赖）：
    ```bash
    uvicorn fastapi_app:app --host 0.0.0.0 --port 8001 --reload
    ```

- **调用抽取接口示例**
  - POST `http://localhost:8001/extract`
  - 示例请求体：
    ```json
    {
      "main_object": "RuleType",
      "text": "这里是一段包含检修规则或系统描述的原始文本……",
      "use_local": true,
      "use_templates": true,
      "store_to_neo4j": false
    }
    ```
  - 返回值中：
    - `data.raw`：模型抽取出的原始 JSON 结构；
    - `data.graph.nodes` / `data.graph.relationships`：按规则拆解的图结构，可直接用于前端可视化或后续入库。

*** End of File
