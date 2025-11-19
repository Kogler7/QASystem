## **uv 快速上手教程**  

### 1. 安装 uv  
推荐使用官方独立安装方式（无需提前安装 Rust 或 Python）：  

**macOS / Linux**  
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows（PowerShell）**  
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

或者用 **pip** 安装：  
```bash
pip install uv
```

---

### 2. 创建新项目
```bash
uv init my_project
cd my_project
```

---

### 3. 添加依赖
```bash
uv add requests
```
会自动创建虚拟环境 `.venv` 并安装依赖。  

---

### 4. 运行代码
可以直接在虚拟环境中运行：  
```bash
uv run python app.py
```

如果是单文件脚本，也可以直接运行：  
```bash
uv run script.py
```

---

### 5. 管理 Python 版本
安装并切换 Python 版本：  
```bash
uv python install 3.11  # 安装 Python 3.11
uv python pin 3.11      # 当前项目固定使用 3.11
```

---

### 6. 使用锁文件保证依赖一致性
生成锁文件：  
```bash
uv lock
```
同步依赖（按锁文件安装）：  
```bash
uv sync
```

---

### 7. 作为 pip 的替代
安装依赖：  
```bash
uv pip install flask
```
同步到 `requirements.txt`：  
```bash
uv pip compile requirements.in --universal --output-file requirements.txt
```

---

### 8. 快速运行临时工具（类似 pipx）
例如执行 `ruff` 检查代码：  
```bash
uvx ruff check .
```
安装工具到本地：  
```bash
uv tool install ruff
```

---

### 9. 更新 uv
```bash
uv self update
```

---

## **总结：团队日常核心命令**
| 场景               | 命令示例 |
|--------------------|----------|
| 创建项目           | `uv init my_project` |
| 添加依赖           | `uv add requests` |
| 运行代码           | `uv run app.py` |
| 安装 Python        | `uv python install 3.11` |
| 固定版本           | `uv python pin 3.11` |
| 锁定依赖           | `uv lock` |
| 同步依赖           | `uv sync` |
| 临时运行工具       | `uvx tool_name args` |
| 安装工具           | `uv tool install tool_name` |
