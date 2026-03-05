from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any
import uvicorn
from QA.answer_solve import Neo4jQAService
from contextlib import asynccontextmanager
from neo4j import GraphDatabase

# 先声明全局变量，但不初始化
qa_service = None



# 定义请求和响应的数据模型
class QuestionRequest(BaseModel):
    question: str
    detailed: Optional[bool] = False


class QuestionResponse(BaseModel):
    success: bool
    question: str
    answer: str
    timestamp: str
    details: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    service: str
    neo4j_connected: bool


class BatchQuestionRequest(BaseModel):
    questions: list[str]
    detailed: Optional[bool] = False

class VisualizedQuestionResponse(QuestionResponse):
    """带可视化的响应"""
    visualization: Optional[str] = None  # HTML文件路径或Base64编码的图像
    visualization_type: Optional[str] = "html"  # html, image, json

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时执行
    global qa_service
    print("正在初始化问答服务...")

    try:
        qa_service = Neo4jQAService()
        print("问答服务初始化完成")
    except Exception as e:
        print(f"问答服务初始化失败: {e}")

    yield  # 服务运行在这里

    # 关闭时执行
    if qa_service:
        try:
            qa_service.close()
            print("服务关闭，Neo4j连接已释放")
        except Exception as e:
            print(f"关闭服务时出错: {e}")


# 创建应用时指定 lifespan
app = FastAPI(
    title="Neo4j 知识图谱问答 API",
    description="通过 Postman 测试的问答服务接口",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """检查服务是否正常运行"""
    global qa_service

    # 如果服务都没初始化，直接返回 False
    if qa_service is None:
        return HealthResponse(
            status="degraded",
            service="neo4j-qa-service",
            neo4j_connected=False
        )

    neo4j_status = False

    try:
        driver = GraphDatabase.driver(
            "bolt://10.126.62.88:7687",
            auth=("neo4j", "neo4j2025")
        )
        with driver.session() as session:
            session.run("RETURN 1").consume()
        driver.close()
        neo4j_status = True
        print("健康检查：Neo4j 连接正常")
    except Exception as e:
        print(f"健康检查：Neo4j 连接失败 - {e}")

    return HealthResponse(
        status="healthy" if neo4j_status else "degraded",
        service="neo4j-qa-service",
        neo4j_connected=neo4j_status
    )


# API 端点：回答问题
@app.post("/ask", response_model=QuestionResponse)
async def ask_question(request: QuestionRequest):
    """
    向知识图谱提问
    """
    global qa_service

    if qa_service is None:
        raise HTTPException(status_code=503, detail="服务未初始化")

    try:
        if request.detailed:
            result = qa_service.get_solver_detail(request.question)
            # 提取答案
            answer = "无答案"
            try:
                if result.get("solver_result"):
                    方案模型列表 = result["solver_result"].get("方案模型", [])
                    if 方案模型列表:
                        outputs = 方案模型列表[0].get("输出", [])
                        if outputs:
                            answer = outputs[0]
            except:
                pass

            return QuestionResponse(
                success=result.get("success", False),
                question=request.question,
                answer=answer,
                timestamp=result.get("timestamp", ""),
                details=result.get("solver_result") if result.get("success") else None,
                error=result.get("error")
            )
        else:
            result = qa_service.answer_question(request.question)
            return QuestionResponse(
                success=result.get("success", False),
                question=request.question,
                answer=result.get("answer", "无答案"),
                timestamp=result.get("timestamp", ""),
                error=result.get("error")
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ask/visualize", response_model=VisualizedQuestionResponse)
async def ask_question_with_visualization(request: QuestionRequest):
    """
    回答问题并生成可视化
    """
    global qa_service

    if qa_service is None:
        raise HTTPException(status_code=503, detail="服务未初始化")

    try:
        result = qa_service.answer_question_with_visualization(
            request.question,
            visualize=True
        )

        return VisualizedQuestionResponse(
            success=result.get("success", False),
            question=request.question,
            answer=result.get("answer", "无答案"),
            visualization=result.get("visualization"),
            visualization_type="html",
            timestamp=result.get("timestamp", ""),
            error=result.get("error")
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/visualize/path/{question_id}")
async def get_path_visualization(question_id: str):
    """
    获取已生成的可视化文件
    """
    import os
    from fastapi.responses import HTMLResponse

    # 查找对应的HTML文件
    html_files = [f for f in os.listdir('.') if f.startswith(f"answer_path_{question_id}") and f.endswith('.html')]

    if html_files:
        with open(html_files[0], 'r', encoding='utf-8') as f:
            content = f.read()
        return HTMLResponse(content=content)

    raise HTTPException(status_code=404, detail="可视化文件不存在")


# 启动服务
if __name__ == '__main__':
    uvicorn.run(
        "api_service:app",
        host='0.0.0.0',
        port=5000,
        reload=True
    )