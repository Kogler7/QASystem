# 轨道交通知识服务系统 - 后端（FastAPI）
FROM python:3.12-slim

WORKDIR /app

# 系统依赖（如需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码
COPY app ./app

# 文档上传目录（运行时由卷挂载）
RUN mkdir -p /app/data/documents

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
