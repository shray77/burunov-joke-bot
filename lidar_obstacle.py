"""
lidar_obstacle.py — простая логика "препятствие <0.4м → стоп" по Livox-MID360.

Назначение: на демо робот не должен въехать в людей/мебель. Делаем простую
секторную проверку: смотрим точки в секторе ±30° перед роботом, если есть
точки ближе STOP_DISTANCE_M — стоп.

Два способа получения данных:
  1. Через unitree_sdk2 DDS (топик лидара G1) — предпочтительный
  2. Через livox_sdk2 напрямую — если DDS-топик недоступен

ВАЖНО: точные имена DDS-топиков и формат PointCloud надо проверить на G1.
       Заглушка возвращает безопасное состояние "STOP" пока лидар не инициализирован
       — это безопаснее чем ехать вслепую.
"""
from __future__ import annotations

import os
import sys
import time
import math
import threading
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

log = logging.getLogger("lidar_obstacle")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")


# -----------------------------------------------------------------------------
# Конфигурация
# -----------------------------------------------------------------------------
STOP_DISTANCE_M  = 0.40   # ближе = стоп однозначно
WARN_DISTANCE_M  = 0.80   # ближе = замедлиться
SECTOR_HALF_DEG  = 30.0   # сектор ±30° от "вперёд" (только то что прямо перед роботом)
SCAN_HZ          = 10.0   # частота опроса в фоновом режиме
MIN_POINTS_STOP  = 3      # минимум точек в опасной зоне чтобы сработать (от шума)


class ObstacleState(str, Enum):
    CLEAR = "clear"   # путь свободен
    WARN  = "warn"    # что-то в WARN_DISTANCE — замедлиться
    STOP  = "stop"    # что-то в STOP_DISTANCE — стоп


@dataclass
class ObstacleReading:
    state: ObstacleState
    nearest_m: float
    points_in_sector: int
    angle_to_nearest_deg: float
    message: str


# -----------------------------------------------------------------------------
# Lidar wrapper — TODO_SDK
# -----------------------------------------------------------------------------
class LidarSource:
    """
    Обёртка над Livox-MID360 через unitree_sdk2 DDS.

    TODO_SDK: точный топик/формат надо проверить. Возможные варианты:
      1. DDS-топик "rt/api/lidar/scan" с типом PointCloud_
      2. Локальный UDP-стрим от лидара на порт 56200 ( Livox SDK2 )
      3. ROS2 topic /livox/lidar если поднят bridging

    В доках Unitree G1 см. https://support.unitree.com/home/en/G1_developer/services_interface
    """

    def __init__(self, interface: str = "eth0"):
        self.interface = interface
        self._initialised = False
        self._subscriber = None

    def init(self) -> bool:
        """
        TODO_SDK: реальная инициализация.
        Примерный каркас:

            from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
            from unitree_sdk2py.idl.python.pointcloud_pc import PointCloud_  # проверить имя

            ChannelFactoryInitialize(0, self.interface)
            self._subscriber = ChannelSubscriber("rt/api/lidar/scan", PointCloud_)
            self._subscriber.Init()
            self._initialised = True
        """
        log.warning("LidarSource.init() — STUB. Реальная подписка на DDS-топик лидара не настроена.")
        log.warning("  -> Заменить TODO_SDK на реальный импорт/топик после проверки на G1.")
        self._initialised = False
        return False

    def get_points(self) -> Optional[np.ndarray]:
        """
        Получить последний скан лидара как Nx3 массив (x, y, z) в метрах.
        x — вперёд, y — влево, z — вверх.

        TODO_SDK: забрать последнее сообщение из subscriber, преобразовать в np.ndarray.
        Заглушка возвращает None.
        """
        # TODO_SDK
        return None


# -----------------------------------------------------------------------------
# RealSenseObstacleSource — препятствия по глубине D435, вместо Livox
# -----------------------------------------------------------------------------
# На реальном железе (диагностика по SSH) Livox MID360 НЕ обнаружен, а RealSense
# D435 подтверждён рабочим и уже используется в yolo_coffee.py для чашки.
# Даём ObstacleMonitor тот же интерфейс get_points(), но данные — из depth-кадра
# RealSense вместо лидара. Точность хуже (69° HFOV вместо 360° лидара, дальность
# всего 2.5м), но лучше чем постоянный STOP из-за отсутствующего Livox.
DEPTH_STRIDE_PX   = 16     # шаг сэмплирования по пикселям (реже = быстрее, грубее)
CAM_MOUNT_HEIGHT_M = 1.1   # плейсхолдер высоты камеры над полом — сверить на роботе
DEPTH_HFOV_DEG    = 69.0   # горизонтальный FOV RGB-модуля D435 (см. yolo_coffee.py)
DEPTH_VFOV_DEG    = 42.0   # вертикальный FOV D435 (паспортное значение)


