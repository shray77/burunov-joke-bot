"""
robot_controller.py
───────────────────
Главный оркестратор на Unitree G1.

Поток:
  1. Получить тему (HTTP / UI / кнопка / микрофон)
  2. POST /tell на RAG (локальный :8000) → текст в стиле Бурунова
  3. POST /stream на TTS (:8001) → чанки wav
  4. Воспроизвести через pyaudio на USB/внешнем динамике
  5. Параллельно — жесты через unitree_gestures

────────────────────────────────────────────────────────────────────────
Запуск на G1:
  python robot_controller.py

Или как service:
  systemd unit / supervisor / nohup — см. EDGE_README.md
────────────────────────────────────────────────────────────────────────
"""
import io
import re
import time
import threading
from pathlib import Path

import httpx
import pyaudio

# ─── Конфиг ───────────────────────────────────────────────────────────
RAG_HOST = "http://localhost:8000"
TTS_HOST = "http://localhost:8001"

# Аудио-параметры Piper: 22050 Hz, моно, 16-bit
SAMPLE_RATE = 22050
CHANNELS = 1
SAMPLE_WIDTH = 2

# Индекс аудиоустройства для воспроизведения.
# None = системный default (обычно работает на G1 из коробки).
# Чтобы найти конкретный индекс USB-колонки:
#   python -c "import pyaudio; p=pyaudio.PyAudio(); [print(i, p.get_device_info_by_index(i)['name']) for i in range(p.get_device_count())]"
OUTPUT_DEVICE_INDEX = None

# Включить ли жесты (False если SDK нет или робот не подключён)
GESTURES_ENABLED = True

# Скорость речи Бурунова (1.0 = норма, 0.85 = медленно/лениво)
TTS_SPEED = 0.9

# Таймауты
RAG_TIMEOUT = 90.0    # Gemma на CPU может думать долго
TTS_TIMEOUT = 60.0


