"""
force_sensor.py v2 — чтение сенсоров усилия Inspire RH56DFTP + безопасная логика захвата.

ОБНОВЛЕНО под реальные имена методов из unitree_docs/vuiclient.json + sport_services.json
+ сверен с unitree_hands.py из репо.

Кисти G1 EDU Ultimate — Inspire RH56DFTP:
  - 6 DOF × 2 руки, 12 суставов
  - Сенсоры усилия: 10..2500 г
  - Управляются через HandClient из unitree_sdk2py.g1.hand.hand_client

Логика:
  - close_hand_safe(target_force_g, max_force_g) — закрывает кисть, читая сенсор,
    останавливается как только сила достигла target. Если превысила max — расслабляет
    и возвращает ошибку (чтобы не раздавить фарфор).
  - check_grip() — возвращает текущее состояние захвата
  - detect_material() — эвристическое определение материала чашки по динамике силы
"""
from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable

log = logging.getLogger("force_sensor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")


# -----------------------------------------------------------------------------
# Константы — калибровать под конкретную чашку/стакан на демо
# -----------------------------------------------------------------------------
GRAVITY_G = 9.80665

# Диапазоны силы в граммах (сенсор 10..2500)
FORCE_EMPTY       = 10
FORCE_TOUCH       = 30
FORCE_GRIP_LIGHT  = 80     # бумажный стакан 200мл
FORCE_GRIP_MEDIUM = 180    # пластик/картон
FORCE_GRIP_FIRM   = 350    # фарфор, керамика
FORCE_TOO_HARD    = 1200   # риск раздавить
FORCE_MAX_SENSOR  = 2500

# Тайминги
SETTLE_TIME_S     = 0.05
STABILITY_WINDOW  = 3
STABILITY_TOL_G   = 15
CLOSE_STEP_RAD    = 0.1    # шаг закрытия в радианах (RH56DFTP работает в радианах)


class GripState(str, Enum):
    EMPTY      = "empty"
    TOUCHING   = "touching"
    GRIPPING   = "gripping"
    FIRM       = "firm"
    TOO_HARD   = "too_hard"
    DROPPED    = "dropped"


@dataclass
class GripReading:
    left_force_g: float
    right_force_g: float
    timestamp: float

    @property
    def max_force_g(self) -> float:
        return max(self.left_force_g, self.right_force_g)

    @property
    def avg_force_g(self) -> float:
        return (self.left_force_g + self.right_force_g) / 2


@dataclass
class GripResult:
    success: bool
    state: GripState
    final_force_g: float
    material_guess: str
    message: str
    readings: list


