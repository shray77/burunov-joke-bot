"""
unitree_gestures.py
───────────────────
Обёртка над unitree_sdk2 LocoClient для синхронизации жестов с речью.

LocoClient — это Sport Service Interface из официальной доки.
Методы (из доки Sport Services Interface):
  - Start()               — вход в main operation control
  - StandUp()             — встать
  - Sit()                 — сесть
  - Squat()               — присесть
  - BalanceStand()        — балансировка стоя
  - Move(vx, vy, vyaw)    — движение (для интерактива)
  - StopMove()            — стоп движения
  - Damp()                — демпфирование
  - ZeroTorque()          — нулевой момент
  - ContinuousGait(flag)  — постоянная ходьба

Жесты для нашего проекта ("Бурунов рассказывает анекдот"):
  - idle       — StandUp, руки вниз
  - talking    — лёгкое покачивание корпуса (Move с малой vyaw)
  - thinking   — Move head (через AX-12 если есть)
  - punchline  — резкий взмах (через руки)
  - laugh      — Sit → StandUp
  - bow        — наклон корпуса (через тазобедренный)

ВАЖНО: Робот двигается! Все вызовы должны быть безопасными.
Не делай Move с большой скоростью — G1 может упасть.

────────────────────────────────────────────────────────────────────────
Требования к прошивке:
  Sport Service: входит в базовую прошивку G1
  Motion Switcher: для переключения режимов
"""
import time
import threading
from typing import Optional

import config


# ─── Безопасные параметры движения ────────────────────────────────────
SAFE_VYAW_TALKING = 0.05    # рад/с, лёгкое покачивание
SAFE_VX = 0.0
SAFE_VY = 0.0
GESTURE_TRANSITION_PAUSE = 0.5  # сек между сменами поз


class LocoController:
    """
    Управление движениями G1 через LocoClient.

    Silent fallback если SDK/робот недоступен.
    """

    def __init__(self, network_interface: str = "eth0", enable: bool = True):
        self.network_interface = network_interface
        self.enable = enable
        self._loco = None
        self._available = False
        self._current_pose = "idle"
        self._talking_thread = None
        self._stop_talking = threading.Event()

        if not enable:
            print("[gestures] DISABLED (config)")
            return

        try:
            self._init_sdk()
            self._available = True
            print(f"[gestures] OK, LocoClient loaded on {network_interface}")
        except Exception as e:
            print(f"[gestures] SDK/робот недоступны: {e}")
            print("[gestures] Работаем в silent-режиме.")
            self._available = False

    def _init_sdk(self):
        from unitree_sdk2py.core.channel import ChannelFactory
        from unitree_sdk2py.g1.loco.loco_client import LocoClient

        # ChannelFactory уже инициализирован в AudioClient — реинициализация
        # в unitree_sdk2 безопасна (singleton)
        ChannelFactory.Initialize(0, self.network_interface)

        self._loco = LocoClient()
        self._loco.Init()
        self._loco.SetTimeout(10.0)

    @property
    def available(self) -> bool:
        return self._available

    # ─── Базовые позы ────────────────────────────────────────────────

    def start(self) -> bool:
        """Войти в main operation control. Первая команда после включения."""
        if not self._available:
            return True
        ret = self._loco.Start()
        if ret == 0:
            print("[gestures] Start OK")
            time.sleep(GESTURE_TRANSITION_PAUSE)
            return True
        return False

    def stand_up(self) -> bool:
        if not self._available:
            return True
        ret = self._loco.StandUp()
        if ret == 0:
            self._current_pose = "standing"
            time.sleep(GESTURE_TRANSITION_PAUSE)
            return True
        return False

    def sit(self) -> bool:
        if not self._available:
            return True
        ret = self._loco.Sit()
        if ret == 0:
            self._current_pose = "sitting"
            time.sleep(GESTURE_TRANSITION_PAUSE)
            return True
        return False

    def squat(self) -> bool:
        if not self._available:
            return True
        ret = self._loco.Squat()
        if ret == 0:
            self._current_pose = "squatting"
            time.sleep(GESTURE_TRANSITION_PAUSE)
            return True
        return False

    def balance_stand(self) -> bool:
        """Балансировка стоя — рекомендованная поза для разговоров."""
        if not self._available:
            return True
        ret = self._loco.BalanceStand()
        if ret == 0:
            self._current_pose = "balance_stand"
            time.sleep(GESTURE_TRANSITION_PAUSE)
            return True
        return False

    # ─── Движение ────────────────────────────────────────────────────

    def move(self, vx: float = 0.0, vy: float = 0.0, vyaw: float = 0.0,
              continuous: bool = False) -> bool:
        """
        Скорость: vx вперёд, vy вбок, vyaw поворот.
        Безопасные значения: |v| < 0.3, |vyaw| < 0.5
        """
        if not self._available:
            return True
        ret = self._loco.Move(vx, vy, vyaw)
        return ret == 0

    def stop_move(self) -> bool:
        if not self._available:
            return True
        ret = self._loco.StopMove()
        return ret == 0

    # ─── Синхронизация с речью ───────────────────────────────────────

    def start_talking_sway(self):
        """
        Запускает лёгкое покачивание корпуса во время речи.
        Асинхронное — возвращает сразу.
        """
        if not self._available:
            return
        self._stop_talking.clear()
        self._talking_thread = threading.Thread(
            target=self._sway_loop, daemon=True
        )
        self._talking_thread.start()
        print("[gestures] → talking (sway started)")

    def stop_talking_sway(self):
        """Остановить покачивание."""
        self._stop_talking.set()
        if self._talking_thread and self._talking_thread.is_alive():
            self._talking_thread.join(timeout=2.0)
        if self._available:
            self.stop_move()
        print("[gestures] → idle (sway stopped)")

    def _sway_loop(self):
        """Лёгкое покачивание: поворот влево-вправо."""
        direction = 1
        while not self._stop_talking.is_set():
            self.move(vx=0, vy=0, vyaw=direction * SAFE_VYAW_TALKING)
            direction *= -1
            self._stop_talking.wait(2.0)  # меняется каждые 2 сек
        self.stop_move()

    def punchline_gesture(self):
        """Короткий жест при панчлайне — лёгкий поворот."""
        if not self._available:
            return
        print("[gestures] → punchline")
        # Останавливаем sway
        self._stop_talking.set()
        if self._talking_thread and self._talking_thread.is_alive():
            self._talking_thread.join(timeout=1.0)
        # Резкий маленький поворот
        self.move(vyaw=0.15)
        time.sleep(0.3)
        self.move(vyaw=-0.15)
        time.sleep(0.3)
        self.stop_move()

    def laugh_gesture(self):
        """Жест "смеётся" — присесть-встать."""
        if not self._available:
            return
        print("[gestures] → laugh")
        self.squat()
        time.sleep(0.5)
        self.stand_up()

    def bow_gesture(self):
        """Лёгкий поклон — наклон через тазобедренный."""
        # На G1 нет прямого API для наклона корпуса через LocoClient,
        # но через low-level motor control можно. Для безопасности хакатона
        # делаем через Squat → StandUp
        if not self._available:
            return
        print("[gestures] → bow")
        self.squat()
        time.sleep(0.8)
        self.stand_up()

    def thinking_gesture(self):
        """Жест "думает" — лёгкое движение головы влево-вправо."""
        # Через тазобедренный сустав
        if not self._available:
            return
        print("[gestures] → thinking")
        self.move(vyaw=0.1)
        time.sleep(0.5)
        self.move(vyaw=-0.1)
        time.sleep(0.5)
        self.stop_move()

    @property
    def current_pose(self) -> str:
        return self._current_pose

    def emergency_stop(self):
        """Аварийная остановка — Damp режим."""
        if not self._available:
            return
        self._stop_talking.set()
        self._loco.StopMove()
        self._loco.Damp()
        print("[gestures] EMERGENCY STOP (Damp mode)")


