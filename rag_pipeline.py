"""
rag_pipeline.py
───────────────
Склейка retriever + generator. Один вызов —> готовый текст для TTS.
"""
from dataclasses import dataclass, field

from retriever import Retriever
from generator import generate_burunov
import config


@dataclass
class RagResult:
    topic: str
    text: str                  # готовый текст для TTS (Бурунов стиль)
    sources: list[dict] = field(default_factory=list)  # анекдоты-источники
    fallback: bool = False     # True если ничего не нашли в базе


def tell_joke(topic: str, top_k: int = config.TOP_K) -> RagResult:
    """
    Главная точка входа. Юзер даёт тему → получаем текст + источники.
    """
    topic = (topic or "").strip()
    if not topic:
        return RagResult(
            topic=topic,
            text="Ну... ты хотя бы тему назови, что ли...",
            fallback=True,
        )

    # 1. RAG: достаём релевантные анекдоты
    hits = Retriever.search(topic, top_k=top_k)

    if not hits:
        return RagResult(
            topic=topic,
            text=(
                "Хм... В 86-м я такого не слышал... "
                "Может, попроще чего спросишь? "
                "Про Штирлица там, или про колбасу..."
            ),
            fallback=True,
        )

    # 2. LLM: пересказываем в стиле Бурунова
    text = generate_burunov(topic, hits)

    return RagResult(
        topic=topic,
        text=text,
        sources=hits,
        fallback=False,
    )


# ─── Тест из консоли ──────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    topic = " ".join(sys.argv[1:]) or "Штирлиц и Мюллер"
    result = tell_joke(topic)

    print(f"\nТЕМА: {result.topic}")
    print(f"FALLBACK: {result.fallback}")
    print(f"ИСТОЧНИКОВ: {len(result.sources)}")
    for s in result.sources:
        print(f"  [{s['score']}] {s['tags']} — {s['text'][:80]}...")
    print("\nТЕКСТ ДЛЯ TTS:")
    print("─" * 60)
    print(result.text)
    print("─" * 60)
