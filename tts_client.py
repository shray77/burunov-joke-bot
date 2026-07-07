"""
tts_client.py
─────────────
Клиент: берёт текст из RAG-пайплайна → озвучивает через TTS-сервер →
либо сохраняет wav, либо играет через локальный аудиопоток,
либо (для демо) стримит на внешнее устройство.

Для real-time на Unitree G1 смотри функцию stream_to_speaker().
"""
import io
import time
from pathlib import Path

import httpx
import pyaudio


TTS_HOST = "http://localhost:8001"
RAG_HOST = "http://localhost:8000"

# Параметры воспроизведения (GPT-SoVITS отдаёт 16kHz mono PCM_16)
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2   # 16-bit = 2 bytes


def tell_and_save(topic: str, out_path: str) -> Path:
    """
    Тема → RAG → TTS → сохраняет wav в out_path.
    Самый простой сценарий, например для предгенерации демо.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"[1/2] RAG: тема «{topic}»")
    with httpx.Client(timeout=120.0) as client:
        rag = client.post(f"{RAG_HOST}/tell", json={"topic": topic})
        rag.raise_for_status()
        text = rag.json()["text"]
        print(f"  текст: {text[:80]}...")

        print(f"[2/2] TTS: синтезирую голос...")
        wav = client.post(f"{TTS_HOST}/synthesize", json={"text": text})

    out.write_bytes(wav.content)
    print(f"  сохранено: {out} ({len(wav.content)} bytes)")
    return out


def tell_and_play(topic: str) -> None:
    """
    Тема → RAG → TTS → стрим-воспроизведение через pyaudio.
    Real-time режим для демо.
    """
    print(f"[1/2] RAG: тема «{topic}»")
    with httpx.Client(timeout=120.0) as client:
        rag = client.post(f"{RAG_HOST}/tell", json={"topic": topic})
        rag.raise_for_status()
        text = rag.json()["text"]
        print(f"  текст: {text[:80]}...")

        print(f"[2/2] TTS: стримю на динамик...")
        # Используем /stream — сервер бьёт по предложениям
        with client.stream("POST", f"{TTS_HOST}/stream",
                          json={"text": text}, timeout=120.0) as resp:
            resp.raise_for_status()
            play_wav_stream(resp.iter_bytes())


def play_wav_stream(chunks):
    """
    Принимает чанки wav-байтов (каждый чанк = полный wav одного предложения),
    парсит wav-header, играет через pyaudio.
    """
    import wave

    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        output=True,
    )

    try:
        for chunk in chunks:
            if not chunk:
                continue
            # В каждом чанке есть wav-заголовок — пропускаем его
            # (44 байта стандартный header)
            # Простая эвристика: ищем "data" chunk
            data_start = chunk.find(b"data")
            if data_start == -1:
                continue
            # +4 байта "data" + 4 байта длина = сами данные
            audio_data = chunk[data_start + 8:]
            if audio_data:
                stream.write(audio_data)
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


def stream_to_speaker(topic: str, speaker_device: str | None = None) -> None:
    """
    То же что tell_and_play, но вывод на конкретное аудиоустройство.
    Для Unitree G1: укажи устройство-динамик робота (или Bluetooth).

    Узнать список устройств:
      python -c "import pyaudio; p=pyaudio.PyAudio(); [print(i, p.get_device_info_by_index(i)['name']) for i in range(p.get_device_count())]"
    """
    # Аналогично tell_and_play, но pyaudio открываем с output_device_index
    # Здесь оставлен каркас — допили под свой G1 SDK/аудиопоток.
    raise NotImplementedError(
        "Допиши под конкретный способ вывода на G1: "
        "Unitree SDK stream / Bluetooth-колонка / pulseaudio sink."
    )


# ─── CLI ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    topic = " ".join(sys.argv[1:]) or "Штирлиц и Мюллер"

    mode = "play"   # play | save
    if "--save" in sys.argv:
        mode = "save"
        topic = " ".join(a for a in sys.argv[1:] if a != "--save")

    if mode == "save":
        out = tell_and_save(topic, f"output/{topic[:30]}.wav")
        print(f"\nСохранил: {out}")
    else:
        tell_and_play(topic)
