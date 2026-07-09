"""
unitree_audio.py
────────────────
Обёртка над unitree_sdk2 AudioClient для G1.

Методы (из официальной доки VuiClient Service Interface):
  - PlayStream(app_name, stream_id, pcm_data)  — стрим аудио на динамик Stanley
  - PlayStop(app_name)                         — стоп стрима
  - SetVolume(0-100)                           — громкость
  - GetVolume() -> int                         — текущая громкость
  - LedControl(R, G, B)                        — RGB-лента (256 цветов)
  - TtsMaker(text, speaker_id)                 — встроенный TTS (CN/EN)

Формат PCM для PlayStream (КРИТИЧНО):
  - PCM без заголовка (raw)
  - 16000 Hz sample rate
  - 1 канал (моно)
  - 16-bit signed

Требования к прошивке (из доки):
  Vui_Service  >= 2.0.3.8
  Vui Module   >= 2.0.0.3
  Vul Service  >= 2.0.4.4
  Webrtc Bridge >= 1.0.7.5
  Audio Hub    >= 1.0.1.0

────────────────────────────────────────────────────────────────────────
Сеть: робот доступен через multicast 239.168.123.161:5555
Сетевой интерфейс на хосте: обычно eth0 (если кабель) или usb0 (USB-tether)
IP робота: 192.168.123.161 (по умолчанию)

Если unitree_sdk2_python не установлен — работает в silent-режиме
(можно тестировать код на ноуте без робота).
"""
import time
import threading
import struct
from pathlib import Path
from typing import Optional

import config


