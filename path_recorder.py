"""
path_recorder.py — "запись трека через пульт": человек ведёт G1 вручную штатным
пультом, мы пишем одометрию, потом робот сам повторяет путь.

Зачем: полноценный path planning / SLAM за оставшееся время не поднять.
Teach-and-repeat — стандартный обходной манёвр: один раз проходим маршрут
руками (кухня → получатель кофе), пишем трек, дальше просто повторяем его.

Источник одометрии: DDS-топик rt/lowstate (unitree_hg.msg.dds_.LowState_),
поле imu_state.rpy[2] = курс (yaw).

ИСТОРИЯ РЕШЕНИЯ (проверено на реальном роботе, 2026-07-10):
  Изначально рассчитывали на rt/odommodestate (unitree_go.msg.dds_.IMUState_,
  в доке dds_services.json помечен как "Get odometry information") — но на
  практике этот топик НЕ публикуется вообще (0 сообщений за 5 секунд слушания
  в правильном DDS-домене, домен 0, тот же что ChannelFactoryInitialize(0, iface)).
  Дело не в разборе полей — публикатора для этого топика просто нет в эфире.

  Переключились на rt/lowstate — подтверждённо публикуется (2929 сообщений за
  3 секунды), и imu_state.rpy даёт реальный курс. Абсолютной (x,y) позиции
  там нет (это чистый IMU, без интеграции координат) — поэтому запись всегда
  идёт в режиме "yaw" (см. Replayer._replay_yaw_timed): повтор как "поворачивай
  на записанные курсы, иди вперёд той же длительностью", без точного
  позиционирования, но повторяет маршрут с поворотами вокруг мебели/препятствий.

  Если позже понадобится X/Y — проверить rt/sportmodestate (по официальной
  доке даёт только fsm_id/fsm_mode/task_id/task_time, НЕ позицию — уже
  проверено что бесполезно для этой цели) или спросить организаторов, есть
  ли на этом G1 вообще рабочая одометрия/SLAM.

Запуск:
  python3 path_recorder.py --mode record --file tracks/kitchen_to_oleg.json --duration 30
  python3 path_recorder.py --mode replay --file tracks/kitchen_to_oleg.json
"""
from __future__ import annotations

import os
import sys
import time
import json
import math
import logging
import threading
from dataclasses import dataclass, asdict, field
from typing import Optional

log = logging.getLogger("path_recorder")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

G1_INTERFACE = os.environ.get("G1_INTERFACE", "eth0")
TRACKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracks")

SAMPLE_HZ = 10.0
# Кандидаты имён полей — пробуем по очереди, без гарантий что все актуальны.
POS_FIELD_CANDIDATES = [
    ("position", 0), ("position", 1),          # msg.position[0], msg.position[1]
    ("pos", 0), ("pos", 1),
    ("x", None), ("y", None),
]
YAW_FIELD_CANDIDATES = [
    ("rpy", 2),           # обычно rpy = [roll, pitch, yaw]
    ("imu_state", "rpy", 2),
    ("yaw", None),
]


@dataclass
class Waypoint:
    t: float                       # секунды от начала записи
    x: Optional[float] = None
    y: Optional[float] = None
    yaw: Optional[float] = None    # радианы


@dataclass
class Track:
    name: str
    mode: str                      # "xy" | "yaw" | "empty"
    waypoints: list = field(default_factory=list)

    def to_json(self) -> dict:
        return {"name": self.name, "mode": self.mode,
                "waypoints": [asdict(w) for w in self.waypoints]}

    @staticmethod
    def from_json(d: dict) -> "Track":
        wps = [Waypoint(**w) for w in d["waypoints"]]
        return Track(name=d["name"], mode=d["mode"], waypoints=wps)


