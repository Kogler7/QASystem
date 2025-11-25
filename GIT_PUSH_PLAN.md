# Git 推送计划文档

## 任务目标
将 `qasystem-wzc-dev` 项目推送到远程仓库的 `wzc-dev` 分支

## 远程仓库信息
- **远程仓库地址**: http://10.126.62.88/qasystem/qasystem
- **目标分支**: wzc-dev
- **提交信息**: "Initial commit: qasystem-wzc-dev project"

## 执行步骤

### 1. 初始化 Git 仓库
```bash
cd /mnt/data3/wzc/qasystem-wzc-dev
git init
```

**说明**: 在项目目录中初始化一个新的 Git 仓库

---

### 2. 配置远程仓库
```bash
cd /mnt/data3/wzc/qasystem-wzc-dev
git remote add origin http://10.126.62.88/qasystem/qasystem.git
```

**说明**: 添加远程仓库地址,命名为 origin

---

### 3. 添加所有文件到暂存区
```bash
cd /mnt/data3/wzc/qasystem-wzc-dev
git add .
```

**说明**: 
- 将所有文件添加到 Git 暂存区
- `.gitignore` 文件会自动排除不需要跟踪的文件(如 `__pycache__/`, `*.pyc`, 虚拟环境等)

---

### 4. 提交更改
```bash
cd /mnt/data3/wzc/qasystem-wzc-dev
git commit -m "Initial commit: qasystem-wzc-dev project"
```

**说明**: 创建初始提交,包含所有添加的文件

---

### 5. 切换到 wzc-dev 分支
```bash
cd /mnt/data3/wzc/qasystem-wzc-dev
git checkout -b wzc-dev
```

**说明**: 创建并切换到 `wzc-dev` 分支

---

### 6. 推送到远程仓库
```bash
cd /mnt/data3/wzc/qasystem-wzc-dev
git push -u origin wzc-dev
```

**说明**: 
- 将 `wzc-dev` 分支推送到远程仓库
- `-u` 参数设置上游跟踪分支,后续可以直接使用 `git push`

---

## 注意事项

### 1. 认证要求
如果远程仓库需要认证,推送时可能会提示输入用户名和密码:
- **用户名**: [需要您的 GitLab/Git 用户名]
- **密码**: [需要您的密码或访问令牌]

### 2. .gitignore 已配置排除项
根据项目的 `.gitignore` 文件,以下内容不会被提交:
- Python 缓存文件 (`__pycache__/`, `*.pyc`)
- 虚拟环境目录 (`venv/`, `env/`)
- 数据文件 (`*.csv`, `*.json`, `*.xlsx` 等)
- 模型文件 (`*.pkl`, `*.pt`, `*.pth`, `*.h5`)
- 日志文件 (`*.log`)
- IDE 配置 (`.vscode/`, `.idea/`)

### 3. 可能需要处理的情况

#### 情况A: 远程分支已存在
如果 `wzc-dev` 分支已经存在于远程仓库,可能需要先拉取:
```bash
git pull origin wzc-dev --allow-unrelated-histories
```

#### 情况B: 需要强制推送
如果遇到冲突且确认要覆盖远程分支:
```bash
git push -f origin wzc-dev
```
⚠️ **警告**: 强制推送会覆盖远程仓库的历史记录,请谨慎使用!

#### 情况C: SSL 证书问题
如果遇到 SSL 证书验证问题:
```bash
git config --global http.sslVerify false
```

---

## 项目文件结构
```
qasystem-wzc-dev/
├── configs/          # 配置文件
├── dataset/          # 数据集
├── embeddings/       # 嵌入向量
├── intentgpt/        # IntentGPT 模块
├── models/           # 模型文件
├── prompts/          # 提示词模板
├── results_v3/       # 结果输出
├── scripts/          # 脚本文件
├── .env              # 环境变量
├── .gitignore        # Git 忽略配置
├── main.py           # 主程序
├── predict_intent.py # 意图预测
├── README.md         # 项目说明
└── requirements.txt  # 依赖包列表
```

---

## 执行流程图

```mermaid
graph TD
    A[开始] --> B[初始化 Git 仓库]
    B --> C[配置远程仓库地址]
    C --> D[添加所有文件到暂存区]
    D --> E[提交更改]
    E --> F[创建并切换到 wzc-dev 分支]
    F --> G[推送到远程仓库]
    G --> H{推送是否成功?}
    H -->|是| I[完成]
    H -->|否| J{需要认证?}
    J -->|是| K[输入用户名和密码]
    K --> G
    J -->|否| L{分支冲突?}
    L -->|是| M[处理冲突或强制推送]
    M --> G
    L -->|否| N[检查错误信息]
    N --> G
</mermaid>

---

## 验证步骤

推送完成后,可以通过以下方式验证:

1. **检查远程分支**
```bash
git branch -r
```
应该能看到 `origin/wzc-dev`

2. **访问 GitLab Web 界面**
访问: http://10.126.62.88/qasystem/qasystem/-/tree/wzc-dev
检查文件是否正确上传

3. **查看提交历史**
```bash
git log --oneline
```

---

## 后续操作建议

### 保持分支同步
```bash
# 拉取最新更改
git pull origin wzc-dev

# 推送新的提交
git add .
git commit -m "Your commit message"
git push origin wzc-dev
```

### 分支管理
```bash
# 查看所有分支
git branch -a

# 切换分支
git checkout branch-name

# 合并分支
git merge branch-name
```

---

## 总结

本计划涵盖了从初始化 Git 仓库到推送至远程分支的完整流程。所有命令都已经针对项目路径 `/mnt/data3/wzc/qasystem-wzc-dev` 进行了配置。

准备好后,可以切换到 **Code 模式** 执行这些命令。