class AudioClient:
    """
    Обёртка над unitree_sdk2 AudioClient.

    Silent fallback если SDK/робот недоступны — все методы возвращают
    success=True но ничего не делают. Это позволяет тестировать код
    без реального робота.
    """

    def __init__(self, network_interface: str = "eth0", enable: bool = True):
        self.network_interface = network_interface
        self.enable = enable
        self._sdk_client = None
        self._available = False

        if not enable:
            print("[audio] DISABLED (config)")
            return

        try:
            self._init_sdk()
            self._available = True
            print(f"[audio] OK, AudioClient loaded on {network_interface}")
        except Exception as e:
            print(f"[audio] SDK/робот недоступны: {e}")
            print("[audio] Работаем в silent-режиме (для тестов без G1).")
            self._available = False

    def _init_sdk(self):
        """Инициализация unitree_sdk2 AudioClient."""
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.g1.audio.audio_client import AudioClient as SDKAudioClient

        # Инициализация DDS-канала
        ChannelFactoryInitialize(0, self.network_interface)
        # SetNetworkLevel НЕ подтверждён — не встречается ни в одном реально
        # проверенном на роботе файле (coffee_delivery.py, g1_cup_solution_package).
        # Похоже этот файл (unitree_audio.py) вообще не используется —
        # coffee_delivery.py несёт свою встроенную G1Audio с проверенными
        # импортами (g1.audio.g1_audio_client, НЕ g1.audio.audio_client как
        # тут строкой выше). Если что-то реально вызывает этот класс — сначала
        # проверить оба несоответствия, не просто убрать строку.

        self._sdk_client = SDKAudioClient()
        self._sdk_client.Init()
        self._sdk_client.SetTimeout(10.0)

    @property
    def available(self) -> bool:
        return self._available

    # ─── Базовые методы ──────────────────────────────────────────────

    def set_volume(self, volume: int) -> bool:
        """Громкость 0-100. Дока говорит: ставь 100 для максимальной."""
        if not self._available:
            return True
        volume = max(0, min(100, int(volume)))
        ret = self._sdk_client.SetVolume(volume)
        if ret == 0:
            print(f"[audio] volume = {volume}%")
            return True
        print(f"[audio] SetVolume failed: ret={ret}")
        return False

    def get_volume(self) -> Optional[int]:
        if not self._available:
            return None
        # Python SDK может иметь другой API — зависит от версии
        try:
            vol = self._sdk_client.GetVolume()
            return int(vol) if isinstance(vol, (int, float)) else None
        except Exception:
            return None

    def led_control(self, r: int, g: int, b: int) -> bool:
        """
        RGB-лента на голове G1. 256 цветов.
        ВАЖНО: интервал между вызовами > 200ms (из доки).
        """
        if not self._available:
            return True
        r, g, b = max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))
        ret = self._sdk_client.LedControl(r, g, b)
        return ret == 0

    def tts_maker(self, text: str, speaker_id: int = 0) -> bool:
        """
        Встроенный TTS робота.
        speaker_id: 0 = китайский, 1 = английский.
        Русский НЕ поддерживается — это только fallback.
        """
        if not self._available:
            return True
        ret = self._sdk_client.TtsMaker(text, speaker_id)
        return ret == 0

    # ─── PlayStream — главное для нас ────────────────────────────────

    def play_stream(self, app_name: str, pcm_data: bytes,
                     stream_id: str = None) -> bool:
        """
        Стримит PCM 16kHz mono 16-bit на динамик робота.

        Параметры (из доки):
          app_name:  идентификатор приложения (например "burunov_bot")
          stream_id: одинаковый ID = продолжение воспроизведения из кэша,
                     разный ID = прерывание текущего воспроизведения
          pcm_data:  raw PCM bytes (16kHz, 1ch, 16-bit)

        Возвращает True при успехе.
        """
        if not self._available:
            # Имитация задержки для тестов
            time.sleep(len(pcm_data) / (16000 * 2) * 0.1)
            return True

        if stream_id is None:
            stream_id = str(int(time.time() * 1000))

        try:
            # Python SDK может принимать list[int] или bytes — зависит от версии
            # Пробуем оба варианта
            try:
                ret = self._sdk_client.PlayStream(app_name, stream_id, pcm_data)
            except (TypeError, AttributeError):
                # Конвертируем bytes → list[int16]
                samples = list(struct.unpack(f"<{len(pcm_data)//2}h", pcm_data))
                ret = self._sdk_client.PlayStream(app_name, stream_id, samples)
            return ret == 0
        except Exception as e:
            print(f"[audio] PlayStream error: {e}")
            return False

    def play_stop(self, app_name: str) -> bool:
        """Остановить воспроизведение."""
        if not self._available:
            return True
        ret = self._sdk_client.PlayStop(app_name)
        return ret == 0

    # ─── Утилиты для воспроизведения WAV ─────────────────────────────

    def play_wav_file(self, wav_path: str | Path,
                       app_name: str = "burunov_bot",
                       chunk_duration_sec: float = 1.0) -> bool:
        """
        Читает WAV файл (16kHz mono 16-bit) и стримит на робота чанками.

        Чанкинг нужен чтобы:
          1. Не упереться в лимит размера одного PCM-сообщения
          2. Иметь возможность прервать воспроизведение
          3. Синхронизировать жесты с речью по таймингам

        chunk_duration_sec: длительность одного чанка (1.0 сек = 32KB)
        """
        wav_path = Path(wav_path)
        if not wav_path.exists():
            print(f"[audio] файл не найден: {wav_path}")
            return False

        pcm_data, sample_rate, num_channels = self._read_wav(wav_path)
        if pcm_data is None:
            return False

        if sample_rate != 16000 or num_channels != 1:
            print(f"[audio] формат не подходит: {sample_rate}Hz {num_channels}ch")
            print("[audio] нужно: 16000Hz mono. Конвертируй ffmpeg-ом.")
            return False

        # Бьём на чанки
        chunk_size = int(16000 * 2 * chunk_duration_sec)
        total_chunks = (len(pcm_data) + chunk_size - 1) // chunk_size
        print(f"[audio] стримю {len(pcm_data)} байт ({len(pcm_data)/32000:.1f} сек), "
              f"{total_chunks} чанков по {chunk_duration_sec}с")

        stream_id = str(int(time.time() * 1000))

        for i in range(0, len(pcm_data), chunk_size):
            chunk = pcm_data[i:i + chunk_size]
            if not self.play_stream(app_name, chunk, stream_id):
                print(f"[audio] ошибка на чанке {i//chunk_size}")
                return False
            # Небольшая пауза чтобы робот успел доиграть чанк
            time.sleep(chunk_duration_sec * 0.95)

        return True

    @staticmethod
    def _read_wav(path: Path) -> tuple[Optional[bytes], int, int]:
        """Читает WAV, возвращает (pcm_bytes, sample_rate, num_channels)."""
        try:
            import wave
            with wave.open(str(path), "rb") as wav:
                sample_rate = wav.getframerate()
                num_channels = wav.getnchannels()
                sample_width = wav.getsampwidth()
                if sample_width != 2:
                    print(f"[audio] WAV sample_width={sample_width}, нужно 2 (16-bit)")
                    return None, 0, 0
                pcm = wav.readframes(wav.getnframes())
                return pcm, sample_rate, num_channels
        except Exception as e:
            print(f"[audio] read_wav error: {e}")
            return None, 0, 0