# -----------------------------------------------------------------------------
# OdomSource — подписка на rt/lowstate (только курс, rt/odommodestate мёртв —
# см. историю решения в шапке файла)
# -----------------------------------------------------------------------------
class OdomSource:
    def __init__(self, interface: str = G1_INTERFACE):
        self.interface = interface
        self._sub = None
        self._initialised = False
        self._last_msg = None
        self._lock = threading.Lock()

    def init(self) -> bool:
        try:
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
            from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_

            ChannelFactoryInitialize(0, self.interface)
            self._sub = ChannelSubscriber("rt/lowstate", LowState_)
            self._sub.Init(self._on_msg, 1)
            self._initialised = True
            log.info("OdomSource подписан на rt/lowstate (курс из imu_state.rpy)")
            return True
        except ImportError as e:
            log.error(f"unitree_sdk2py / LowState_ недоступны: {e}")
            return False
        except Exception as e:
            log.error(f"OdomSource init failed: {e}")
            return False

    def _on_msg(self, msg):
        with self._lock:
            self._last_msg = msg

    def get_pose(self) -> Optional[Waypoint]:
        """Best-effort извлечение позы. Возвращает None если сообщений ещё не было."""
        with self._lock:
            msg = self._last_msg
        if msg is None:
            return None
        x = self._try_extract(msg, POS_FIELD_CANDIDATES[0:2])
        y = self._try_extract(msg, POS_FIELD_CANDIDATES[2:4]) if x is None else \
            self._try_extract_pair(msg, "position", "pos")
        yaw = self._try_extract_yaw(msg)
        return Waypoint(t=0.0, x=x, y=y, yaw=yaw)

    @staticmethod
    def _try_extract_pair(msg, *names):
        for name in names:
            arr = getattr(msg, name, None)
            if arr is not None and len(arr) >= 2:
                return float(arr[1])
        return None

    @staticmethod
    def _try_extract(msg, candidates):
        for name, idx in candidates:
            arr = getattr(msg, name, None)
            if arr is None:
                continue
            try:
                if idx is None:
                    return float(arr)
                return float(arr[idx])
            except (TypeError, IndexError, ValueError):
                continue
        return None

    @staticmethod
    def _try_extract_yaw(msg):
        rpy = getattr(msg, "rpy", None)
        if rpy is not None and len(rpy) >= 3:
            return float(rpy[2])
        imu = getattr(msg, "imu_state", None)
        if imu is not None:
            rpy2 = getattr(imu, "rpy", None)
            if rpy2 is not None and len(rpy2) >= 3:
                return float(rpy2[2])
        yaw = getattr(msg, "yaw", None)
        if yaw is not None:
            return float(yaw)
        return None

    def dump_raw(self) -> str:
        """Для дебага — вывести все атрибуты последнего сообщения."""
        with self._lock:
            msg = self._last_msg
        if msg is None:
            return "(нет сообщений с rt/lowstate)"
        if hasattr(msg, "__dict__"):
            return json.dumps(vars(msg), default=str, ensure_ascii=False)
        return repr(msg)


# -----------------------------------------------------------------------------
# Запись
# -----------------------------------------------------------------------------
class PathRecorder:
    def __init__(self, odom: OdomSource):
        self.odom = odom

    def record(self, name: str, duration_s: float, hz: float = SAMPLE_HZ) -> Track:
        if not self.odom._initialised:
            log.error("OdomSource не инициализирован — запись невозможна (нет одометрии)")
            return Track(name=name, mode="empty", waypoints=[])

        log.info(f"Запись трека '{name}' на {duration_s}с. Веди робота пультом СЕЙЧАС.")
        t0 = time.time()
        waypoints: list[Waypoint] = []
        have_xy = False
        have_yaw = False
        period = 1.0 / hz
        while time.time() - t0 < duration_s:
            pose = self.odom.get_pose()
            if pose is not None:
                pose.t = round(time.time() - t0, 3)
                waypoints.append(pose)
                if pose.x is not None and pose.y is not None:
                    have_xy = True
                if pose.yaw is not None:
                    have_yaw = True
            time.sleep(period)

        mode = "xy" if have_xy else ("yaw" if have_yaw else "empty")
        if mode == "empty":
            log.warning(
                "Ни (x,y), ни yaw не нашлись в сообщениях rt/lowstate — "
                "см. историю решения в шапке файла. "
                f"Последнее сырое сообщение: {self.odom.dump_raw()}"
            )
        else:
            log.info(f"Записано {len(waypoints)} точек, режим повтора: '{mode}'")
        return Track(name=name, mode=mode, waypoints=waypoints)


