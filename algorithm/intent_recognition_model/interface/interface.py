from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel,Field
import uvicorn

from init_database.init_database import DatabaseInit
from intent_recognition.intent_app import IntentRecognition


class TextInput(BaseModel):
    text: str = Field(..., min_length=1, max_length=10000, description="待处理的文本")

app = FastAPI(title="intent API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # 替换为你的前端地址
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def init_database():
    init = DatabaseInit()
    init.create_collection()
    init.insert_data()


@app.post("/intent_recognition")
def intent_interface(user_input: TextInput):
    """意图识别接口"""
    try:
        query = user_input.text
        results = IntentRecognition().generate_result(query)
        return {"code": 200, "message": "success", "data": results}
    except HTTPException as e:
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")

if __name__ == "__main__":
    uvicorn.run(app, host="localhost", port=8000)

