"""
retriever.py
────────────
Поиск топ-K анекдотов по теме запроса.
"""
import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

import config


class Retriever:
    _embedder = None
    _collection = None

    @classmethod
    def _ensure_loaded(cls):
        if cls._embedder is None:
            cls._embedder = SentenceTransformer(config.EMBED_MODEL, device="cpu")
        if cls._collection is None:
            client = chromadb.PersistentClient(
                path=str(config.CHROMA_DIR),
                settings=Settings(anonymized_telemetry=False),
            )
            cls._collection = client.get_collection(config.CHROMA_COLLECTION)

    @classmethod
    def search(cls, query: str, top_k: int = config.TOP_K) -> list[dict]:
        """
        Возвращает список вида:
          [{"id", "text", "tags", "year", "score"}, ...]
        отсортированный по убыванию релевантности.
        """
        cls._ensure_loaded()

        # e5 требует префикс "query: " для поисковых запросов
        q_vec = cls._embedder.encode(
            [config.EMBED_QUERY_PREFIX + query],
            normalize_embeddings=True,
        )

        results = cls._collection.query(
            query_embeddings=q_vec.tolist(),
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )

        out = []
        # results — dict со списками внутри (на 1 запрос)
        for i, doc in enumerate(results["documents"][0]):
            dist = results["distances"][0][i]
            # ChromaDB отдаёт cosine distance (0 = identical, 2 = opposite)
            # Переводим в similarity: sim = 1 - dist/2 (для нормализованных векторов)
            similarity = 1 - dist / 2
            if similarity < config.MIN_SIMILARITY:
                continue
            meta = results["metadatas"][0][i]
            out.append({
                "id": results["ids"][0][i],
                "text": doc,
                "tags": [t.strip() for t in meta.get("tags", "").split(",") if t.strip()],
                "year": meta.get("year", 1986),
                "score": round(similarity, 3),
            })
        return out


# ─── Тест из консоли ──────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Штирлиц и Мюллер"
    print(f"Запрос: {q}\n")
    for hit in Retriever.search(q, top_k=5):
        print(f"[{hit['score']}] {hit['tags']}")
        print(f"  {hit['text'][:200]}...")
        print()
