"""
f5_tts_server.py — FastAPI сервер над F5-TTS-Russian (zero-shot клон голоса Бурунова).

Модель: https://huggingface.co/hotstone228/F5-TTS-Russian
Автор: hotstone228 — дообученный F5-TTS на русском языке.

Преимущества:
  - Zero-shot клон: даёшь референс аудио Бурунова + текст → синтез его голосом
  - Не надо обучать модель — всего лишь подобрать хороший референс (5-15 сек чистого голоса)
  - Качество выше чем у Piper

Риски:
  - На CPU G1 (x86 мини-ПК) F5-TTS может быть медленным (flow-matching = много шагов)
  - На GPU был бы real-time, но GPU на G1 нет
  - Если real-time factor > 2-3x → голос Бурунова будет запаздывать

План:
  - F5-TTS как ОСНОВНОЙ голос Бурунова если потянет на CPU
  - Piper ONNX как FALLBACK если F5 медленный (см. edge_tts_server.py)
  - edge_tts_server.py и f5_tts_server.py НЕ должны висеть на одном порту одновременно.
    Запускать один из них на :8001.

API (совместим с edge_tts_server.py):
  POST /synthesize_pcm  {text}            -> PCM 16kHz mono 16-bit (без заголовка)
  POST /synthesize_wav  {text}            -> WAV 16kHz mono 16-bit
  GET  /health                             -> {ready, model, device, ref_audio_loaded}
  GET  /info                               -> инфа о модели

Запуск:
  python3 f5_tts_server.py --port 8001 --ref-audio /path/to/burunov_ref.wav --ref-text "Привет, я Бурунов"

Требования:
  pip install f5-tts torch torchaudio fastapi uvicorn soundfile numpy
  # Может потребоваться pyrpr (Radeon) или CUDA torch для GPU (на G1 бесполезно — GPU нет)

ВАЖНО: если на G1 F5-TTS выдаёт RTF > 2 (т.е. 10 сек речи синтезируются дольше 20 сек),
       переключаемся на edge_tts_server.py (Piper) — он быстрее на CPU.
"""
from __future__ import annotations

import os
import sys
import io
import time
import wave
import logging
import argparse
import tempfile
from typing import Optional

import numpy as np

log = logging.getLogger("f5_tts")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")