# ─── Контекстный менеджер для LED в такт речи ────────────────────────
class LedPulse:
    """
    Меняет цвет RGB-ленты G1 в такт речи.
    Запускается в отдельном потоке, плавно "дышит" синим во время речи,
    зелёным при панчлайне, красным при ошибке.

    Использование:
        with LedPulse(audio_client, color=(0, 0, 255)):
            audio_client.play_wav_file("joke.wav")
    """
    def __init__(self, audio: AudioClient, color=(0, 0, 255), interval: float = 0.5):
        self.audio = audio
        self.base_color = color
        self.interval = max(0.25, interval)  # мин 0.25 (т.к. дока требует >200ms)
        self._stop = threading.Event()
        self._thread = None
        self._override_color = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._pulse, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        # Гасим ленту
        if self.audio.available:
            self.audio.led_control(0, 0, 0)

    def set_color(self, r, g, b):
        """Сменить цвет "на лету" — например, для панчлайна."""
        self._override_color = (r, g, b)

    def _pulse(self):
        """Плавное "дыхание" яркостью."""
        phase = 0
        while not self._stop.is_set():
            color = self._override_color or self.base_color
            # Косинусоида для плавности
            brightness = 0.5 + 0.5 * (1 + __import__('math').cos(phase)) / 2
            r = int(color[0] * brightness)
            g = int(color[1] * brightness)
            b = int(color[2] * brightness)
            if self.audio.available:
                self.audio.led_control(r, g, b)
            phase += 0.6
            self._stop.wait(self.interval)


# ─── Тест из консоли ─────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    print("=== Тест AudioClient ===")
    print(f"network_interface: {config.G1_NETWORK_INTERFACE}")
    print(f"silent_mode (если SDK нет): включается автоматически\n")

    audio = AudioClient(network_interface=config.G1_NETWORK_INTERFACE)

    print(f"\navailable: {audio.available}")
    print(f"volume: {audio.get_volume()}")

    # Тест LED
    print("\n--- LED тест (синий → зелёный → красный) ---")
    audio.led_control(0, 0, 255)
    time.sleep(1)
    audio.led_control(0, 255, 0)
    time.sleep(1)
    audio.led_control(255, 0, 0)
    time.sleep(1)
    audio.led_control(0, 0, 0)

    # Тест громкости
    print("\n--- Volume тест ---")
    audio.set_volume(80)
    time.sleep(0.5)
    audio.set_volume(100)

    # Тест PlayStream с тестовым тоном (если есть numpy)
    print("\n--- PlayStream тест (1 сек тишины) ---")
    try:
        import numpy as np
        # 1 сек тишины в PCM 16-bit
        silence = np.zeros(16000, dtype=np.int16).tobytes()
        audio.play_stream("test_app", silence)
        print("  PlayStream вызван (тишина)")
    except ImportError:
        print("  numpy не установлен, пропускаем")

    # Тест WAV (если есть файл)
    if len(sys.argv) > 1:
        wav_path = sys.argv[1]
        print(f"\n--- PlayWav тест: {wav_path} ---")
        audio.play_wav_file(wav_path)
    else:
        print("\n  (для теста WAV передай путь: python unitree_audio.py file.wav)")

    print("\n=== Тест завершён ===")