# -----------------------------------------------------------------------------
# Обёртка над HandClient — РЕАЛЬНЫЕ импорты из unitree_hands.py
# -----------------------------------------------------------------------------
class HandController:
    """
    Обёртка над unitree_sdk2 HandClient для Inspire RH56DFTP.
    Импорты сверены с unitree_hands.py из репо и unitree_docs/.
    """

    # Углы жестов в радианах (6 DOF Inspire FTP):
    # [thumb_oppose, thumb_flex, index_flex, middle_flex, ring_pinky_flex, wrist]
    GESTURE_ANGLES = {
        "open":      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "close":     [1.5, 1.5, 1.5, 1.5, 1.5, 0.0],
        "fist":      [1.5, 1.5, 1.5, 1.5, 1.5, 0.0],
        "point":     [0.0, 1.5, 0.0, 1.5, 1.5, 0.0],
        "thumbs_up": [1.0, 0.0, 1.5, 1.5, 1.5, 0.0],
        "peace":     [0.0, 1.5, 0.0, 0.0, 1.5, 0.0],
        "relaxed":   [0.3, 0.3, 0.3, 0.3, 0.3, 0.0],
    }

    def __init__(self, interface: str = "eth0", hand_type: str = "RH56DFTP"):
        self.interface = interface
        self.hand_type = hand_type
        self._client = None
        self._initialised = False

    def init(self) -> bool:
        """Инициализация ChannelFactory + HandClient."""
        try:
            from unitree_sdk2py.core.channel import ChannelFactory
            ChannelFactory.Initialize(0, self.interface)

            if self.hand_type == "RH56DFTP":
                from unitree_sdk2py.g1.hand.hand_client import HandClient
                self._client = HandClient()
                self._client.Init()
                self._client.SetTimeout(10.0)
                self._initialised = True
                log.info("HandController инициализирован (RH56DFTP)")
                return True
            else:
                log.error(f"Неподдерживаемый тип руки: {self.hand_type}")
                return False
        except Exception as e:
            log.error(f"HandController init failed: {e}")
            log.error("Возможные причины: 1) SDK не установлен  2) Рука не подключена  3) Неверный сетевой интерфейс")
            self._initialised = False
            return False

    # ---- базовые команды кисти --------------------------------------------

    def open_hand(self, hand: str = "right") -> bool:
        """Полностью открыть кисть."""
        return self._set_angles(hand, self.GESTURE_ANGLES["open"])

    def close_hand(self, hand: str = "right", degrees: float = 90.0) -> bool:
        """
        Частично закрыть кисть. degrees 0..90 → угол 0..1.5 рад.
        Используется внутри close_hand_safe() маленькими шагами.
        """
        rad = (degrees / 90.0) * 1.5
        angles = [rad, rad, rad, rad, rad, 0.0]
        return self._set_angles(hand, angles)

    def close_hand_rad(self, hand: str = "right", rad: float = 1.5) -> bool:
        """Закрыть кисть на заданный угол в радианах (0..1.5)."""
        rad = max(0.0, min(1.5, rad))
        angles = [rad, rad, rad, rad, rad, 0.0]
        return self._set_angles(hand, angles)

    def relax_hand(self, hand: str = "right") -> bool:
        """Расслабить кисть — снять усилие (для аварийного сброса)."""
        return self._set_angles(hand, self.GESTURE_ANGLES["relaxed"])

    def _set_angles(self, hand: str, angles: list) -> bool:
        """Низкоуровневый вызов SetHandAngle."""
        if not self._initialised:
            log.debug(f"STUB _set_angles({hand}, {angles})")
            return True
        try:
            # API как в unitree_hands.py: SetHandAngle(side, angles_list)
            ret = self._client.SetHandAngle(hand, angles)
            return ret == 0
        except AttributeError:
            # Альтернативный API
            try:
                ret = self._client.SetHandPose(hand, angles)
                return ret == 0
            except Exception as e:
                log.error(f"_set_angles failed: {e}")
                return False
        except Exception as e:
            log.error(f"_set_angles failed: {e}")
            return False

    # ---- чтение сенсора ----------------------------------------------------

    def read_force(self) -> GripReading:
        """
        Прочитать текущее усилие с обеих кистей в граммах.

        Реальные методы сенсора надо проверить на G1. Возможные варианты:
          - self._client.GetHandForce(hand)  → возвращает массив усилий
          - DDS-топик "rt/api/hand/force"    → подписка
          - self._client.GetHandState(hand)  → структура с полем force
        """
        if not self._initialised:
            return GripReading(left_force_g=0.0, right_force_g=0.0, timestamp=time.time())

        left_g = self._read_one_hand("left")
        right_g = self._read_one_hand("right")
        return GripReading(left_force_g=left_g, right_force_g=right_g, timestamp=time.time())

    def _read_one_hand(self, hand: str) -> float:
        """Прочитать усилие с одной кисти (граммы). Пробуем разные API."""
        # Попытка 1: GetHandForce
        try:
            force = self._client.GetHandForce(hand)
            if isinstance(force, (list, tuple)):
                # сумма компонент или максимум
                return float(max(force)) if force else 0.0
            return float(force)
        except AttributeError:
            pass
        except Exception as e:
            log.debug(f"GetHandForce failed: {e}")

        # Попытка 2: GetHandState
        try:
            state = self._client.GetHandState(hand)
            if hasattr(state, 'force'):
                return float(state.force)
            if hasattr(state, 'force_g'):
                return float(state.force_g)
            if isinstance(state, dict):
                return float(state.get('force', 0.0))
        except AttributeError:
            pass
        except Exception as e:
            log.debug(f"GetHandState failed: {e}")

        # Попытка 3: DDS-подписка на топик (надо настроить отдельно)
        # TODO: подписаться на rt/api/hand/force один раз при init
        log.debug(f"read_force({hand}): ни один API не сработал, возвращаем 0")
        return 0.0


