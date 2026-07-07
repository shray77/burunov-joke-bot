# Burunov Joke Bot — RAG Pipeline

RAG-система для бота, который рассказывает анекдоты 1986 года голосом
Сергея Бурунова.

## Что внутри

```
jokes_raw.json  →  prepare_jokes.py  →  jokes_clean.jsonl
                                              ↓
                                    build_vector_db.py
                                              ↓
                                       ChromaDB (векторная база)
                                              ↓
retriever.py ←───────────────────────────────┘
     ↓
generator.py (Ollama + Gemma, промпт Бурунова)
     ↓
rag_pipeline.py  →  api.py (FastAPI)
```

## Запуск по шагам

### 1. Зависимости

```bash
pip install -r requirements.txt
```

### 2. Ollama + LLM

```bash
# Установи Ollama: https://ollama.com
ollama pull gemma3:4b    # или gemma4, как у тебя зовётся
ollama serve             # запустит на localhost:11434
```

Проверь что работает:
```bash
curl http://localhost:11434/api/tags
```

### 3. Датасет анекдотов

Попроси друга-скраппера положить сюда:
```
data/jokes_raw.json
```

Формат (важно!):
```json
[
  {
    "id": 1,
    "text": "Штирлиц подошёл к окну. Из окна дуло...",
    "year": 1986,
    "tags": ["Штирлиц"]
  }
]
```

Если у него другой формат — поправь `load_raw()` в `prepare_jokes.py`.

### 4. Чистка датасета

```bash
python prepare_jokes.py
```

Создаст `data/jokes_clean.jsonl`. Должно вывести статистику:
```
Загружено сырых анекдотов: N
После чистки: M (выкинуто K)
После дедупликации: ...
Топ-10 тегов: ...
```

### 5. Векторная база

```bash
python build_vector_db.py
```

Первый запуск скачает `multilingual-e5-small` (~120 МБ), потом индексация
1-2 минуты на 1000 анекдотов. Результат: `data/chroma_db/`.

### 6. Тест retriever-а

```bash
python retriever.py "Штирлиц и Мюллер"
```

Должен вывести топ-5 анекдотов с косинусной близостью.

### 7. Тест пайплайна целиком

```bash
python rag_pipeline.py "Штирлиц и Мюллер"
```

Должен вывести текст в стиле Бурунова + список источников.

### 8. Поднять API

```bash
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

Документация: http://localhost:8000/docs

Тест:
```bash
curl -X POST http://localhost:8000/tell \
  -H "Content-Type: application/json" \
  -d '{"topic": "Штирлиц"}'
```

## Что подключать дальше (TTS)

API отдаёт `text` — это готовая строка для TTS. Дальше скорми её в
GPT-SoVITS / XTTS-v2 / Fish Speech (клон Бурунова) и получи wav.
Склейку можно сделать прямо в эндпоинте `/tell`, добавив поле `audio_url`.

## Тюнинг под себя

- **Промпт Бурунова** — в `config.py`, `SYSTEM_PROMPT`. От него зависит
  узнаваемость стиля. Пробуй разные формулировки.
- **TOP_K** — сколько анекдотов давать в контекст. 3-5 оптимально.
- **MIN_SIMILARITY** — порог отсечения мусора. 0.3-0.4 норм.
- **temperature** в `OLLAMA_OPTIONS` — выше = креативнее, ниже = точнее.

## Частые баги

| Симптом | Причина |
|---|---|
| `Connection refused localhost:11434` | Ollama не запущена. `ollama serve` |
| `model not found` | Не сделал `ollama pull`. Имя модели в `config.py` |
| Retriever пустой | Не построил базу. `python build_vector_db.py` |
| LLM выдаёт отсебятину | Подними `MIN_SIMILARITY`, опусти `temperature` |
| LLM здоровается/прощается | Допиши запрет в `SYSTEM_PROMPT` |
