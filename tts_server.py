"""
tts_server.py
─────────────
FastAPI-обёртка над GPT-SoVITS для real-time инференса голоса Бурунова.

Эндпоинты:
  GET  /health           — статус + модель загружена?
  POST /synthesize       — text → wav (sync, для коротких фраз)
  POST /stream           — text → wav stream (для длинных анекдотов)

Запуск:
  python tts_server.py
  # или
  uvicorn tts_server:app --host 0.0.0.0 --port 8001

────────────────────────────────────────────────────────────────────────
ВАЖНО: GPT-SoVITS не ставится через pip. Клонируй репо и ставь отдельно:

  git clone https://github.com/RVC-Boss/GPT-SoVITS
  cd GPT-SoVITS
  pip install -r requirements.txt
  # Скачать предобученные модели:
  #   https://huggingface.co/lj1995/GPT-SoVITS
  # Положить в GPT-SoVITS/GPT_SoVITS/pretrained_models/

После fine-tune на Бурунове у тебя появится:
  burunov.ckpt      — GPT модель
  burunov.pth       — SoVITS модель
  reference.wav     — референсный аудио (5-10 сек чистого Бурунова)
  reference.txt     — что он говорит в референсе

Пропиши к ним пути в config.TTS_* (см. ниже в этом файле).
"""
import io
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel, Field
import httpx

# ─── TTS-настройки (можно вынести в config.py, но для простоты тут) ──
# Пути к fine-tuned моделям GPT-SoVITS (после обучения)
GPT_SOVITS_DIR = Path("./GPT-SoVITS")          # путь к клону репо
CKPT_PATH = GPT_SOVITS_DIR / "GPT_weights" / "burunov-e10.ckpt"
SOVITS_PATH = GPT_SOVITS_DIR / "SoVITS_weights" / "burunov-e10.pth"

# Референс для zero-shot/few-shot клонирования (короткий чистый кусок)
# GPT-SoVITS использует референс для тембра
REF_AUDIO = GPT_SOVITS_DIR / "reference" / "burunov_ref.wav"
REF_TEXT = "Здрасьте. Это я, Бурунов. Слушай анекдот."  # что сказано в референсе

# Язык референса и синтеза
REF_LANG = "ru"
SYNTH_LANG = "ru"

# Скорость/темп: 1.0 = норма. Бурунов = медленно, ~0.9
SPEED = 0.9
# Top-p / temperature для разнообразия
TOP_P = 0.7
TEMPERATURE = 0.6

# ─── FastAPI ──────────────────────────────────────────────────────────
app = FastAPI(title="Burunov TTS Server", version="0.1.0")

# Ленивая загрузка модели — чтобы сервер стартовал быстро
_tts_pipeline = None


def load_tts():
    """Импортирует GPT-SoVITS и поднимает pipeline. Тяжёлая инициализация."""
    global _tts_pipeline
    if _tts_pipeline is not None:
        return _tts_pipeline

    import sys
    sys.path.insert(0, str(GPT_SOVITS_DIR))

    from GPT_SoVITS.inference_webui import get_tts_pipeline

    _tts_pipeline = get_tts_pipeline(
        gpt_path=str(CKPT_PATH),
        sovits_path=str(SOVITS_PATH),
        ref_audio_path=str(REF_AUDIO),
        ref_text=REF_TEXT,
        ref_lang=REF_LANG,
    )
    return _tts_pipeline


class SynthRequest(BaseModel):
    text: str = Field(..., examples=["Штирлиц подошёл к окну. Из окна дуло..."])
    speed: float = Field(default=SPEED, ge=0.5, le=2.0)
    top_p: float = Field(default=TOP_P, ge=0.1, le=1.0)
    temperature: float = Field(default=TEMPERATURE, ge=0.1, le=2.0)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "tts_loaded": _tts_pipeline is not None,
        "ckpt_exists": CKPT_PATH.exists(),
        "sovits_exists": SOVITS_PATH.exists(),
        "ref_audio_exists": REF_AUDIO.exists(),
    }


def _synth_to_bytes(text: str, speed: float, top_p: float, temperature: float) -> bytes:
    """
    Синтез → возвращаем wav bytes (16kHz, mono, 16-bit).
    GPT-SoVITS отдаёт numpy array, мы оборачиваем в wav через soundfile.
    """
    import numpy as np
    import soundfile as sf

    pipeline = load_tts()
    audio_data = pipeline.synthesize(
        text=text,
        language=SYNTH_LANG,
        speed=speed,
        top_p=top_p,
        temperature=temperature,
    )

    # GPT-SoVITS отдаёт list/np.array с float32 в [-1, 1]
    if not isinstance(audio_data, np.ndarray):
        audio_data = np.array(audio_data, dtype=np.float32)

    buf = io.BytesIO()
    sf.write(buf, audio_data, 16000, format="WAV", subtype="PCM_16")
    return buf.getvalue()


@app.post("/synthesize")
def synth(req: SynthRequest):
    """Синхронный синтез. Для коротких фраз (до ~20 сек)."""
    if not req.text.strip():
        raise HTTPException(400, "text пустой")
    try:
        t0 = time.time()
        wav_bytes = _synth_to_bytes(
            req.text, req.speed, req.top_p, req.temperature
        )
        elapsed = time.time() - t0
        print(f"[synth] {len(req.text)} chars → {len(wav_bytes)} bytes in {elapsed:.2f}s")
    except FileNotFoundError as e:
        raise HTTPException(500, f"Model files missing: {e}")
    except Exception as e:
        raise HTTPException(500, f"Synthesis failed: {e}")

    return Response(content=wav_bytes, media_type="audio/wav")


@app.post("/stream")
def stream(req: SynthRequest):
    """
    Streaming-синтез. Разбиваем текст по предложениям,
    отдаём wav чанками. Для длинных анекдотов — юзер слышит быстрее.
    """
    import re
    # Бьём по . ! ? … — сохраняя разделитель
    sentences = re.split(r'(?<=[.!?…])\s+', req.text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        raise HTTPException(400, "text пустой")

    def gen():
        for s in sentences:
            try:
                wav = _synth_to_bytes(s, req.speed, req.top_p, req.temperature)
                yield wav
            except Exception as e:
                print(f"[stream] sentence failed: {e}")
                continue

    return StreamingResponse(gen(), media_type="audio/wav")


# ─── Интеграция с RAG: комбинированный эндпоинт ───────────────────────
class TellVoiceRequest(BaseModel):
    topic: str = Field(..., examples=["Штирлиц и Мюллер"])

@app.post("/tell_voice")
def tell_voice(req: TellVoiceRequest):
    """
    Один вызов: тема → RAG → текст → TTS → wav bytes.
    Так удобнее клиенту (роботу): одна HTTP-ручка на всё.
    """
    RAG_HOST = "http://localhost:8000"   # api.py (RAG)
    try:
        with httpx.Client(timeout=120.0) as client:
            rag_resp = client.post(
                f"{RAG_HOST}/tell",
                json={"topic": req.topic},
            )
            rag_resp.raise_for_status()
            rag_data = rag_resp.json()
    except Exception as e:
        raise HTTPException(502, f"RAG failed: {e}")

    text = rag_data.get("text", "")
    if not text:
        raise HTTPException(500, "RAG вернул пустой текст")

    wav_bytes = _synth_to_bytes(text, SPEED, TOP_P, TEMPERATURE)
    return Response(content=wav_bytes, media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
