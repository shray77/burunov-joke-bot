"""
api.py
──────
FastAPI-обёртка над RAG-пайплайном.

Запуск:
  uvicorn api:app --host 0.0.0.0 --port 8000 --reload

Эндпоинты:
  GET  /         — healthcheck
  GET  /search?q=...   — только retriever (для дебага)
  POST /tell     — главная точка: {topic} → {text, sources}
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from rag_pipeline import tell_joke
from retriever import Retriever
import config

app = FastAPI(
    title="Burunov Joke Bot — RAG API",
    description="Анекдоты 1986 в стиле Сергея Бурунова",
    version="0.1.0",
)


class TellRequest(BaseModel):
    topic: str = Field(..., examples=["Штирлиц и Мюллер"])
    top_k: int = Field(default=config.TOP_K, ge=1, le=20)


class TellResponse(BaseModel):
    topic: str
    text: str
    fallback: bool
    sources: list[dict]


@app.get("/")
def healthcheck():
    return {
        "status": "ok",
        "model": config.OLLAMA_MODEL,
        "embed_model": config.EMBED_MODEL,
        "collection": config.CHROMA_COLLECTION,
    }


@app.get("/search")
def search(q: str, top_k: int = 5):
    """Только retriever. Полезно для отладки и просмотра что в базе."""
    hits = Retriever.search(q, top_k=top_k)
    return {"query": q, "results": hits}


@app.post("/tell", response_model=TellResponse)
def tell(req: TellRequest):
    try:
        result = tell_joke(req.topic, top_k=req.top_k)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"RAG failed: {e}")

    return TellResponse(
        topic=result.topic,
        text=result.text,
        fallback=result.fallback,
        sources=result.sources,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
