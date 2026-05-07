# 阶段二：方案规划 → 方案模型

## 输入

JSON，含 **`problem_model`**（问题定义阶段产出，可经人工编辑）。

## 输出

合法 JSON，顶层键 **`solution_model`**，至少包含：

- **`steps`**：数组；每项与组件匹配单请求体中的 **`step`** 同形：
  - `objective`（string）
  - `inputs`（string 数组）
  - `outputs`（string 数组）
  - `constraints`（string 数组）

可选 **`summary`**：对方案的简短概括。

编排层应对 `solution_model.steps` **逐元素**调用 `POST .../match`，每次 body 为 `{ "step": <该元素> }`。
