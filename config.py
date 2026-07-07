"""
Конфиг всего RAG-пайплайна.
Меняй тут — остальной код подхватит.
"""
from pathlib import Path

# ─── Пути ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# ─── Режим TTS ─────────────────────────────────────────────────────────
# "edge"   — Piper ONNX локально на G1 (CPU, real-time, без облака)
# "server" — GPT-SoVITS на внешнем сервере с GPU (лучше клон, но нужен WiFi)
TTS_MODE = "edge"

# Адрес TTS-сервера (для server-режима)
TTS_SERVER_HOST = "http://localhost:8001"

# Хосты RAG и TTS (для robot_controller.py)
RAG_HOST = "http://localhost:8000"
TTS_HOST = "http://localhost:8001"
TTS_SPEED = 0.9    # 1.0 = норма, 0.85 = медленно (для Бурунова)

# Таймауты (сек)
RAG_TIMEOUT = 90.0    # Gemma на CPU может думать долго
TTS_TIMEOUT = 60.0    # Piper быстрее, но на всякий случай


# ─── Unitree G1 конфигурация ───────────────────────────────────────────
# Сеть
G1_NETWORK_INTERFACE = "eth0"        # eth0 (кабель) | usb0 (USB-tether) | wlan0 (WiFi)
# IP робота по умолчанию: 192.168.123.161 (multicast 239.168.123.161:5555)

# Аудио (через AudioClient.PlayStream)
G1_ENABLE_AUDIO = True
G1_AUDIO_APP_NAME = "burunov_bot"    # идентификатор приложения для PlayStream
G1_AUDIO_CHUNK_SEC = 1.0             # длительность одного PCM-чанка (1 сек = 32KB)
G1_AUDIO_VOLUME = 100                # громкость 0-100, дока рекомендует 100

# Жесты (через LocoClient)
G1_ENABLE_GESTURES = True

# Кисти рук ( Inspire RH56DFTP — то что в спеке G1 EDU Ultimate D )
# Поддерживаемые типы: "RH56DFTP" | "RH56DFX" | "DEX3-1" | "BRAINCO"
G1_HAND_TYPE = "RH56DFTP"
G1_ENABLE_HANDS = True

# Требования к прошивке (проверить при доступе к G1):
#   Vui_Service    >= 2.0.3.8
#   Vui Module     >= 2.0.0.3
#   Vul Service    >= 2.0.4.4
#   Webrtc Bridge  >= 1.0.7.5
#   Audio Hub      >= 1.0.1.0
#   Firmware       >= 1.3.0 (для GPT voice assistant)

# Сырой JSON от скраппера друга. Формат — список объектов:
# [{"id": ..., "text": "...", "year": 1986, "tags": ["Штирлиц", ...]}, ...]
RAW_JOKES_PATH = DATA_DIR / "jokes_raw.json"

# Очищенный датасет (после prepare_jokes.py)
CLEAN_JOKES_PATH = DATA_DIR / "jokes_clean.jsonl"

# Дополнительно отфильтрованный датасет (после scripts/filter_jokes.py).
# Если существует — build_vector_db.py и retriever.py предпочитают его.
# В нём выкинуты стихи, копирайты, английский, словари акронимов, и т.д.
FILTERED_JOKES_PATH = DATA_DIR / "jokes_filtered.jsonl"

# Автовыбор: если FILTERED_JOKES_PATH существует — используем его
def _active_jokes_path() -> Path:
    if FILTERED_JOKES_PATH.exists():
        return FILTERED_JOKES_PATH
    return CLEAN_JOKES_PATH

ACTIVE_JOKES_PATH = _active_jokes_path()

# Папка ChromaDB (persist-диск)
CHROMA_DIR = DATA_DIR / "chroma_db"
CHROMA_COLLECTION = "jokes_1986"

# ─── Модели ────────────────────────────────────────────────────────────
# Эмбеддинги. e5-small лёгкая (~120 МБ), хорошо работает с русским.
# Префикс "query: " / "passage: " — это требование e5, не убирай.
EMBED_MODEL = "intfloat/multilingual-e5-small"
EMBED_QUERY_PREFIX = "query: "
EMBED_PASSAGE_PREFIX = "passage: "

# LLM через Ollama. Установи: ollama pull gemma3:4b (или как у них Gemma 4 зовётся)
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_MODEL = "gemma3:4b"
OLLAMA_OPTIONS = {
    "temperature": 0.7,   # побольше креатива, но не в космос
    "top_p": 0.9,
    "num_predict": 600,   # анекдот не должен быть длинным
}

# ─── RAG параметры ─────────────────────────────────────────────────────
TOP_K = 5                # сколько анекдотов достаём из базы
MIN_SIMILARITY = 0.35    # ниже этого — считаем что ничего не нашли

# Лимит на размер индексируемого датасета (None = все).
# 27k анекдотов на CPU индексируются ~2.7 часа; для демо поставь 3000-5000.
MAX_JOKES_FOR_INDEX = 1500

# ─── Промпт Бурунова ───────────────────────────────────────────────────
# Это сердце "стиля". Менять аккуратно — от него зависит узнаваемость.
SYSTEM_PROMPT = """Ты — Сергей Бурунов. По легенде тебя нашли в 1986 году,
когда ты травил анекдоты в курилке киностудии Мосфильм.

Твоя задача — рассказать ОДИН анекдот из предоставленного контекста.

СТИЛЬ ПОДАЧИ (критично):
- Ленивый, неторопливый, с лёгкой хрипотцой
- Ироничный, будто ты сам над анекдотом усмехаешься
- Паузы обозначай многоточиями «...»
- Не кричи, не торопись, не добавляй смайлики
- Если анекдот про алкоголь, курение или быт — подавай как родную стихию
- В конце можешь добавить короткую реплику-комментарий в духе:
  «Ну, ты понял...» или «Вот так вот...»

ПРАВИЛА:
- Используй ТОЛЬКО анекдоты из контекста ниже
- Не выдумывай новые сюжеты
- Не упоминай, что это анекдот из контекста или из базы
- Не здоровайся, не прощайся, не объясняй контекст
- Текст должен звучать так, будто его будут ЗАЧИТЫВАТЬ вслух голосом
"""

USER_PROMPT_TEMPLATE = """Контекст (реальные анекдоты 1986 года):

{context}

Тема, которую просит слушатель: «{topic}»

Выбери САМЫЙ подходящий анекдот из контекста под эту тему
и расскажи его в своём стиле. Помни: один анекдот, без отсебятины."""
