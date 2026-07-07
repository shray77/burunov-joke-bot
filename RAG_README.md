# RAG Pipeline — Burunov Joke Bot

End-to-end RAG: тема → semantic search по 27 326 анекдотам → генерация в стиле Сергея Бурунова.

## Архитектура

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│  User topic     │ -> │   Retriever      │ -> │   Generator     │ -> текст для TTS
│  "Штирлиц"      │    │   ChromaDB + e5  │    │   Ollama/LLM    │
└─────────────────┘    │   top-K=5        │    │   (fallback)    │
                       └──────────────────┘    └─────────────────┘
                              ↑
                       ┌──────────────────┐
                       │  27 326 анекдотов│
                       │  from 4 sources  │
                       └──────────────────┘
```

## Источники данных

| Источник | Анекдотов | Описание |
|---|---|---|
| `lib.ru/ANEKDOTY` | 22 418 | Текстовый архив Мошкова (Остер, Филатов, Кривин, митьки) |
| `anekdot.ru` 1996-1999 | 4 751 | Готовые отдельные анекдоты (Штирлиц, Вовочка, Чапаев) |
| Anna's Archive книги | 413 | Метаданные книг про анекдоты (с MD5-ссылками) |
| Журн. «Крокодил» 1985-1990 | 558 | Метаданные PDF-выпусков |

**После дедупликации: 27 326 уникальных анекдотов.**

## Запуск

### 1. Установить зависимости

```bash
pip install -r requirements.txt
```

### 2. Подготовить данные (опционально)

Если есть свежие скрапы в `download/`:
```bash
python3 prepare_jokes.py    # -> data/jokes_clean.jsonl (27 326 анекдотов)
```

### 3. Построить векторную базу

```bash
python3 build_vector_db.py   # -> data/chroma_db/ (ChromaDB + e5 embeddings)
```

В `config.py` можно настроить `MAX_JOKES_FOR_INDEX`:
- `None` — все 27 326 (≈2.7 часа на CPU)
- `1500` — дефолт для демо (≈45 сек на CPU)

### 4. Тест retriever

```bash
python3 retriever.py "Штирлиц и Мюллер"
```

### 5. End-to-end тест (retrieval + генерация)

```bash
python3 rag_pipeline.py "Штирлиц"
python3 rag_pipeline.py "Вовочка"
python3 rag_pipeline.py "Чапаев"
```

### 6. FastAPI сервер

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

Эндпоинты:
- `GET /` — healthcheck
- `GET /search?q=Штирлиц&top_k=5` — только retriever (дебаг)
- `POST /tell` — главная точка: `{"topic": "Штирлиц"}` → `{"text": "...", "sources": [...]}`

## LLM: Ollama (опционально)

Для полноценной генерации в стиле Бурунова нужен Ollama:

```bash
# Установить: https://ollama.com
ollama pull gemma3:4b
ollama serve
```

Если Ollama недоступен — `generator.py` использует **fallback-режим**:
берёт топ-1 анекдот из retrieval и оборачивает его в "буруновские"
интро/аутро ("Ну, слушай...", "Такие дела, дорогой...").

## Конфигурация (config.py)

```python
EMBED_MODEL = "intfloat/multilingual-e5-small"  # 120 МБ, RU ок
OLLAMA_MODEL = "gemma3:4b"                       # или llama3.2, qwen2.5
TOP_K = 5                                        # сколько анекдотов достаём
MIN_SIMILARITY = 0.35                            # порог релевантности
MAX_JOKES_FOR_INDEX = 1500                       # лимит для демо
```

## Пример вывода

```
$ python3 rag_pipeline.py "Штирлиц"

ТЕМА: Штирлиц
FALLBACK: False
ИСТОЧНИКОВ: 5
  [0.833] ['anekdot.ru', 'Штирлиц'] — Подходит Мюллер к Борману...
  [0.832] ['anekdot.ru', 'Штирлиц'] — Штирлиц идет подвалами гестапо...

ТЕКСТ ДЛЯ TTS:
────────────────────────────────────────────────────────────────
Значит, так... Подходит Мюллер к Борману... - Борман, а вы знаете,
что Штирлиц - русский шпион? Борман (ласково так): - Да бог с ним,
вы лучше послушайте, какую я песню сочинил: ``Мила-ая моя,...``
Ну, бывает...
────────────────────────────────────────────────────────────────
```

## Структура файлов

```
config.py              — конфиг всего пайплайна
prepare_jokes.py       — загрузка 4 JSON-источников → data/jokes_clean.jsonl
build_vector_db.py     — JSONL → embeddings → ChromaDB
retriever.py           — semantic search по анекдотам
generator.py           — Ollama LLM + fallback-стилизация
rag_pipeline.py        — склейка retriever + generator
api.py                 — FastAPI сервер

scripts/
  anekdot_scraper.py          — Anna's Archive скрапер (1264 книг)
  extra_sources_scraper.py    — lib.ru + anekdot.ru + Крокодил

download/
  anekdoty_sssr_1985-1990.{md,json}     — книги AA (11 релевантных + 421 контекст)
  libru_anekdoty.{md,json}              — 252 файла с lib.ru
  anekdotru_1996-2000.{md,json}         — 4800 анекдотов с anekdot.ru
  krokodil_1985-1990.{md,json}          — 558 выпусков «Крокодил»
```