# ─── Главный класс ────────────────────────────────────────────────────
class RobotController:
    def __init__(self):
        self.client = httpx.Client(timeout=RAG_TIMEOUT)
        self.pa = pyaudio.PyAudio()
        self.audio_stream = None

        # Жесты
        if GESTURES_ENABLED:
            from unitree_gestures import GestureController, SpeechSync
            self.gestures = GestureController(enable=True)
            self.sync = SpeechSync(self.gestures)
        else:
            self.gestures = None
            self.sync = None

    def close(self):
        if self.audio_stream:
            self.audio_stream.stop_stream()
            self.audio_stream.close()
        self.pa.terminate()
        self.client.close()
        if self.gestures:
            self.gestures.stop()

    # ─── Поиск динамика ──────────────────────────────────────────────
    def list_output_devices(self):
        """Список доступных аудиоустройств для вывода."""
        print("\nДоступные аудиоустройства:")
        for i in range(self.pa.get_device_count()):
            info = self.pa.get_device_info_by_index(i)
            if info["maxOutputChannels"] > 0:
                print(f"  [{i}] {info['name']}  (out: {info['maxOutputChannels']}, sr: {int(info['defaultSampleRate'])})")

    def _open_stream(self):
        return self.pa.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            output=True,
            output_device_index=OUTPUT_DEVICE_INDEX,
        )

    # ─── Воспроизведение ─────────────────────────────────────────────
    def _play_wav_bytes(self, wav_bytes: bytes):
        """Играет один wav-файл (с заголовком)."""
        # Пропускаем WAV-заголовок (44 байта) и ищем "data" chunk
        data_start = wav_bytes.find(b"data")
        if data_start == -1:
            return
        audio_data = wav_bytes[data_start + 8:]
        if not audio_data:
            return

        if self.audio_stream is None:
            self.audio_stream = self._open_stream()
        self.audio_stream.write(audio_data)

    def _play_stream(self, response):
        """
        Стримит wav-чанки (каждый = отдельное предложение) на динамик.
        Параллельно запускает жест "говорит".
        """
        if self.audio_stream is None:
            self.audio_stream = self._open_stream()

        total_bytes = 0
        started_gesture = False

        for chunk in response.iter_bytes():
            if not chunk:
                continue
            # Запускаем жест при первом чанке
            if not started_gesture and self.sync:
                # Оцениваем длительность по размеру (~2.2 мин на байт при 22050/16/mono)
                est_duration = len(chunk) / (SAMPLE_RATE * SAMPLE_WIDTH)
                self.sync.start_talking(est_duration * 1.2)
                started_gesture = True

            # Извлекаем audio-данные (пропускаем wav-заголовок)
            data_start = chunk.find(b"data")
            if data_start == -1:
                continue
            audio_data = chunk[data_start + 8:]
            if audio_data:
                self.audio_stream.write(audio_data)
                total_bytes += len(audio_data)

        if self.sync:
            self.sync.end_talking()

        audio_sec = total_bytes / (SAMPLE_RATE * SAMPLE_WIDTH)
        print(f"  ▶ проиграно {audio_sec:.1f} сек аудио")

    # ─── Главная команда ─────────────────────────────────────────────
    def tell_joke(self, topic: str) -> dict:
        """
        Тема → анекдот Бурунова с голосом и жестами.
        """
        print(f"\n{'='*60}")
        print(f"ТЕМА: «{topic}»")
        print(f"{'='*60}")

        # 1. Жест "думает"
        if self.sync:
            print("🤔 (жест: думает)")
            self.sync.thinking(1.5)
            time.sleep(1.0)

        # 2. RAG: текст в стиле Бурунова
        print("📝 RAG: генерация текста...")
        t0 = time.time()
        try:
            rag_resp = self.client.post(
                f"{RAG_HOST}/tell",
                json={"topic": topic},
                timeout=RAG_TIMEOUT,
            )
            rag_resp.raise_for_status()
            rag_data = rag_resp.json()
        except Exception as e:
            print(f"❌ RAG failed: {e}")
            return {"error": "rag_failed", "detail": str(e)}

        text = rag_data.get("text", "")
        fallback = rag_data.get("fallback", False)
        sources_count = len(rag_data.get("sources", []))
        print(f"  RAG done in {time.time()-t0:.1f}s, sources: {sources_count}")
        print(f"  Текст: {text[:120]}{'...' if len(text) > 120 else ''}")
        print(f"  Fallback: {fallback}")

        if not text:
            return {"error": "empty_text"}

        # 3. TTS streaming + воспроизведение
        print("🎤 TTS: стримю синтез на динамик...")
        t0 = time.time()
        try:
            with self.client.stream(
                "POST",
                f"{TTS_HOST}/stream",
                json={"text": text, "speed": TTS_SPEED},
                timeout=TTS_TIMEOUT,
            ) as tts_resp:
                tts_resp.raise_for_status()
                self._play_stream(tts_resp)
        except Exception as e:
            print(f"❌ TTS/playback failed: {e}")
            return {"error": "tts_failed", "detail": str(e)}

        print(f"  ▶ done in {time.time()-t0:.1f}s")
        return {
            "topic": topic,
            "text": text,
            "sources": rag_data.get("sources", []),
            "fallback": fallback,
        }


# ─── CLI ──────────────────────────────────────────────────────────────
def main():
    import sys

    bot = RobotController()

    # Если передан --list-devices — показать аудиоустройства и выйти
    if "--list-devices" in sys.argv:
        bot.list_output_devices()
        bot.close()
        return

    # Тема из аргументов
    topic = " ".join(a for a in sys.argv[1:] if not a.startswith("--"))
    if not topic:
        topic = "Штирлиц и Мюллер"

    print(f"🤖 Burunov bot готов. Динамик: {OUTPUT_DEVICE_INDEX or 'default'}")
    print(f"   Жесты: {'вкл' if GESTURES_ENABLED else 'выкл'}")

    try:
        result = bot.tell_joke(topic)
        if "error" in result:
            print(f"\n❌ Ошибка: {result['error']}")
    finally:
        bot.close()


if __name__ == "__main__":
    main()
