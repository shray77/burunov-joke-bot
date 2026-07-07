"""
unitree_hands.py
────────────────
Управление кистями рук Unitree G1.

В спеке робота: "кисти RH56DFTP (на две руки) с 12 суставами,
6 степенями свободы и интегрированными сенсорами усилия каждая"

Это Inspire RH56DFTP — поддерживается в доках Unitree как
"Inspire FTP Dexterity Hand".

────────────────────────────────────────────────────────────────────────
ВАЖНО: Руки у робота МЕНЯНЫЕ. Поддерживаются разные модели:
  - Dex3-1              (3-палая, 7 DOF)
  - Inspire FTP (RH56DFTP) — наш случай, 6 DOF
  - Inspire DFX         (новая версия)
  - Brainco Hand

Каждая модель имеет свой SDK. Этот файл реализует общую абстракцию
с авто-детекцией модели.
"""
import time
import threading
from typing import Optional

import config


# ─── Типы рук ────────────────────────────────────────────────────────
HAND_TYPES = {
    "RH56DFTP": "inspire_ftp",   # наш случай
    "RH56DFX": "inspire_dfx",
    "DEX3-1": "dex3",
    "BRAINCO": "brainco",
}

# Простые жесты для каждой руки (абстракция)
HAND_GESTURES = {
    "open":       "Открыть кисть",
    "close":      "Сжать кулак",
    "point":      "Указать пальцем",
    "thumbs_up":  "Большой палец вверх",
    "peace":      "V знак",
    "fist":       "Кулак (плотно)",
    "relaxed":    "Расслабленная кисть",
}


class HandController:
    """
    Управление одной кистью G1.

    Silent fallback если SDK/рука не подключены.
    """

    def __init__(self, side: str = "right",
                 hand_type: str = "RH56DFTP",
                 network_interface: str = "eth0",
                 enable: bool = True):
        """
        side: "left" | "right"
        hand_type: "RH56DFTP" | "RH56DFX" | "DEX3-1" | "BRAINCO"
        """
        self.side = side
        self.hand_type = hand_type
        self.network_interface = network_interface
        self.enable = enable
        self._hand_sdk = None
        self._available = False

        if hand_type not in HAND_TYPES:
            print(f"[hand:{side}] неизвестный тип руки: {hand_type}")
            self.enable = False
            return

        if not enable:
            print(f"[hand:{side}] DISABLED (config)")
            return

        try:
            self._init_sdk()
            self._available = True
            print(f"[hand:{side}] OK, {hand_type} loaded")
        except Exception as e:
            print(f"[hand:{side}] SDK/рука недоступны: {e}")
            print(f"[hand:{side}] Silent-режим.")
            self._available = False

    def _init_sdk(self):
        """Инициализация под конкретную модель руки."""
        from unitree_sdk2py.core.channel import ChannelFactory

        ChannelFactory.Initialize(0, self.network_interface)

        if self.hand_type == "RH56DFTP":
            # Inspire FTP dexterity hand
            from unitree_sdk2py.g1.hand.hand_client import HandClient
            self._hand_sdk = HandClient()
            self._hand_sdk.Init()
            self._hand_sdk.SetTimeout(10.0)

        elif self.hand_type == "DEX3-1":
            # Dex3-1 — другая модель, другой API
            try:
                from unitree_sdk2py.g1.hand.dex3_client import Dex3Client
                self._hand_sdk = Dex3Client()
                self._hand_sdk.Init()
            except ImportError:
                # На некоторых SDK версиях путь другой
                from unitree_sdk2py.g1.hand.hand_client import HandClient
                self._hand_sdk = HandClient()
                self._hand_sdk.Init()

        else:
            # Fallback — HandClient (если он универсальный)
            from unitree_sdk2py.g1.hand.hand_client import HandClient
            self._hand_sdk = HandClient()
            self._hand_sdk.Init()

    @property
    def available(self) -> bool:
        return self._available

    # ─── Жесты ───────────────────────────────────────────────────────

    def set_pose(self, gesture: str) -> bool:
        """
        Установить жест. Возможные значения — см. HAND_GESTURES.
        """
        if gesture not in HAND_GESTURES:
            print(f"[hand:{self.side}] неизвестный жест: {gesture}")
            return False

        if not self._available:
            return True

        try:
            # Конкретный API зависит от версии SDK.
            # В большинстве случаев есть SetHandPose или подобное.
            if self.hand_type == "RH56DFTP":
                # Inspire FTP — углы по 6 DOF
                angles = self._gesture_to_ftp_angles(gesture)
                # API: передаём список углов
                ret = self._hand_sdk.SetHandAngle(self.side, angles)
            else:
                # Универсальный API
                ret = self._hand_sdk.SetHandPose(self.side, gesture)
            return ret == 0
        except AttributeError:
            # Если API называется иначе — пробуем альтернативы
            try:
                ret = self._hand_sdk.SetPose(gesture)
                return ret == 0
            except Exception as e:
                print(f"[hand:{self.side}] SetPose failed: {e}")
                return False
        except Exception as e:
            print(f"[hand:{self.side}] set_pose error: {e}")
            return False

    def _gesture_to_ftp_angles(self, gesture: str) -> list[float]:
        """
        Конвертирует жест в углы Inspire RH56DFTP.
        6 DOF: thumb_oppose, thumb_flex, index_flex, middle_flex, ring_pinky_flex, wrist

        Значения в радианах. 0 = выпрямлено, π/2 = согнуто.
        """
        gestures_map = {
            "open":      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "close":     [1.5, 1.5, 1.5, 1.5, 1.5, 0.0],
            "fist":      [1.5, 1.5, 1.5, 1.5, 1.5, 0.0],
            "point":     [0.0, 1.5, 0.0, 1.5, 1.5, 0.0],
            "thumbs_up": [1.0, 0.0, 1.5, 1.5, 1.5, 0.0],
            "peace":     [0.0, 1.5, 0.0, 0.0, 1.5, 0.0],
            "relaxed":   [0.3, 0.3, 0.3, 0.3, 0.3, 0.0],
        }
        return gestures_map.get(gesture, gestures_map["relaxed"])

    def open_hand(self) -> bool:
        return self.set_pose("open")

    def close_hand(self) -> bool:
        return self.set_pose("close")

    def thumbs_up(self) -> bool:
        return self.set_pose("thumbs_up")

    def relax(self) -> bool:
        return self.set_pose("relaxed")


