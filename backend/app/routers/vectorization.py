from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from algorithm.NL_to_cypher.vector import batch_text_to_vectors
from backend.app.responses import payload
from backend.app.schemas import BatchTextVectorRequest


router = APIRouter(tags=["vectorization"])


@router.post("/api/component/nl2cypher/text-to-vectors")
async def text_to_vectors_api(body: BatchTextVectorRequest) -> dict:
    vectors = await run_in_threadpool(batch_text_to_vectors, body.texts)
    return payload(
        {
            "count": len(vectors),
            "texts": body.texts,
            "vectors": vectors,
            "items": [
                {"index": index, "text": text, "vector": vector}
                for index, (text, vector) in enumerate(zip(body.texts, vectors))
            ],
        },
        f"批量文本向量化完成，共 {len(vectors)} 个向量",
    )