def record_cli(name: str, filepath: str, duration_s: float):
    odom = OdomSource()
    odom.init()
    track = PathRecorder(odom).record(name, duration_s)
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(track.to_json(), f, ensure_ascii=False, indent=2)
    log.info(f"Сохранено: {filepath} (режим {track.mode}, {len(track.waypoints)} точек)")


# -----------------------------------------------------------------------------
# Повтор
# -----------------------------------------------------------------------------
class PathReplayer:
    """
    Повторяет записанный трек через G1Mover.move(). Использует ObstacleMonitor
    для безопасности — та же логика что в coffee_delivery.py.
    """
    XY_KP_LIN = 0.6            # П-регулятор: скорость вперёд от расстояния до точки
    XY_KP_YAW = 1.2            # П-регулятор: поворот от рассогласования курса
    XY_WAYPOINT_TOLERANCE_M = 0.15
    XY_MAX_VX = 0.25
    XY_MAX_VYAW = 0.4
    YAW_STEP_DURATION_S = 0.4  # длительность одного "шага" в yaw-режиме

    def __init__(self, mover, monitor=None, odom: Optional[OdomSource] = None,
                 yaw_replay_vx: float = 0.175):
        self.mover = mover
        self.monitor = monitor
        self.odom = odom
        # Скорость вперёд в yaw-режиме повтора. Запись делается пультом — человек
        # обычно ведёт быстрее, чем "безопасная" скриптовая скорость, поэтому
        # повтор по тем же временнЫм интервалам может пройти МЕНЬШЕ реальной
        # дистанции, чем было записано. Крутить этот параметр — самый быстрый
        # способ подогнать пройденное расстояние под записанное (см. --replay-vx).
        self.yaw_replay_vx = yaw_replay_vx

    def replay(self, track: Track) -> dict:
        if track.mode == "empty" or not track.waypoints:
            return {"ok": False, "message": "пустой трек, нечего повторять"}
        if track.mode == "xy" and self.odom is not None:
            return self._replay_xy(track)
        return self._replay_yaw_timed(track)

    def _obstacle_blocks(self) -> bool:
        if self.monitor is None:
            return False
        from lidar_obstacle import ObstacleState
        return self.monitor.get_state().state == ObstacleState.STOP

    def _replay_xy(self, track: Track) -> dict:
        """Закрытый цикл: на каждом шаге едем к следующей ещё не достигнутой точке."""
        log.info(f"Повтор трека '{track.name}' в режиме xy ({len(track.waypoints)} точек)")
        for i, wp in enumerate(track.waypoints):
            if wp.x is None or wp.y is None:
                continue
            for _ in range(200):  # защита от зависания на одной точке
                if self._obstacle_blocks():
                    self.mover.stop_move()
                    log.warning("Повтор трека приостановлен — препятствие")
                    if self.monitor and not self.monitor.wait_until_clear(timeout_s=10.0):
                        return {"ok": False, "message": f"застряли на точке {i}: препятствие не убралось"}
                    continue
                cur = self.odom.get_pose()
                if cur is None or cur.x is None:
                    time.sleep(0.1)
                    continue
                dx, dy = wp.x - cur.x, wp.y - cur.y
                dist = math.hypot(dx, dy)
                if dist < self.XY_WAYPOINT_TOLERANCE_M:
                    break
                target_yaw = math.atan2(dy, dx)
                yaw_err = _normalize_angle(target_yaw - (cur.yaw or 0.0))
                vx = min(self.XY_MAX_VX, self.XY_KP_LIN * dist)
                vyaw = max(-self.XY_MAX_VYAW, min(self.XY_MAX_VYAW, self.XY_KP_YAW * yaw_err))
                self.mover.move(vx=vx, vy=0.0, vyaw=vyaw, duration_s=0.2, monitor=self.monitor)
        self.mover.stop_move()
        return {"ok": True, "message": f"трек '{track.name}' повторён (xy)"}

    def _replay_yaw_timed(self, track: Track) -> dict:
        """
        Без (x,y): просто повторяем последовательность курсов и интервалов
        как её проходил человек — поворачиваем на записанный yaw, идём вперёд
        то же время, что было между соседними точками записи.
        """
        log.info(f"Повтор трека '{track.name}' в режиме yaw ({len(track.waypoints)} точек, без точных координат)")
        prev_t = 0.0
        prev_yaw = track.waypoints[0].yaw if track.waypoints[0].yaw is not None else 0.0
        for wp in track.waypoints:
            if self._obstacle_blocks():
                self.mover.stop_move()
                if self.monitor and not self.monitor.wait_until_clear(timeout_s=10.0):
                    return {"ok": False, "message": "застряли: препятствие не убралось"}
            dt = max(0.0, wp.t - prev_t)
            if wp.yaw is not None:
                yaw_err = _normalize_angle(wp.yaw - prev_yaw)
                if abs(yaw_err) > 0.05:
                    vyaw = max(-self.XY_MAX_VYAW, min(self.XY_MAX_VYAW, yaw_err))
                    self.mover.move(vx=0.0, vy=0.0, vyaw=vyaw, duration_s=self.YAW_STEP_DURATION_S, monitor=self.monitor)
                prev_yaw = wp.yaw
            if dt > 0.05:
                self.mover.move(vx=self.yaw_replay_vx, vy=0.0, vyaw=0.0, duration_s=min(dt, 2.0), monitor=self.monitor)
            prev_t = wp.t
        self.mover.stop_move()
        return {"ok": True, "message": f"трек '{track.name}' повторён (yaw, без точной одометрии)"}


