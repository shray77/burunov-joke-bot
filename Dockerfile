# ─── RAG (Gemma через Ollama + ChromaDB + FastAPI) ────────────────────
FROM python:3.11-slim AS rag

WORKDIR /app

# Системные зависимости для sentence-transformers + chromadb
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py prepare_jokes.py build_vector_db.py \
     retriever.py generator.py rag_pipeline.py api.py ./

# Данные: jokes_clean.jsonl + chroma_db (пробрасываем через volume)
# Ollama должна быть доступна по OLLAMA_HOST

EXPOSE 8000
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]


# ─── Edge TTS (Piper ONNX, CPU-only) ─────────────────────────────────
FROM python:3.11-slim AS tts

WORKDIR /app

# espeak-ng обязателен для Piper русского
RUN apt-get update && apt-get install -y --no-install-recommends \
    espeak-ng \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    piper-tts \
    fastapi==0.115.6 \
    uvicorn[standard]==0.34.0 \
    pydantic==2.10.4

COPY edge_tts_server.py ./

# Модель burunov.onnx кладём через volume в /app/models/

EXPOSE 8001
CMD ["uvicorn", "edge_tts_server:app", "--host", "0.0.0.0", "--port", "8001"]


# ─── Robot Controller (оркестратор + жесты) ──────────────────────────
FROM python:3.11-slim AS robot

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    portaudio19-dev \
    python3-pyaudio \
    espeak-ng \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    httpx==0.28.1 \
    pyaudio==0.2.14 \
    numpy

# unitree_sdk2_python — клонируется отдельно, см. EDGE_README.md
# COPY unitree_sdk2_python ./unitree_sdk2_python
# RUN pip install -e ./unitree_sdk2_python

COPY robot_controller.py unitree_gestures.py ./

# Запускается в интерактивном режиме (или как service)
CMD ["python", "robot_controller.py", "Штирлиц и Мюллер"]