# ─── Оркестратор двух рук ────────────────────────────────────────────
class DualHandController:
    """Управление обеими кистями."""

    def __init__(self, network_interface: str = "eth0",
                 left_type: str = "RH56DFTP",
                 right_type: str = "RH56DFTP",
                 enable: bool = True):
        self.left = HandController(
            side="left", hand_type=left_type,
            network_interface=network_interface, enable=enable
        )
        self.right = HandController(
            side="right", hand_type=right_type,
            network_interface=network_interface, enable=enable
        )

    def set_pose_both(self, gesture: str):
        """Синхронно установить жест на обеих руках."""
        threads = []
        for hand in (self.left, self.right):
            t = threading.Thread(target=hand.set_pose, args=(gesture,))
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=2.0)

    def open_both(self):
        self.set_pose_both("open")

    def close_both(self):
        self.set_pose_both("close")

    def thumbs_up_both(self):
        self.set_pose_both("thumbs_up")

    def relax_both(self):
        self.set_pose_both("relaxed")

    def wave_right(self):
        """Помахать правой рукой — открыть + короткое движение."""
        self.right.open_hand()
        time.sleep(0.3)
        # Если доступен low-level motor — можно сделать реальное помахивание
        # через мотор плеча. Здесь упрощённо.
        time.sleep(1.0)
        self.right.relax()

    @property
    def available(self) -> bool:
        return self.left.available or self.right.available


# ─── Тест ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== Тест DualHandController ===\n")
    print(f"Тип руки (из спеки): {config.G1_HAND_TYPE}")
    print(f"Сетевой интерфейс: {config.G1_NETWORK_INTERFACE}\n")

    hands = DualHandController(
        network_interface=config.G1_NETWORK_INTERFACE,
        left_type=config.G1_HAND_TYPE,
        right_type=config.G1_HAND_TYPE,
    )

    print(f"\nLeft available: {hands.left.available}")
    print(f"Right available: {hands.right.available}")

    print("\n--- Жесты по очереди ---")
    for gesture in HAND_GESTURES:
        print(f"\n→ {gesture}: {HAND_GESTURES[gesture]}")
        hands.set_pose_both(gesture)
        time.sleep(2.0)

    print("\n--- Помахать правой ---")
    hands.wave_right()

    print("\n=== Тест завершён ===")
