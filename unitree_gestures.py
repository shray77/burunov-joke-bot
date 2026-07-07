"""
unitree_gestures.py
───────────────────
Обёртка над unitree_sdk2_python для синхронизации жестов с речью.

Если SDK не установлен или робот недоступен — всё работает,
просто без жестов (silent fallback). Это чтобы можно было
тестировать на ноуте без робота.

────────────────────────────────────────────────────────────────────────
Установка SDK (если не установлен на G1):
  git clone https://github.com/unitreerobotics/unitree_sdk2_python.git
  cd unitree_sdk2_python
  pip install -e .

Документация:
  https://support.unitree.com/home/en/G1_developer/about_G1
────────────────────────────────────────────────────────────────────────
"""
import time
import threading
from typing import Optional

# ─── Конфиг ───────────────────────────────────────────────────────────
ROBOT_NETWORK_INTERFACE = "eth0"   # сетевой интерфейс к роботу
# Если робот через USB- tether: "usb0"
# Если через WiFi: "wlan0"

# Порог громкости для "говорит" жеста (если используем mic)
SPEAK_VOLUME_THRESHOLD = 0.05

# Жесты (упрощённо — реальная реализация требует unitree_sdk2)
GESTURES = {
    "idle":           {"description": "Стоит, руки вниз"},
    "talking":        {"description": "Лёгкое движение руками в такт речи"},
    "thinking":       {"description": "Рука к подбородку"},
    "punchline":      {"description": "Резкий взмах при панчлайне"},
    "laugh":          {"description": "Голова назад, плечи поднимаются"},
}


class GestureController:
    """
    Управление жестами G1.

    Реальная реализация требует unitree_sdk2_python и знания
    конкретных Motor-IDs для рук/головы G1. Здесь — каркас,
    который безопасно работает без реального робота.
    """

    def __init__(self, enable: bool = True, interface: str = ROBOT_NETWORK_INTERFACE):
        self.enable = enable
        self.interface = interface
        self._sdk = None
        self._current = "idle"
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()

        if not enable:
            print("[gestures] DISABLED (config)")
            return

        try:
            self._init_sdk()
            print(f"[gestures] OK, SDK loaded on {interface}")
        except Exception as e:
            print(f"[gestures] SDK не доступен: {e}")
            print("[gestures] Работаем в silent-режиме (без жестов).")
            self.enable = False

    def _init_sdk(self):
        """Попытка инициализации unitree_sdk2."""
        from unitree_sdk2py.core.channel import ChannelFactory
        # Инициализация канала связи с роботом
        ChannelFactory.Initialize(0, self.interface)

        # Импорт motor-клиентов для рук
        # На G1: arm = 5 DOF × 2, hands (RH56DFTP) = 6 DOF × 2
        # Реальные Motor-IDs зависят от firmware — см. доки Unitree
        from unitree_sdk2py.gclient.loco.client import (
            RobotClient, data as robot_data,
        )
        self._sdk = RobotClient(self.interface)
        # Ставим в режим стояния
        self._sdk.stand_up()
        time.sleep(2)

    def play_gesture(self, name: str, duration: float = 2.0):
        """
        Запустить жест. Асинхронный — возвращает сразу.
        """
        if name not in GESTURES:
            print(f"[gestures] неизвестный жест: {name}")
            return

        if not self.enable:
            return

        # Останавливаем предыдущий поток
        self._stop_flag.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)

        self._stop_flag.clear()
        self._current = name
        self._thread = threading.Thread(
            target=self._run_gesture,
            args=(name, duration),
            daemon=True,
        )
        self._thread.start()

    def _run_gesture(self, name: str, duration: float):
        """Реальное выполнение жеста. Заглушка — просто логируем."""
        # TODO: реализовать реальные движения моторов через SDK
        # Например для "talking" — легкое покачивание руками 0.5 Гц
        start = time.time()
        while time.time() - start < duration and not self._stop_flag.is_set():
            # Реальная отправка команд моторам — зависит от SDK
            time.sleep(0.1)

    def stop(self):
        """Остановить все жесты, вернуть в idle."""
        self._stop_flag.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self.enable:
            self.play_gesture("idle", 0.5)

    def current(self) -> str:
        return self._current


# ─── Синхронизация с речью ────────────────────────────────────────────
class SpeechSync:
    """
    Синхронизирует жесты с воспроизведением аудио.

    Пример:
        sync = SpeechSync(gestures)
        sync.start("talking", audio_duration_sec=8.5)
        # ... воспроизводим аудио ...
        sync.end()  # вернёт в idle
    """

    def __init__(self, controller: GestureController):
        self.ctrl = controller

    def start_talking(self, duration_sec: float):
        """Начать жест "говорит" на указанную длительность."""
        self.ctrl.play_gesture("talking", duration=duration_sec)

    def end_talking(self):
        """Закончить говорить, вернуться в idle."""
        self.ctrl.stop()

    def punchline(self):
        """Короткий жест для панчлайна (0.8 сек)."""
        self.ctrl.play_gesture("punchline", duration=0.8)

    def thinking(self, duration: float = 1.5):
        """Жест "думает" перед началом шутки."""
        self.ctrl.play_gesture("thinking", duration=duration)


# ─── Тест из консоли ──────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    print("Тест GestureController (silent mode если нет SDK)...")
    ctrl = GestureController(enable=True)

    print("\nЖесты по очереди (2 сек каждый):")
    for name in GESTURES:
        print(f"  → {name}: {GESTURES[name]['description']}")
        ctrl.play_gesture(name, duration=2.0)
        time.sleep(2.0)

    print("\nТест SpeechSync...")
    sync = SpeechSync(ctrl)
    sync.thinking(1.5)
    time.sleep(1.5)
    sync.start_talking(3.0)
    time.sleep(3.0)
    sync.end_talking()
    print("Done.")
