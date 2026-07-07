# Burunov Joke Bot — Unitree G1 × Бурунов

Медиа-проект Олега Сироты (Истринская сыроварня): робот Unitree G1 EDU,
которого «нашли в сарае в 1986 году», рассказывает анекдоты той эпохи
голосом Сергея Бурунова.

## Архитектура проекта

```
[Скраппер (друг)]                  # scripts/anekdot_scraper.py
  ↓ сборники с Anna's Archive       # download/anekdoty_1986.json
[prepare_jokes.py]                 # чистка/дедупликация
  ↓
[build_vector_db.py]               # ChromaDB + e5-small
  ↓
[retriever.py] ← topic             # поиск топ-K
  ↓
[generator.py]                     # Ollama (Gemma) + промпт Бурунова
  ↓ текст в стиле Бурунова
[edge_tts_server.py]               # Piper ONNX → PCM 16kHz mono 16-bit
  ↓ /synthesize_pcm
[robot_controller.py]              # оркестратор на G1
  ├─ unitree_audio.py    → AudioClient.PlayStream() → динамик Stanley
  ├─ unitree_gestures.py → LocoClient.Sit/StandUp/Move → жесты в такт речи
  ├─ unitree_hands.py    → HandClient (Inspire RH56DFTP) → кисти рук
  └─ LedControl()        → RGB-лента 256 цветов
```

## Что в репо

| Файл | Назначение |
|---|---|
| `scripts/anekdot_scraper.py` | Скраппер Anna's Archive (друг) |
| `config.py` | Все настройки + промпт Бурунова + параметры G1 |
| `prepare_jokes.py` | Чистка/дедупликация датасета анекдотов |
| `build_vector_db.py` | ChromaDB + multilingual-e5-small |
| `retriever.py` | Поиск топ-K анекдотов по теме |
| `generator.py` | Ollama (Gemma) + системный промпт Бурунова |
| `rag_pipeline.py` | Склейка retriever+generator |
| `api.py` | FastAPI RAG: `POST /tell {topic}` → `{text, sources}` |
| `audio_prep.py` | Подготовка аудио Бурунова (Whisper+demucs) |
| `piper_train_prep.py` | Конвертация датасета в LJSpeech-формат |
| `edge_tts_server.py` | **Edge TTS** на G1 (Piper ONNX, `/synthesize_pcm`) |
| `tts_server.py` | Server-режим TTS (GPT-SoVITS, нужен GPU) |
| `tts_client.py` | Простой клиент (для тестов без G1) |
| **`robot_controller.py`** | **Оркестратор на G1** |
| **`unitree_audio.py`** | **AudioClient.PlayStream + LedControl** |
| **`unitree_gestures.py`** | **LocoClient (Sit/StandUp/Move) + жесты в такт речи** |
| **`unitree_hands.py`** | **HandClient для Inspire RH56DFTP** |
| `Dockerfile` / `docker-compose.yml` | Деплой на G1 одной командой |
| `README.md` | Этот файл |
| `TTS_README.md` | Обучение GPT-SoVITS (server-режим) |
| `EDGE_README.md` | **Деплой на G1 (главная инструкция)** |

## Три блока

1. **RAG-пайплайн** (ниже) — текст анекдота в стиле Бурунова
2. **Edge TTS** (`EDGE_README.md`) — клон голоса Бурунова через Piper ONNX
3. **Robot integration** — `unitree_audio.py` + `unitree_gestures.py` + `unitree_hands.py`

---

# Часть 1. RAG-пайплайн

## Запуск по шагам

### 1. Зависимости

```bash
pip install -r requirements.txt
```

### 2. Ollama + LLM

```bash
# Установи Ollama: https://ollama.com
ollama pull gemma3:4b
ollama serve
```

### 3. Датасет анекдотов

Положить в `data/jokes_raw.json` (формат — список `[{id, text, year, tags}]`).
Сгенерируй из `download/anekdoty_1986.json` или попроси друга скраппера
сделать конвертер (см. ниже — у него JSON со *сборниками книг*, не с
самими анекдотами).

### 4. Чистка датасета

```bash
python prepare_jokes.py            # -> data/jokes_clean.jsonl (~27 000 анекдотов)
python scripts/filter_jokes.py     # -> data/jokes_filtered.jsonl (~18 000, без стихов/копирайтов/мусора)
```

`build_vector_db.py` автоматически берёт `jokes_filtered.jsonl` если он есть.

### 5. Векторная база

```bash
python build_vector_db.py
```

### 6. Тест

```bash
python retriever.py "Штирлиц и Мюллер"
python rag_pipeline.py "Штирлиц"
```

### 7. Поднять API

```bash
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

Документация: http://localhost:8000/docs

---

# Часть 2. Скраппер анекдотов (от друга)

Параллельный асинхронный скрапер [Anna's Archive](https://ru.annas-archive.gl)
для поиска сборников советских анекдотов 1986 года.

## Что делает

- Параллельно обходит несколько поисковых запросов (`сборник анекдотов 1986`, `анекдоты 1986`, ...)
- Авто-детектит пагинацию и собирает все страницы результатов
- Для каждой книги открывает детальную страницу `/md5/...` и достаёт структурированные метаданные: название, автор, издатель, год, язык, формат, размер, категория, источник, альтернативные имена файлов
- Фильтрует по 1986 году и релевантности (тема анекдотов)
- Сохраняет результаты в **Markdown** (`download/anekdoty_1986.md`) и **JSON** (`download/anekdoty_1986.json`)

## Стек

- **Python 3.11+**
- [`httpx`](https://www.python-httpx.org/) (HTTP/2, async)
- [`beautifulsoup4`](https://www.crummy.com/software/BeautifulSoup/) + `lxml`
- `asyncio` для параллелизма (concurrency=20)

## Запуск

```bash
pip install httpx beautifulsoup4 lxml
python scripts/anekdot_scraper.py
```

## Конфигурация

Все параметры в начале файла `scripts/anekdot_scraper.py`:

```python
BASE_URL = "https://ru.annas-archive.gl"
QUERIES = ["сборник анекдотов 1986", "анекдоты 1986", ...]
YEAR_FILTER = "1986"
DETAIL_CONCURRENCY = 20
MAX_PAGES_PER_QUERY = 10
```

## Историческая справка (важно для лора!)

В СССР 1986 года сборники анекдотов **официально не издавались** — жанр
оставался неофициальным и распространялся устно/через самиздат. Первые
легальные сборники появились только в позднюю перестройку (1988–1989) и
массово — после 1991 года (Хазанов, Никулин, Карцев).

Поэтому фильтр ровно по 1986 году даёт ~0 результатов, но скрапер собирает
250+ контекстных сборников анекдотов за 1995–2023 годы. Для RAG-пайплайна
нужно либо:
- Вытащить сами тексты анекдотов из этих сборников (pdf-экстрактор)
- Либо дополнительно спарсить тексты анекдотов с anekdot.ru/lib.ru и
  пометить их как "эпоха 1986" вручную

---

## Лицензия

MIT
