"""
robot_controller.py
───────────────────
Главный оркестратор на Unitree G1.

Переписан под правильную архитектуру с unitree_sdk2:
  - RAG (текст) на отдельном сервисе
  - TTS (Piper ONNX) на отдельном сервисе или на самом G1
  - AudioClient.PlayStream() — воспроизведение через родной динамик G1
  - LocoClient — жесты в такт речи
  - HandClient — кисти Inspire RH56DFTP
  - LedControl — RGB-лента в такт речи

────────────────────────────────────────────────────────────────────────
Запуск:
  python robot_controller.py "Штирлиц"
  python robot_controller.py --interactive
  python robot_controller.py --http  (HTTP-сервер для управления с телефона)

Не требует pyaudio/USB-колонок — всё через unitree_sdk2.
"""
import io
import time
import threading
from pathlib import Path
from typing import Optional

import httpx

import config
from unitree_audio import AudioClient, LedPulse
from unitree_gestures import LocoController, GestureOrchestrator
from unitree_hands import DualHandController


# ─── Главный класс ────────────────────────────────────────────────────
class RobotController:
    """
    Оркестратор: тема → RAG → TTS → динамик G1 + жесты + LED.
    """

    def __init__(self):
        # HTTP-клиент к RAG и TTS сервисам
        self.http = httpx.Client(timeout=config.RAG_TIMEOUT)

        # Аудио (через unitree_sdk2 AudioClient)
        self.audio = AudioClient(
            network_interface=config.G1_NETWORK_INTERFACE,
            enable=config.G1_ENABLE_AUDIO,
        )

        # Жесты (через LocoClient)
        self.loco = LocoController(
            network_interface=config.G1_NETWORK_INTERFACE,
            enable=config.G1_ENABLE_GESTURES,
        )
        self.gestures = GestureOrchestrator(self.loco)

        # Кисти рук (RH56DFTP)
        self.hands = DualHandController(
            network_interface=config.G1_NETWORK_INTERFACE,
            left_type=config.G1_HAND_TYPE,
            right_type=config.G1_HAND_TYPE,
            enable=config.G1_ENABLE_HANDS,
        )

        # Подготовка
        self._init_robot()

    def _init_robot(self):
        """Подготовка робота к демо."""
        print("\n[init] Подготовка робота...")
        self.audio.set_volume(100)   # дока рекомендует 100%
        self.audio.led_control(0, 0, 50)  # тусклый синий = готов
        self.hands.relax_both()
        self.gestures.prepare()       # встать и балансировать
        self.audio.led_control(0, 50, 0)  # зелёный = готов
        print("[init] Готов.")

    def close(self):
        """Корректное завершение."""
        print("\n[close] Завершение...")
        try:
            self.gestures.idle()
            self.hands.relax_both()
            self.audio.led_control(0, 0, 0)
            self.audio.play_stop(config.G1_AUDIO_APP_NAME)
        finally:
            self.http.close()

    # ─── Главная команда ─────────────────────────────────────────────

    def tell_joke(self, topic: str) -> dict:
        """
        Тема → анекдот Бурунова с голосом + жесты + LED.
        """
        print(f"\n{'='*60}")
        print(f"ТЕМА: «{topic}»")
        print(f"{'='*60}")

        # 1. Жест "думает" + LED синий
        self.audio.led_control(0, 0, 255)
        self.gestures.before_joke()
        time.sleep(0.5)

        # 2. RAG: текст в стиле Бурунова
        print("📝 RAG: генерация текста...")
        t0 = time.time()
        try:
            rag_resp = self.http.post(
                f"{config.RAG_HOST}/tell",
                json={"topic": topic},
                timeout=config.RAG_TIMEOUT,
            )
            rag_resp.raise_for_status()
            rag_data = rag_resp.json()
        except Exception as e:
            print(f"❌ RAG failed: {e}")
            self._error_feedback()
            return {"error": "rag_failed", "detail": str(e)}

        text = rag_data.get("text", "")
        fallback = rag_data.get("fallback", False)
        sources_count = len(rag_data.get("sources", []))
        print(f"  RAG done in {time.time()-t0:.1f}s, sources: {sources_count}")
        print(f"  Текст: {text[:120]}{'...' if len(text) > 120 else ''}")

        if not text:
            self._error_feedback()
            return {"error": "empty_text"}

        # 3. TTS: синтез в PCM (16kHz mono 16-bit — родной формат G1)
        print("🎤 TTS: синтез...")
        t0 = time.time()
        try:
            tts_resp = self.http.post(
                f"{config.TTS_HOST}/synthesize_pcm",
                json={"text": text, "speed": config.TTS_SPEED},
                timeout=config.TTS_TIMEOUT,
            )
            tts_resp.raise_for_status()
            pcm_data = tts_resp.content
        except Exception as e:
            print(f"❌ TTS failed: {e}")
            self._error_feedback()
            return {"error": "tts_failed", "detail": str(e)}

        audio_sec = len(pcm_data) / (16000 * 2)
        print(f"  TTS done in {time.time()-t0:.1f}s, {audio_sec:.1f} сек аудио")

        # 4. Воспроизведение через AudioClient.PlayStream
        # Параллельно: жесты + LED
        print("🔊 Воспроизведение на G1...")
        self._play_with_gestures(pcm_data, text)

        return {
            "topic": topic,
            "text": text,
            "sources": rag_data.get("sources", []),
            "fallback": fallback,
            "audio_seconds": audio_sec,
        }

    def _play_with_gestures(self, pcm_data: bytes, text: str):
        """
        Воспроизводит PCM на роботе + синхронизирует жесты и LED.
        """
        # Запускаем жест "говорит"
        self.gestures.start_telling()

        # LED-пульсация синим во время речи
        with LedPulse(self.audio, color=(0, 0, 255), interval=0.4):
            # Стримим PCM чанками через PlayStream
            chunk_size = int(16000 * 2 * config.G1_AUDIO_CHUNK_SEC)
            stream_id = str(int(time.time() * 1000))
            total_chunks = (len(pcm_data) + chunk_size - 1) // chunk_size

            for i in range(0, len(pcm_data), chunk_size):
                chunk = pcm_data[i:i + chunk_size]
                if not self.audio.play_stream(
                    config.G1_AUDIO_APP_NAME, chunk, stream_id
                ):
                    print(f"  ⚠ ошибка на чанке {i//chunk_size}/{total_chunks}")
                    break
                # Ждём чтобы чанк доиграл (с небольшим запасом)
                time.sleep(config.G1_AUDIO_CHUNK_SEC * 0.95)

        # После речи — жест "смех"
        self.gestures.after_joke()

        # Зелёный = завершили
        self.audio.led_control(0, 50, 0)

    def _error_feedback(self):
        """Красная лента + отрицательный жест при ошибке."""
        self.audio.led_control(255, 0, 0)
        time.sleep(1.0)
        self.audio.led_control(0, 0, 0)

    # ─── Интерактивный режим ─────────────────────────────────────────

    def interactive_loop(self):
        """Чтение тем из stdin. Ввод = рассказать анекдот."""
        print("\n" + "="*60)
        print("🤖 Burunov Bot — интерактивный режим")
        print("="*60)
        print("Вводи тему анекдота, Enter — рассказать")
        print("Команды: 'exit' / 'quit' — выход, 'stop' — стоп аудио\n")

        while True:
            try:
                topic = input("\n📝 Тема > ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not topic:
                continue
            if topic.lower() in ("exit", "quit", "q"):
                break
            if topic.lower() == "stop":
                self.audio.play_stop(config.G1_AUDIO_APP_NAME)
                self.gestures.idle()
                continue

            try:
                self.tell_joke(topic)
            except Exception as e:
                print(f"❌ Ошибка: {e}")

    # ─── HTTP-режим для управления с телефона ────────────────────────

    def http_server(self, port: int = 8002):
        """Поднимает простую HTTP-ручку для управления с телефона."""
        from fastapi import FastAPI, HTTPException
        from pydantic import BaseModel
        import uvicorn

        app = FastAPI(title="Burunov Robot Controller")

        class TellReq(BaseModel):
            topic: str

        @app.get("/")
        def index():
            return {"status": "ok", "robot": "G1 Burunov"}

        @app.post("/tell")
        def tell(req: TellReq):
            try:
                result = self.tell_joke(req.topic)
                return result
            except Exception as e:
                raise HTTPException(500, str(e))

        @app.post("/stop")
        def stop():
            self.audio.play_stop(config.G1_AUDIO_APP_NAME)
            self.gestures.idle()
            return {"status": "stopped"}

        @app.get("/health")
        def health():
            return {
                "audio": self.audio.available,
                "gestures": self.loco.available,
                "hands": self.hands.available,
            }

        print(f"\n🌐 HTTP-сервер: http://0.0.0.0:{port}")
        print(f"   Управление: POST /tell {{\"topic\":\"Штирлиц\"}}")
        uvicorn.run(app, host="0.0.0.0", port=port)


# ─── CLI ──────────────────────────────────────────────────────────────
def main():
    import sys

    bot = RobotController()

    try:
        # Режимы запуска
        if "--http" in sys.argv:
            port = 8002
            for i, a in enumerate(sys.argv):
                if a == "--port" and i+1 < len(sys.argv):
                    port = int(sys.argv[i+1])
            bot.http_server(port)

        elif "--interactive" in sys.argv or "-i" in sys.argv:
            bot.interactive_loop()

        else:
            # Один анекдот из аргументов
            topic = " ".join(a for a in sys.argv[1:] if not a.startswith("--"))
            if not topic:
                topic = "Штирлиц и Мюллер"
            bot.tell_joke(topic)
    finally:
        bot.close()


if __name__ == "__main__":
    main()
