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
    """Загружает анекдоты из ACTIVE_JOKES_PATH (filtered если есть, иначе clean)."""
    path = config.ACTIVE_JOKES_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"Сначала запусти prepare_jokes.py (и опционально scripts/filter_jokes.py) "
            f"— нет файла {path}"
        )
    print(f"  Загружаем: {path.name} ({'filtered' if path == config.FILTERED_JOKES_PATH else 'clean'})")
    jokes = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                jokes.append(json.loads(line))
    return jokes


def select_jokes_for_index(jokes: list[dict]) -> list[dict]:
    """Выбирает подмножество для индексации, если стоит MAX_JOKES_FOR_INDEX.

    Приоритет источников (чтобы контент был разнородный, а не только lib.ru):
      1. anekdot.ru — готовые отдельные анекдоты, высокое качество
      2. lib.ru — текстовые файлы, разбитые на абзацы
      3. Крокодил + Anna's Archive — метаданные книг (для подсказок)
    """
    limit = getattr(config, "MAX_JOKES_FOR_INDEX", None)
    if not limit or limit >= len(jokes):
        return jokes

    # Группируем по источнику
    by_source: dict[str, list[dict]] = {"anekdot.ru": [], "lib.ru": [], "Крокодил": [], "Anna's Archive": []}
    for j in jokes:
        src = j.get("source", "")
        if src in by_source:
            by_source[src].append(j)
        else:
            by_source.setdefault("lib.ru", []).append(j)

    # Распределяем лимит: 50% anekdot.ru, 40% lib.ru, 5% Крокодил, 5% Anna's Archive
    quotas = {
        "anekdot.ru": int(limit * 0.50),
        "lib.ru": int(limit * 0.40),
        "Крокодил": int(limit * 0.05),
        "Anna's Archive": int(limit * 0.05),
    }
    # Если какой-то источник не дотянул до квоты — отдадим остаток lib.ru
    selected: list[dict] = []
    leftover = 0
    for src, q in quotas.items():
        pool = by_source.get(src, [])
        take = min(q, len(pool))
        selected.extend(pool[:take])
        leftover += q - take
    if leftover > 0:
        # добавляем из lib.ru (там обычно много)
        extra = by_source.get("lib.ru", [])[quotas["lib.ru"]:quotas["lib.ru"] + leftover]
        selected.extend(extra)

    print(f"  Лимит {limit}: выбрано {len(selected)} (из {len(jokes)})")
    aa_count = sum(1 for j in selected if j.get('source') == "Anna's Archive")
    print(f"    anekdot.ru: {sum(1 for j in selected if j.get('source')=='anekdot.ru')}")
    print(f"    lib.ru:     {sum(1 for j in selected if j.get('source')=='lib.ru')}")
    print(f"    Крокодил:   {sum(1 for j in selected if j.get('source')=='Крокодил')}")
    print(f"    AA books:   {aa_count}")
    return selected


def build():
    jokes = load_clean_jokes()
    print(f"Загружено анекдотов: {len(jokes)}")
    if not jokes:
        raise RuntimeError("Нет анекдотов для индексации.")

    jokes = select_jokes_for_index(jokes)

    print(f"Загружаю эмбеддер {config.EMBED_MODEL} ...")
    # device='cpu' — для хакатона стабильнее. Если есть CUDA, поменяй на 'cuda'.
    model = SentenceTransformer(config.EMBED_MODEL, device="cpu")

    print("Считаю эмбеддинги (это разово, ~1-2 мин на 1000 анекдотов)...")
    t0 = time.time()
    texts = [config.EMBED_PASSAGE_PREFIX + j["embed_text"] for j in jokes]
    # Маленький batch + convert_to_numpy=True чтобы не раздувать память
    embeddings = model.encode(
        texts,
        batch_size=8,
        show_progress_bar=True,
        normalize_embeddings=True,
        convert_to_numpy=True,
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