# ─── Высокоуровневый оркестратор жестов ──────────────────────────────
class GestureOrchestrator:
    """
    Связывает жесты с этапами анекдота:
      1. thinking — перед началом ("хм, что-то вспомню из 86-го...")
      2. talking  — основной рассказ, лёгкое покачивание
      3. punchline — резкий жест на ключевой фразе
      4. laugh    — после панчлайна
      5. idle     — отдых
    """

    def __init__(self, loco: LocoController):
        self.loco = loco

    def prepare(self):
        """Подготовка к демо — встать и балансировать."""
        if not self.loco.available:
            return
        self.loco.start()
        self.loco.stand_up()
        self.loco.balance_stand()

    def before_joke(self):
        """Жест перед рассказом анекдота."""
        self.loco.thinking_gesture()

    def start_telling(self):
        """Начать рассказ — лёгкое покачивание."""
        self.loco.start_talking_sway()

    def on_punchline(self):
        """Панчлайн — резкий жест."""
        self.loco.punchline_gesture()

    def after_joke(self):
        """После анекдота — присесть от смеха."""
        self.loco.stop_talking_sway()
        self.loco.laugh_gesture()

    def idle(self):
        """Вернуться в спокойное состояние."""
        self.loco.stop_talking_sway()
        if self.loco.available:
            self.loco.balance_stand()


# ─── Тест ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== Тест LocoController ===\n")
    loco = LocoController(network_interface=config.G1_NETWORK_INTERFACE)

    print(f"\navailable: {loco.available}")

    # Базовые позы
    print("\n--- Базовые позы ---")
    loco.prepare()
    time.sleep(2)

    # Жесты
    print("\n--- Жесты ---")
    orch = GestureOrchestrator(loco)

    print("\n1. Before joke (thinking)")
    orch.before_joke()
    time.sleep(2)

    print("\n2. Start telling (sway)")
    orch.start_telling()
    time.sleep(5)

    print("\n3. Punchline")
    orch.on_punchline()
    time.sleep(1)

    print("\n4. After joke (laugh)")
    orch.after_joke()
    time.sleep(2)

    print("\n5. Idle")
    orch.idle()

    print("\n=== Тест завершён ===")