class RealSenseObstacleSource:
    """Тот же интерфейс что LidarSource (init/get_points), но через depth D435."""

    def __init__(self):
        self._cam = None
        self._initialised = False

    def init(self) -> bool:
        try:
            from yolo_coffee import RealSenseCamera
        except ImportError as e:
            log.error(f"yolo_coffee.RealSenseCamera недоступен: {e}")
            return False
        self._cam = RealSenseCamera()
        self._initialised = self._cam.start()
        if self._initialised:
            log.info("RealSenseObstacleSource: используем D435 depth вместо Livox")
        return self._initialised

    def get_points(self) -> Optional[np.ndarray]:
        if not self._initialised:
            return None
        frames = self._cam.get_frames()
        if frames is None:
            return None
        _, depth = frames
        h, w = depth.shape

        ys = np.arange(0, h, DEPTH_STRIDE_PX)
        xs = np.arange(0, w, DEPTH_STRIDE_PX)
        grid_x, grid_y = np.meshgrid(xs, ys)
        d = depth[grid_y, grid_x]
        valid = (d > 0.1) & (d < 2.5)
        if not np.any(valid):
            return np.empty((0, 3))

        # Пинхол-проекция: угол пикселя от центра кадра по HFOV/VFOV → x,y,z в
        # системе робота (x=вперёд, y=влево, z=вверх), depth ~= расстояние вперёд.
        hfov, vfov = math.radians(DEPTH_HFOV_DEG), math.radians(DEPTH_VFOV_DEG)
        px_ang_x = ((grid_x - w / 2) / w) * hfov      # + = вправо от камеры
        px_ang_y = ((grid_y - h / 2) / h) * vfov      # + = вниз от камеры

        depth_v = d[valid]
        ang_x_v = px_ang_x[valid]
        ang_y_v = px_ang_y[valid]

        robot_x = depth_v * np.cos(ang_x_v)                          # вперёд
        robot_y = -depth_v * np.sin(ang_x_v)                         # влево (камера вправо => минус)
        robot_z = CAM_MOUNT_HEIGHT_M - depth_v * np.sin(ang_y_v)      # вверх от пола

        return np.stack([robot_x, robot_y, robot_z], axis=1)


class CompositeObstacleSource:
    """
    Пробует настоящий Livox первым (LidarSource), если недоступен — падает на
    RealSense depth. Не хардкодим "лидара нет" навсегда: если его переподключат/
    прошивку поправят, LidarSource.init() сам начнёт возвращать True.
    """

    def __init__(self, interface: str = "eth0"):
        self._lidar = LidarSource(interface)
        self._realsense = RealSenseObstacleSource()
        self._active = None
        self._initialised = False

    def init(self) -> bool:
        if self._lidar.init():
            self._active = self._lidar
            self._initialised = True
            log.info("CompositeObstacleSource: активен Livox")
            return True
        log.warning("CompositeObstacleSource: Livox недоступен, пробуем RealSense depth")
        if self._realsense.init():
            self._active = self._realsense
            self._initialised = True
            log.info("CompositeObstacleSource: активен RealSense depth (fallback)")
            return True
        log.error("CompositeObstacleSource: ни Livox, ни RealSense недоступны — остаёмся в safe-STOP")
        self._initialised = False
        return False

    def get_points(self) -> Optional[np.ndarray]:
        if self._active is None:
            return None
        return self._active.get_points()