# -----------------------------------------------------------------------------
# Обёртка над F5-TTS
# -----------------------------------------------------------------------------
class F5TTSWrapper:
    """
    Zero-shot TTS через F5-TTS-Russian.

    TODO_SDK/F5: точный API зависит от версии f5-tts. Возможные варианты:
      from f5_tts.api import F5TTS
      from f5_tts.infer.utils_infer import load_model, infer_process

    Свериться с:
      - https://huggingface.co/hotstone228/F5-TTS-Russian
      - https://github.com/SWivid/F5-TTS
    """

    def __init__(self, model_name: str = "hotstone228/F5-TTS-Russian", device: str = "cpu"):
        self.model_name = model_name
        self.device = device  # 'cpu' на G1, 'cuda' если есть GPU
        self.model = None
        self.ref_audio_path: Optional[str] = None
        self.ref_text: Optional[str] = None
        self._ready = False
        self._ref_loaded = False

    def load_model(self) -> bool:
        """Загрузить модель F5-TTS (один раз при старте сервера)."""
        try:
            # TODO_F5: проверить реальный API под hotstone228/F5-TTS-Russian
            # Возможный вариант 1 (через f5_tts.api):
            #   from f5_tts.api import F5TTS
            #   self.model = F5TTS(model_type="F5-TTS", device=self.device)
            #   # HF-репозиторий с моделью скачается автоматически

            # Возможный вариант 2 (через infer utils):
            #   from f5_tts.infer.utils_infer import load_model, load_vocoder
            #   from f5_tts.model import DiT
            #   ...
            log.warning("F5TTSWrapper.load_model() — STUB. Реальная загрузка F5-TTS не подключена.")
            log.warning(f"  -> Должна загрузить модель {self.model_name} на {self.device}")
            log.warning("  -> Свериться с https://huggingface.co/hotstone228/F5-TTS-Russian")
            self._ready = False
            return False
        except Exception as e:
            log.error(f"load_model failed: {e}")
            self._ready = False
            return False

    def load_reference(self, ref_audio_path: str, ref_text: str) -> bool:
        """
        Загрузить референс аудио Бурунова для zero-shot клонирования.
        ref_audio_path: wav 16kHz mono, 5-15 сек чистого голоса Бурунова (без музыки/шума)
        ref_text: точная транскрипция того что говорится в ref_audio
        """
        if not os.path.exists(ref_audio_path):
            log.error(f"ref audio не найден: {ref_audio_path}")
            return False
        self.ref_audio_path = ref_audio_path
        self.ref_text = ref_text
        self._ref_loaded = True
        log.info(f"Референс загружен: {ref_audio_path} ({len(ref_text)} символов транскрипции)")
        return True

    def synthesize(self, text: str) -> Optional[tuple[np.ndarray, int]]:
        """
        Синтез речи голосом Бурунова.
        Возвращает (audio_float32, sample_rate) или None.

        Для длинных текстов дробит на чанки (F5-TTS плохо работает с >15 сек).
        """
        if not self._ready or not self._ref_loaded:
            log.error("F5-TTS не готова (модель или референс не загружены)")
            return None

        # Дробим длинный текст на чанки по предложениям
        chunks = self._split_text(text, max_chars=180)
        log.info(f"Синтез {len(chunks)} чанков для текста: {text[:80]}...")

        all_audio = []
        sr = 24000  # F5-TTS по умолчанию 24kHz, потом ресэмплинг в 16kHz
        for i, chunk in enumerate(chunks):
            try:
                t0 = time.time()
                # TODO_F5: реальный вызов
                # audio, sr = self.model.infer(
                #     ref_file=self.ref_audio_path,
                #     ref_text=self.ref_text,
                #     gen_text=chunk,
                # )
                # STUB: тишина нужной длины (для теста API без модели)
                duration_s = max(1.0, len(chunk) / 15.0)  # ~15 символов в секунду
                audio = np.zeros(int(sr * duration_s), dtype=np.float32)
                rtf = (time.time() - t0) / duration_s
                log.info(f"chunk {i+1}/{len(chunks)}: {len(chunk)} chars, {duration_s:.1f}s audio, RTF={rtf:.2f}")
                all_audio.append(audio)
                # Пауза между чанками
                all_audio.append(np.zeros(int(sr * 0.2), dtype=np.float32))
            except Exception as e:
                log.error(f"chunk {i+1} failed: {e}")
                return None

        if not all_audio:
            return None
        full = np.concatenate(all_audio)
        return full, sr

    def _split_text(self, text: str, max_chars: int = 180) -> list[str]:
        """
        Разбить текст на чанки по предложениям.
        F5-TTS плохо работает с длинными текстами, надо дробить.
        """
        if len(text) <= max_chars:
            return [text]

        # Простая разбивка по . ! ? с сохранением знака
        import re
        sentences = re.split(r'(?<=[.!?…])\s+', text.strip())
        chunks = []
        current = ""
        for s in sentences:
            if not s:
                continue
            if len(current) + len(s) + 1 <= max_chars:
                current = (current + " " + s).strip()
            else:
                if current:
                    chunks.append(current)
                current = s
        if current:
            chunks.append(current)
        return chunks


# -----------------------------------------------------------------------------
# Утилиты конвертации аудио
# -----------------------------------------------------------------------------
def float32_to_pcm16(audio: np.ndarray) -> bytes:
    """Конвертировать float32 [-1, 1] в PCM 16-bit little-endian bytes."""
    audio = np.clip(audio, -1.0, 1.0)
    pcm = (audio * 32767).astype(np.int16)
    return pcm.tobytes()


