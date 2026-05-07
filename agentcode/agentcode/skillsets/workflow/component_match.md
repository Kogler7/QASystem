# 阶段三：组件匹配（单步）

## 输入

JSON，顶层键 **`step`**，结构如下（与方案模型中每一步一致）：

- `objective`：string  
- `inputs`：string 数组  
- `outputs`：string 数组  
- `constraints`：string 数组  

## 输出

合法 JSON，顶层键 **`component_match`**（含 `summary` 与 `matches` 数组）；具体匹配字段由业务 schema 替换。
