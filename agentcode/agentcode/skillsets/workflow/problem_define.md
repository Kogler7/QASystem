# 阶段一：问题定义（模板）

- **输入**：由路由传入的 JSON 字符串（例如含 `question`）；字段名由业务约定。
- **输出**：符合 delivery schema 的 JSON，顶层键 **`problem_model`**（当前为占位结构，内含 `summary`）。
- **说明**：替换本文件与 `workflow_pipeline.problem_model_json_schema()` 即可对接真实问题模型。