def pcm16_to_wav(pcm: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Обернуть PCM 16-bit в WAV заголовок."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit = 2 bytes
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def resample_linear(audio: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    """Простой линейный ресэмплинг. Для качества лучше использовать librosa/soxr."""
    if sr_from == sr_to:
        return audio
    n_out = int(len(audio) * sr_to / sr_from)
    indices = np.linspace(0, len(audio) - 1, n_out)
    return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


# -----------------------------------------------------------------------------
# FastAPI сервер
# -----------------------------------------------------------------------------
def run_server(port: int, ref_audio: str, ref_text: str, model_name: str, device: str):
    try:
        from fastapi import FastAPI, HTTPException
        from pydantic import BaseModel
        import uvicorn
    except ImportError:
        log.error("pip install fastapi uvicorn pydantic")
        return

    app = FastAPI(title="F5-TTS Burunov Server", version="1.0")
    tts = F5TTSWrapper(model_name=model_name, device=device)

    class SynthRequest(BaseModel):
        text: str
        speed: float = 0.9  # 0.9 = чуть медленнее (лениво), как в спецификации Бурунова

    @app.on_event("startup")
    def _startup():
        ok = tts.load_model()
        if ok:
            tts.load_reference(ref_audio, ref_text)
        log.info(f"F5-TTS ready: model={ok}, ref={tts._ref_loaded}")

    @app.get("/health")
    def health():
        return {
            "ready": tts._ready,
            "ref_loaded": tts._ref_loaded,
            "model": tts.model_name,
            "device": tts.device,
            "ref_audio": tts.ref_audio_path,
        }

    @app.get("/info")
    def info():
        return {
            "model": tts.model_name,
            "huggingface": "https://huggingface.co/hotstone228/F5-TTS-Russian",
            "device": tts.device,
            "note": "Zero-shot voice cloning. Requires ref_audio + ref_text of Burunov.",
        }

    @app.post("/synthesize_pcm")
    def synth_pcm(req: SynthRequest):
        """Возвращает PCM 16kHz mono 16-bit (без заголовка) — для AudioClient.PlayStream."""
        if not tts._ready:
            raise HTTPException(503, "F5-TTS model not loaded")
        if not tts._ref_loaded:
            raise HTTPException(503, "Reference audio not loaded")

        result = tts.synthesize(req.text)
        if result is None:
            raise HTTPException(500, "synthesis failed")
        audio, sr = result

        # Ресэмплинг в 16kHz (G1 ожидает 16kHz)
        if sr != 16000:
            audio = resample_linear(audio, sr, 16000)

        # Регулировка скорости (простой time-stretch через ресэмплинг)
        if req.speed != 1.0 and req.speed > 0:
            target_sr = int(16000 / req.speed)
            audio = resample_linear(audio, 16000, target_sr)
            audio = resample_linear(audio, target_sr, 16000)

        pcm = float32_to_pcm16(audio)
        return pcm

    @app.post("/synthesize_wav")
    def synth_wav(req: SynthRequest):
        """Возвращает WAV 16kHz mono 16-bit (для тестов/пресетов)."""
        if not tts._ready or not tts._ref_loaded:
            raise HTTPException(503, "not ready")
        result = tts.synthesize(req.text)
        if result is None:
            raise HTTPException(500, "synthesis failed")
        audio, sr = result
        if sr != 16000:
            audio = resample_linear(audio, sr, 16000)
        pcm = float32_to_pcm16(audio)
        wav = pcm16_to_wav(pcm, sample_rate=16000, channels=1)
        return wav

    log.info(f"Starting F5-TTS server on :{port}")
    log.info(f"  model: {model_name}")
    log.info(f"  device: {device}")
    log.info(f"  ref_audio: {ref_audio}")
    uvicorn.run(app, host="0.0.0.0", port=port)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8001)
    p.add_argument("--ref-audio", required=False, default="/home/unitree/burunov-bot/data/burunov_ref.wav",
                   help="WAV 5-15 сек чистого голоса Бурунова")
    p.add_argument("--ref-text", required=False,
                   default="Привет, я Сергей Бурунов, и сейчас расскажу вам одну историю.",
                   help="Точная транскрипция референс аудио")
    p.add_argument("--model", default="hotstone228/F5-TTS-Russian")
    p.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    args = p.parse_args()

    run_server(args.port, args.ref_audio, args.ref_text, args.model, args.device)


if __name__ == "__main__":
    main()
