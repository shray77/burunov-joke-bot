"""
edge_tts_server.py
──────────────────
FastAPI-обёртка над Piper TTS (ONNX) для запуска на самом роботе G1.

Особенности:
  - CPU-only (работает на 4 ядрах x86 ARM)
  - Real-time: 1.5-2x скорости синтеза
  - Лёгкая модель (~60 МБ)
  - Один wav на запрос (для streaming смотри /stream)

────────────────────────────────────────────────────────────────────────
Установка на G1:
  pip install piper-tts
  sudo apt install espeak-ng
  # + burunov.onnx и burunov.onnx.json в MODELS_DIR

Запуск:
  python edge_tts_server.py
  # или
  uvicorn edge_tts_server:app --host 0.0.0.0 --port 8001
"""
import io
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

# ─── Конфиг ───────────────────────────────────────────────────────────
# Папка с обученной моделью Piper (кладёшь burunov.onnx + .json сюда)
MODELS_DIR = Path("./models")
MODEL_NAME = "burunov"   # ищем burunov.onnx + burunov.onnx.json

# Куда кэшировать синтез (для повторяющихся запросов)
CACHE_DIR = Path("./cache_tts")
CACHE_DIR.mkdir(exist_ok=True)

# Длина sentences для streaming
SENTENCE_BATCH = 1

# ─── FastAPI ──────────────────────────────────────────────────────────
app = FastAPI(title="Edge TTS (Piper ONNX)", version="0.1.0")
_piper = None


def load_piper():
    """Ленивая загрузка Piper-модели. Тяжёлая инициализация (~2 сек)."""
    global _piper
    if _piper is not None:
        return _piper

    from piper.voice import PiperVoice
    onnx_path = MODELS_DIR / f"{MODEL_NAME}.onnx"
    config_path = MODELS_DIR / f"{MODEL_NAME}.onnx.json"

    if not onnx_path.exists():
        raise FileNotFoundError(
            f"Нет модели: {onnx_path}. Обучи Piper и положи сюда."
        )

    _piper = PiperVoice.load(
        str(onnx_path),
        config_path=str(config_path) if config_path.exists() else None,
    )
    return _piper


class SynthRequest(BaseModel):
    text: str = Field(..., examples=["Штирлиц подошёл к окну."])
    speed: float = Field(default=1.0, ge=0.5, le=2.0,
                          description="1.0 = норма, 0.85 = медленно (для Бурунова)")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model_loaded": _piper is not None,
        "model_name": MODEL_NAME,
        "model_exists": (MODELS_DIR / f"{MODEL_NAME}.onnx").exists(),
    }


def _synth_to_wav_bytes(text: str, speed: float) -> bytes:
    """
    Синтез через Piper → wav bytes (22050 Hz, mono, 16-bit).
    """
    import wave
    import numpy as np

    piper = load_piper()

    # Piper синтезирует по предложениям, надо склеить
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setframerate(22050)
        wav_file.setsampwidth(2)
        wav_file.setnchannels(1)

        # synthesize() отдаёт по одному chunks AudioChunk
        for chunk in piper.synthesize(text, length_scale=1.0/speed):
            # chunk.audio — int16 numpy array
            wav_file.writeframes(chunk.audio.tobytes())

    return buf.getvalue()


@app.post("/synthesize")
def synth(req: SynthRequest):
    """Синхронный синтез. Возвращает wav целиком (с заголовком)."""
    if not req.text.strip():
        raise HTTPException(400, "text пустой")
    try:
        t0 = time.time()
        wav_bytes = _synth_to_wav_bytes(req.text, req.speed)
        elapsed = time.time() - t0
        # Длительность аудио = len(bytes) / (22050 * 2)
        audio_sec = len(wav_bytes) / (22050 * 2 + 44)  # +44 wav header
        print(f"[synth] {len(req.text)} chars → {audio_sec:.1f}s audio in {elapsed:.2f}s "
              f"({audio_sec/elapsed:.1f}x realtime)")
    except FileNotFoundError as e:
        raise HTTPException(500, f"Model missing: {e}")
    except Exception as e:
        raise HTTPException(500, f"Synthesis failed: {e}")

    return Response(content=wav_bytes, media_type="audio/wav")


@app.post("/synthesize_pcm")
def synth_pcm(req: SynthRequest):
    """
    Синтез → чистый PCM без заголовка (16kHz, mono, 16-bit).
    Это РОДНОЙ формат для unitree_sdk2 AudioClient.PlayStream().

    Используется robot_controller.py для стриминга на G1.
    """
    if not req.text.strip():
        raise HTTPException(400, "text пустой")
    try:
        t0 = time.time()
        wav_bytes = _synth_to_wav_bytes(req.text, req.speed)
        # Срезаем WAV-заголовок, оставляем только PCM
        data_start = wav_bytes.find(b"data")
        if data_start == -1:
            raise HTTPException(500, "WAV format error")
        pcm_data = wav_bytes[data_start + 8:]
        elapsed = time.time() - t0
        audio_sec = len(pcm_data) / (22050 * 2)
        print(f"[synth_pcm] {len(req.text)} chars → {audio_sec:.1f}s PCM in {elapsed:.2f}s")
    except FileNotFoundError as e:
        raise HTTPException(500, f"Model missing: {e}")
    except Exception as e:
        raise HTTPException(500, f"Synthesis failed: {e}")

    return Response(content=pcm_data, media_type="application/octet-stream")


@app.post("/stream")
def stream(req: SynthRequest):
    """
    Streaming-синтез. Бьём по предложениям, отдаём чанки.
    Для длинных анекдотов юзер слышит быстрее.
    """
    import re
    sentences = re.split(r'(?<=[.!?…])\s+', req.text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        raise HTTPException(400, "text пустой")

    def gen():
        for s in sentences:
            try:
                wav = _synth_to_wav_bytes(s, req.speed)
                yield wav
            except Exception as e:
                print(f"[stream] sentence failed: {e}")
                continue

    return StreamingResponse(gen(), media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