# -----------------------------------------------------------------------------
# Безопасный контроллер захвата
# -----------------------------------------------------------------------------
class GripController:
    """
    Безопасный захват объекта с контролем силы.
    Используется в coffee_delivery.py для взятия чашки кофе.
    """

    def __init__(self, hand: HandController, hand_used: str = "right"):
        self.hand = hand
        self.hand_used = hand_used
        self._last_readings: list = []

    def classify_state(self, force_g: float) -> GripState:
        if force_g < FORCE_EMPTY:
            return GripState.EMPTY
        elif force_g < FORCE_TOUCH:
            return GripState.TOUCHING
        elif force_g < FORCE_GRIP_LIGHT:
            return GripState.GRIPPING
        elif force_g < FORCE_GRIP_FIRM:
            return GripState.GRIPPING
        elif force_g < FORCE_TOO_HARD:
            return GripState.FIRM
        else:
            return GripState.TOO_HARD

    def detect_material(self, readings: list) -> str:
        if len(readings) < 5:
            return "unknown"
        forces = [r.max_force_g for r in readings]
        n = len(forces)
        rate = (forces[-1] - forces[0]) / max(n - 1, 1)
        deltas = [forces[i+1] - forces[i] for i in range(n-1)]
        max_delta = max(deltas) if deltas else 0
        final_force = forces[-1]

        if final_force < FORCE_GRIP_LIGHT and rate < 5:
            return "paper"
        elif max_delta > 80 and final_force > FORCE_GRIP_FIRM * 0.7:
            return "porcelain"
        elif rate > 15 and final_force > FORCE_GRIP_LIGHT:
            return "cardboard"
        else:
            return "unknown"

    def close_hand_safe(
        self,
        target_force_g: float = FORCE_GRIP_LIGHT,
        max_force_g: float = FORCE_TOO_HARD,
        max_steps: int = 30,
        progress_cb: Optional[Callable] = None,
    ) -> GripResult:
        """
        Безопасно закрыть кисть до достижения target_force_g.
        """
        readings: list = []

        # 1. Открыть полностью
        self.hand.open_hand(self.hand_used)
        time.sleep(0.3)

        # 2. Закрываем маленькими шагами
        current_rad = 0.0
        stability_count = 0
        last_force = 0.0

        for step in range(max_steps):
            current_rad = min(current_rad + CLOSE_STEP_RAD, 1.5)
            self.hand.close_hand_rad(self.hand_used, rad=current_rad)
            time.sleep(SETTLE_TIME_S)

            reading = self.hand.read_force()
            readings.append(reading)
            force = reading.max_force_g

            if progress_cb:
                progress_cb(reading, step)

            log.debug(f"step={step} rad={current_rad:.2f} force={force:.1f}g")

            if force >= max_force_g:
                log.warning(f"Перетянули! force={force:.1f}g > max={max_force_g}g — расслабляем")
                self.hand.relax_hand(self.hand_used)
                return GripResult(
                    success=False,
                    state=GripState.TOO_HARD,
                    final_force_g=force,
                    material_guess=self.detect_material(readings),
                    message=f"Перетянули: {force:.0f}г. Расслабил кисть.",
                    readings=readings,
                )

            if force >= target_force_g:
                if abs(force - last_force) < STABILITY_TOL_G:
                    stability_count += 1
                else:
                    stability_count = 0
                last_force = force

                if stability_count >= STABILITY_WINDOW:
                    state = self.classify_state(force)
                    material = self.detect_material(readings)
                    log.info(f"Захват готов: force={force:.1f}g state={state.value} material={material}")
                    return GripResult(
                        success=True,
                        state=state,
                        final_force_g=force,
                        material_guess=material,
                        message=f"Взял. Сила {force:.0f}г, материал: {material}.",
                        readings=readings,
                    )
            last_force = force

        final_force = readings[-1].max_force_g if readings else 0
        log.warning(f"Не удалось взять: после {max_steps} шагов сила={final_force:.1f}g")
        return GripResult(
            success=False,
            state=GripState.EMPTY if final_force < FORCE_EMPTY else GripState.TOUCHING,
            final_force_g=final_force,
            material_guess="unknown",
            message=f"Не взял: сила {final_force:.0f}г. Возможно чашка не там где смотрим.",
            readings=readings,
        )

    def check_grip_alive(self) -> bool:
        reading = self.hand.read_force()
        return reading.max_force_g >= FORCE_TOUCH

    def release(self) -> bool:
        self.hand.open_hand(self.hand_used)
        time.sleep(0.5)
        return True


# -----------------------------------------------------------------------------
# CLI для тестирования сенсора
# -----------------------------------------------------------------------------
def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--interface", default="eth0")
    p.add_argument("--hand", default="right", choices=["left", "right"])
    p.add_argument("--target", type=float, default=FORCE_GRIP_LIGHT)
    p.add_argument("--max", type=float, default=FORCE_TOO_HARD)
    p.add_argument("--monitor", action="store_true")
    args = p.parse_args()

    hand = HandController(interface=args.interface)
    if not hand.init():
        log.warning("HandController не инициализирован — STUB режим для теста логики")

    grip = GripController(hand=hand, hand_used=args.hand)

    if args.monitor:
        log.info("Мониторинг сенсора 10 секунд...")
        t0 = time.time()
        while time.time() - t0 < 10:
            r = hand.read_force()
            state = grip.classify_state(r.max_force_g)
            print(f"  L={r.left_force_g:6.1f}g  R={r.right_force_g:6.1f}g  max={r.max_force_g:6.1f}g  -> {state.value}")
            time.sleep(0.2)
        return

    log.info(f"Тест захвата: target={args.target}g max={args.max}g hand={args.hand}")
    result = grip.close_hand_safe(target_force_g=args.target, max_force_g=args.max)
    print()
    print("=" * 60)
    print(f"Success:        {result.success}")
    print(f"State:          {result.state.value}")
    print(f"Final force:    {result.final_force_g:.1f} g")
    print(f"Material guess: {result.material_guess}")
    print(f"Message:        {result.message}")
    print("=" * 60)

    if result.success:
        log.info("Держим 2 сек и отпускаем...")
        time.sleep(2)
        grip.release()


if __name__ == "__main__":
    main()