def _normalize_angle(a: float) -> float:
    while a > math.pi:
        a -= 2 * math.pi
    while a < -math.pi:
        a += 2 * math.pi
    return a


def replay_cli(filepath: str, replay_vx: float = 0.175):
    with open(filepath, "r", encoding="utf-8") as f:
        track = Track.from_json(json.load(f))

    from coffee_delivery import G1Mover
    from lidar_obstacle import CompositeObstacleSource, ObstacleMonitor

    mover = G1Mover()
    mover.init()
    mover.stand_up()

    odom = OdomSource()
    odom.init()

    lidar = CompositeObstacleSource()
    monitor = ObstacleMonitor(lidar)
    if lidar.init():
        monitor.start_background()

    try:
        result = PathReplayer(mover, monitor, odom, yaw_replay_vx=replay_vx).replay(track)
        log.info(json.dumps(result, ensure_ascii=False))
    finally:
        monitor.stop_background()
        mover.stop_move()


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["record", "replay"], required=True)
    p.add_argument("--file", required=True, help="путь к JSON-файлу трека")
    p.add_argument("--name", default="unnamed")
    p.add_argument("--duration", type=float, default=30.0, help="для --mode record, сек")
    p.add_argument("--replay-vx", type=float, default=0.175,
                    help="скорость вперёд при повторе в yaw-режиме, м/с (для --mode replay). "
                         "Если проходит меньше записанного — увеличь; если больше — уменьши.")
    args = p.parse_args()

    if args.mode == "record":
        record_cli(args.name, args.file, args.duration)
    else:
        replay_cli(args.file, args.replay_vx)


if __name__ == "__main__":
    main()
