"""
force_sensor.py — чтение сенсоров усилия Inspire RH56DFTP + безопасная логика захвата.

Кисти G1 EDU Ultimate — Inspire RH56DFTP:
  - 6 DOF × 2 руки, 12 суставов
  - Сенсоры усилия: 10..2500 г
  - Управляются через HandClient из unitree_sdk2 (точный импорт см. TODO ниже —
    зависит от версии SDK, см. unitree_docs/ в репо).

Логика:
  - close_hand_safe(target_force_g, max_force_g) — закрывает кисть, читая сенсор,
    останавливается как только сила достигла target. Если превысила max — расслабляет
    и возвращает ошибку (чтобы не раздавить фарфор).
  - check_grip() — возвращает текущее состояние захвата (empty/touching/gripping/firm/too_hard/dropped)
  - detect_material() — эвристическое определение материала чашки по динамике нарастания силы

ВАЖНО: точный API HandClient зависит от версии unitree_sdk2 и firmware G1.
       Везде где стоит TODO_SDK — сверить с unitree_docs/ и при необходимости
       подкорректировать имена методов/топиков.
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
GRAVITY_G = 9.80665  # м/с^2

# Диапазоны силы в граммах (сенсор 10..2500)
FORCE_EMPTY       = 10     # ниже = пусто (шум)
FORCE_TOUCH       = 30     # коснулись объекта
FORCE_GRIP_LIGHT  = 80     # лёгкий захват (бумажный стакан 200мл)
FORCE_GRIP_MEDIUM = 180    # средний (пластик/картон)
FORCE_GRIP_FIRM   = 350    # плотный (фарфор, керамика)
FORCE_TOO_HARD    = 1200   # выше = риск раздавить
FORCE_MAX_SENSOR  = 2500   # потолок сенсора

# Тайминги
SETTLE_TIME_S     = 0.05   # пауза между шагами чтения сенсора
STABILITY_WINDOW  = 3      # сколько подряд чтений должны быть в пределах допуска
STABILITY_TOL_G   = 15     # допуск "стабильности" в граммах
CLOSE_STEP_DEG    = 2.0    # шаг закрытия кисти в градусях


class GripState(str, Enum):
    EMPTY      = "empty"        # сенсор < FORCE_EMPTY — ничего в руке
    TOUCHING   = "touching"     # только коснулись
    GRIPPING   = "gripping"     # лёгкий/средний захват — для бумажных стаканов
    FIRM       = "firm"         # плотный захват — для фарфора
    TOO_HARD   = "too_hard"     # перетянули — риск раздавить
    DROPPED    = "dropped"      # была нагрузка, потом упала — выронили


@dataclass
class GripReading:
    """Снимок показаний сенсора в один момент времени."""
    left_force_g: float   # сила на левой кисти, граммы
    right_force_g: float  # сила на правой кисти, граммы
    timestamp: float

    @property
    def max_force_g(self) -> float:
        return max(self.left_force_g, self.right_force_g)

    @property
    def avg_force_g(self) -> float:
        return (self.left_force_g + self.right_force_g) / 2


@dataclass
class GripResult:
    """Результат операции захвата."""
    success: bool
    state: GripState
    final_force_g: float
    material_guess: str   # "paper" / "cardboard" / "porcelain" / "unknown"
    message: str
    readings: list[GripReading]


# -----------------------------------------------------------------------------
# Обёртка над HandClient — ТУТ НАДО ПРОВЕРИТЬ ИМПОРТ И API
# -----------------------------------------------------------------------------
class HandController:
    """
    Обёртка над unitree_sdk2 HandClient для Inspire RH56DFTP.

    TODO_SDK: точный импорт зависит от версии SDK. Возможные варианты:
      from unitree_sdk2py.go2.hand.hand_client import HandClient
      from unitree_sdk2py.g1.hand.hand_client import HandClient
      или отдельный SDK от Inspire Robotics.

    Свериться:
      - unitree_docs/ в репо
      - https://support.unitree.com/home/en/G1_developer/services_interface
    """

    def __init__(self, interface: str = "eth0"):
        self.interface = interface
        self._client = None
        self._initialised = False

    def init(self) -> bool:
        """Инициализация канала и клиента кистей."""
        try:
            # TODO_SDK: раскомментировать правильный импорт после проверки
            # from unitree_sdk2py.core.channel import ChannelFactoryInitialize
            # from unitree_sdk2py.go2.hand.hand_client import HandClient  # ← проверить
            # ChannelFactoryInitialize(0, self.interface)
            # self._client = HandClient()
            # self._initialised = self._client.Init() == 0
            log.warning("HandController.init() — STUB. Раскомментировать реальный импорт после сверки с unitree_docs/")
            self._initialised = False
            return False
        except Exception as e:
            log.error(f"HandController init failed: {e}")
            return False

    # ---- базовые команды кисти --------------------------------------------

    def open_hand(self, hand: str = "both") -> bool:
        """Полностью открыть кисть. hand: 'left' / 'right' / 'both'."""
        # TODO_SDK: self._client.SetHandState(hand_id, OPEN)
        log.info(f"open_hand({hand}) — STUB")
        return True

    def close_hand(self, hand: str = "both", degrees: float = 90.0) -> bool:
        """
        Закрыть кисть на заданный угол. Не использует сенсор — тупо команда.
        Используется внутри close_hand_safe() маленькими шагами.
        """
        # TODO_SDK: self._client.SetHandAngle(hand_id, degrees)
        log.info(f"close_hand({hand}, {degrees}°) — STUB")
        return True

    def relax_hand(self, hand: str = "both") -> bool:
        """Расслабить кисть — снять усилие (для аварийного сброса)."""
        # TODO_SDK: self._client.SetHandState(hand_id, RELAX)
        log.info(f"relax_hand({hand}) — STUB")
        return True

    # ---- чтение сенсора ----------------------------------------------------

    def read_force(self) -> GripReading:
        """
        Прочитать текущее усилие с обеих кистей.
        Возвращает GripReading с силой в граммах.

        TODO_SDK: точный метод получения данных сенсора — может быть
          self._client.GetHandForce(hand_id)
          или через DDS-топик "/api/hand/force"
          или через отдельный Inspire Hand SDK.

        Заглушка возвращает 0 — заменить на реальный вызов!
        """
        # TODO_SDK: real implementation
        return GripReading(left_force_g=0.0, right_force_g=0.0, timestamp=time.time())


# -----------------------------------------------------------------------------
# Высокоуровневая логика захвата
# -----------------------------------------------------------------------------
class GripController:
    """
    Безопасный захват объекта с контролем силы.
    Используется в coffee_delivery.py для взятия чашки кофе.
    """

    def __init__(self, hand: HandController, hand_used: str = "right"):
        self.hand = hand
        self.hand_used = hand_used  # какой кистью берём (правой обычно)
        self._last_readings: list[GripReading] = []

    def classify_state(self, force_g: float) -> GripState:
        """Классифицировать состояние по текущей силе."""
        if force_g < FORCE_EMPTY:
            return GripState.EMPTY
        elif force_g < FORCE_TOUCH:
            return GripState.TOUCHING
        elif force_g < FORCE_GRIP_LIGHT:
            return GripState.GRIPPING
        elif force_g < FORCE_GRIP_FIRM:
            return GripState.GRIPPING   # всё ещё gripping, но плотнее
        elif force_g < FORCE_TOO_HARD:
            return GripState.FIRM
        else:
            return GripState.TOO_HARD

    def detect_material(self, readings: list[GripReading]) -> str:
        """
        Эвристическое определение материала по динамике нарастания силы.
        - Бумажный стакан: мягкий, сила растёт медленно, податливая
        - Картон: жёстче, сила растёт быстрее, упирается
        - Фарфор/керамика: жёсткий, сила растёт резко, упирается сразу
        """
        if len(readings) < 5:
            return "unknown"

        forces = [r.max_force_g for r in readings]
        # Угол наклона нарастания
        n = len(forces)
        # средняя скорость нарастания силы (г/чз-чтение)
        rate = (forces[-1] - forces[0]) / max(n - 1, 1)
        # максимальная производная
        deltas = [forces[i+1] - forces[i] for i in range(n-1)]
        max_delta = max(deltas) if deltas else 0
        final_force = forces[-1]

        # Эвристика — НАСТРОИТЬ ПОСЛЕ ТЕСТОВ НА РЕАЛЬНОЙ ЧАШКЕ
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
        max_steps: int = 45,
        progress_cb: Optional[Callable[[GripReading, int], None]] = None,
    ) -> GripResult:
        """
        Безопасно закрыть кисть до достижения target_force_g.

        Алгоритм:
          1. Открываем кисть полностью
          2. Закрываем маленькими шагами по CLOSE_STEP_DEG
          3. После каждого шага читаем сенсор
          4. Если сила превысила target — стоп, захват готов
          5. Если сила превысила max — расслабляем, ошибка
          6. Если после max_steps сила не набралась — не взяли (пусто)

        Args:
          target_force_g: целевая сила захвата (по умолч. бумажный стакан)
          max_force_g:    предельная сила, выше которой отбой
          max_steps:      максимум шагов закрытия (чтобы не зациклиться)
          progress_cb:    колбэк для логирования/визуализации

        Returns:
          GripResult с финальным состоянием
        """
        readings: list[GripReading] = []

        if not self.hand._initialised:
            log.warning("HandController не инициализирован — работаем в STUB режиме")

        # 1. Открыть
        self.hand.open_hand(self.hand_used)
        time.sleep(0.3)

        # 2. Закрываем по шагам
        current_deg = 0.0
        stability_count = 0
        last_force = 0.0

        for step in range(max_steps):
            current_deg = min(current_deg + CLOSE_STEP_DEG, 90.0)
            self.hand.close_hand(self.hand_used, degrees=current_deg)
            time.sleep(SETTLE_TIME_S)

            reading = self.hand.read_force()
            readings.append(reading)
            force = reading.max_force_g

            if progress_cb:
                progress_cb(reading, step)

            log.debug(f"step={step} deg={current_deg:.1f} force={force:.1f}g")

            # Проверка перетяга
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

            # Проверка достижения целевой силы
            if force >= target_force_g:
                # Проверка стабильности (не растёт ли ещё)
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

        # 3. Не набрали силу — пусто
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
        """
        Проверка — всё ещё держим? Используется во время движения обратно,
        чтобы заметить что чашку выронили.

        Возвращает True если сила всё ещё выше FORCE_TOUCH.
        """
        reading = self.hand.read_force()
        return reading.max_force_g >= FORCE_TOUCH

    def release(self) -> bool:
        """Плавно открыть кисть — поставить чашку."""
        self.hand.open_hand(self.hand_used)
        time.sleep(0.5)
        return True


# -----------------------------------------------------------------------------
# CLI для тестирования сенсора без оркестратора
# -----------------------------------------------------------------------------
def main():
    """Тестовый запуск: инициализировать кисть, сделать тестовый захват."""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--interface", default="eth0", help="сетевой интерфейс G1")
    p.add_argument("--hand", default="right", choices=["left", "right"], help="рабочая кисть")
    p.add_argument("--target", type=float, default=FORCE_GRIP_LIGHT, help="целевая сила, г")
    p.add_argument("--max", type=float, default=FORCE_TOO_HARD, help="макс сила, г")
    p.add_argument("--monitor", action="store_true", help="только мониторить сенсор 10 сек")
    args = p.parse_args()

    hand = HandController(interface=args.interface)
    if not hand.init():
        log.warning("HandController не инициализирован — продолжаем в STUB режиме для теста логики")

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
    print(f"Readings count: {len(result.readings)}")
    print("=" * 60)

    if result.success:
        log.info("Держим 2 сек и отпускаем...")
        time.sleep(2)
        grip.release()
        log.info("Готово.")


if __name__ == "__main__":
    main()
