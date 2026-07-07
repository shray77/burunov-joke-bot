"""
build_vector_db.py
──────────────────
Читает jokes_clean.jsonl, строит векторную базу ChromaDB.

Эмбеддинги: intfloat/multilingual-e5-small (лёгкая, ~120 МБ, RU ок).
Важно: e5 требует префиксов "query: " / "passage: " перед текстом.
"""
import json
import time

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

import config


def load_clean_jokes() -> list[dict]:
    if not config.CLEAN_JOKES_PATH.exists():
        raise FileNotFoundError(
            f"Сначала запусти prepare_jokes.py — нет файла {config.CLEAN_JOKES_PATH}"
        )
    jokes = []
    with config.CLEAN_JOKES_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                jokes.append(json.loads(line))
    return jokes


def build():
    jokes = load_clean_jokes()
    print(f"Загружено анекдотов: {len(jokes)}")
    if not jokes:
        raise RuntimeError("Нет анекдотов для индексации.")

    print(f"Загружаю эмбеддер {config.EMBED_MODEL} ...")
    # device='cpu' — для хакатона стабильнее. Если есть CUDA, поменяй на 'cuda'.
    model = SentenceTransformer(config.EMBED_MODEL, device="cpu")

    print("Считаю эмбеддинги (это разово, ~1-2 мин на 1000 анекдотов)...")
    t0 = time.time()
    texts = [config.EMBED_PASSAGE_PREFIX + j["embed_text"] for j in jokes]
    embeddings = model.encode(
        texts,
        batch_size=32,
        show_progress_bar=True,
        normalize_embeddings=True,  # e5 хочет нормализованные
    )
    print(f"Готово за {time.time() - t0:.1f} сек")

    print(f"Создаю ChromaDB в {config.CHROMA_DIR} ...")
    client = chromadb.PersistentClient(
        path=str(config.CHROMA_DIR),
        settings=Settings(anonymized_telemetry=False),
    )
    # Пересоздаём коллекцию (на случай если запускаем повторно)
    try:
        client.delete_collection(config.CHROMA_COLLECTION)
    except Exception:
        pass

    collection = client.create_collection(
        name=config.CHROMA_COLLECTION,
        metadata={"description": "Анекдоты 1986, RAG for Burunov bot"},
    )

    print("Добавляю векторы в коллекцию...")
    collection.add(
        ids=[str(j["id"]) for j in jokes],
        embeddings=embeddings.tolist(),
        documents=[j["text"] for j in jokes],
        metadatas=[
            {"year": j["year"], "tags": ", ".join(j["tags"]), "embed_text": j["embed_text"]}
            for j in jokes
        ],
    )

    print(f"Готово. В коллекции {collection.count()} анекдотов.")
    print(f"База лежит в: {config.CHROMA_DIR}")


if __name__ == "__main__":
    build()
