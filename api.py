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
from fastapi.responses import Response
from pydantic import BaseModel, Field

from rag_pipeline import tell_joke
from retriever import Retriever
import preset_audio
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


# ─── Готовые wav-пресеты голосом Бурунова (XTTS v2, offline-синтезированные) ──
#
# RAG выше умеет сгенерить текст анекдота на ЛЮБУЮ тему из 27k базы, но
# озвучить его голосом Бурунова может только для фраз из этого фиксированного
# набора — XTTS слишком тяжёлый для live-синтеза на борту G1 (см. preset_audio.py).

@app.get("/presets")
def list_presets():
    """Список доступных пресетов (имя, текст, длительность)."""
    return {
        "presets": [
            {"name": p.name, "text": p.text, "duration_s": p.duration_s, "topic": p.topic}
            for p in preset_audio.list_presets()
        ],
        # topics: тема -> список пресетов с готовым звуком (несколько = реальная
        # вариативность, см. preset_audio.get_random_preset_for_topic).
        "topics": preset_audio.topics_available(),
        "joke_topics": list(preset_audio.JOKE_TOPIC_PRESETS.keys()),
        "coffee_lines": preset_audio.COFFEE_PRESETS,
    }


@app.get("/presets/{name}/audio")
def preset_audio_pcm(name: str):
    """PCM голосом Бурунова для пресета (16kHz/mono/16-bit, без заголовка —
    тот же контракт, что и /synthesize_pcm в edge_tts_server.py)."""
    try:
        pcm = preset_audio.get_preset_pcm(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Нет пресета '{name}'")
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=500, detail=str(e))
    return Response(content=pcm, media_type="application/octet-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