# -----------------------------------------------------------------------------
# Безопасная логика
# -----------------------------------------------------------------------------
class ObstacleMonitor:
    """
    Фоновый монитор препятствий. Запускается отдельным потоком.
    coffee_delivery.py опрашивает is_safe_to_move() перед каждым Move().
    """

    def __init__(self, lidar: LidarSource, sector_half_deg: float = SECTOR_HALF_DEG):
        self.lidar = lidar
        self.sector_half_rad = math.radians(sector_half_deg)
        self._latest: ObstacleReading = ObstacleReading(
            state=ObstacleState.STOP,  # по умолчанию стоп = безопасно (лидар ещё не готов)
            nearest_m=float("inf"),
            points_in_sector=0,
            angle_to_nearest_deg=0.0,
            message="Лидар не инициализирован",
        )
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start_background(self):
        """Запустить фоновый опрос лидара."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="lidar-monitor")
        self._thread.start()
        log.info("ObstacleMonitor запущен в фоне")

    def stop_background(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def _loop(self):
        period = 1.0 / SCAN_HZ
        while self._running:
            try:
                self._update_once()
            except Exception as e:
                log.warning(f"lidar loop error: {e}")
            time.sleep(period)

    def _update_once(self):
        pts = self.lidar.get_points()
        if pts is None or len(pts) == 0:
            with self._lock:
                self._latest = ObstacleReading(
                    state=ObstacleState.STOP,
                    nearest_m=float("inf"),
                    points_in_sector=0,
                    angle_to_nearest_deg=0.0,
                    message="Нет данных с лидара",
                )
            return

        # pts: Nx3 (x=вперёд, y=влево, z=вверх)
        x = pts[:, 0]
        y = pts[:, 1]
        # Z не фильтруем — нам важна любая точка перед роботом на любой высоте от 0.1 до 1.5м
        z = pts[:, 2]
        height_mask = (z > 0.05) & (z < 1.8)

        # Угол в горизонтальной плоскости от оси "вперёд"
        angle = np.arctan2(y, x)  # 0 = вперёд, ±pi
        sector_mask = np.abs(angle) <= self.sector_half_rad

        # Точки в секторе, на разумной высоте
        mask = sector_mask & height_mask
        in_sector = pts[mask]

        if len(in_sector) == 0:
            with self._lock:
                self._latest = ObstacleReading(
                    state=ObstacleState.CLEAR,
                    nearest_m=float("inf"),
                    points_in_sector=0,
                    angle_to_nearest_deg=0.0,
                    message="Путь свободен",
                )
            return

        # Расстояние до ближайшей точки в секторе
        distances = np.sqrt(in_sector[:, 0] ** 2 + in_sector[:, 1] ** 2)
        nearest_idx = int(np.argmin(distances))
        nearest_m = float(distances[nearest_idx])
        nearest_angle_deg = math.degrees(float(np.arctan2(in_sector[nearest_idx, 1], in_sector[nearest_idx, 0])))

        # Подсчёт точек в опасной зоне
        stop_points = int(np.sum(distances < STOP_DISTANCE_M))

        if stop_points >= MIN_POINTS_STOP:
            state = ObstacleState.STOP
            msg = f"СТОП! Препятствие {nearest_m:.2f}м ({stop_points} точек < {STOP_DISTANCE_M}м)"
        elif nearest_m < WARN_DISTANCE_M:
            state = ObstacleState.WARN
            msg = f"Осторожно: препятствие {nearest_m:.2f}м"
        else:
            state = ObstacleState.CLEAR
            msg = f"Путь свободен, ближайшее {nearest_m:.2f}м"

        with self._lock:
            self._latest = ObstacleReading(
                state=state,
                nearest_m=nearest_m,
                points_in_sector=len(in_sector),
                angle_to_nearest_deg=nearest_angle_deg,
                message=msg,
            )

    def get_state(self) -> ObstacleReading:
        with self._lock:
            return self._latest

    def is_safe_to_move(self) -> bool:
        """True если путь свободен (CLEAR). WARN уже небезопасно — замедлиться."""
        return self.get_state().state == ObstacleState.CLEAR

    def can_cautiously_move(self) -> bool:
        """True если CLEAR или WARN (можно двигаться медленно)."""
        s = self.get_state().state
        return s in (ObstacleState.CLEAR, ObstacleState.WARN)

    def wait_until_clear(self, timeout_s: float = 30.0, poll_hz: float = 5.0) -> bool:
        """Ждать пока путь не освободится. Возвращает True если дождались."""
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if self.is_safe_to_move():
                return True
            time.sleep(1.0 / poll_hz)
        return False


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def main():
    """Тест: читать лидар 10 секунд, печатать состояние."""
    lidar = LidarSource()
    monitor = ObstacleMonitor(lidar)

    if not lidar.init():
        log.warning("Лидар не инициализирован — продолжаем в STUB режиме")

    monitor.start_background()
    log.info("Мониторинг 10 секунд...")
    t0 = time.time()
    while time.time() - t0 < 10:
        r = monitor.get_state()
        print(f"  state={r.state.value:6s} nearest={r.nearest_m:5.2f}m pts={r.points_in_sector:4d}  {r.message}")
        time.sleep(0.5)

    monitor.stop_background()
    print(f"\nIs safe to move: {monitor.is_safe_to_move()}")


if __name__ == "__main__":
    main()
