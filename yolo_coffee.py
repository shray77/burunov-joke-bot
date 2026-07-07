"""
yolo_coffee.py — детекция чашки кофе через YOLOv8n (COCO, класс "cup") + RealSense D435.

Стратегия:
  - Берём ДЕФОЛТНУЮ YOLOv8n (yolov8n.pt) — детектит класс COCO "cup" (id 41)
  - RealSense D435 даёт RGB + выровненный depth
  - Для самой уверенной чашки:
      * bbox → центр
      * depth в области центра (медиана по 5x5 пикселям) → расстояние в метрах
      * смещение центра от центра кадра → угол для поворота робота

Не обучаем свой YOLO — для демо хватает дефолтного COCO "cup".
Если на демо детектит плохо (ложные срабатывания на кружках) — добавим фильтр
по цвету/пропорциям bbox.

Зависимости:
  pip install ultralytics pyrealsense2 numpy opencv-python

Запуск на G1:
  python3 yolo_coffee.py --mode single        # один снимок, напечатать результат
  python3 yolo_coffee.py --mode track         # 30 сек трекинга, печать каждые 0.3с
  python3 yolo_coffee.py --mode server --port 8005  # HTTP сервер /detect
"""
from __future__ import annotations

import time
import math
import logging
import json
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np

log = logging.getLogger("yolo_coffee")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# -----------------------------------------------------------------------------
# Конфигурация
# -----------------------------------------------------------------------------
YOLO_MODEL_NAME = "yolov8n.pt"        # дефолт COCO, скачается при первом запуске
CUP_CLASS_ID    = 41                  # COCO "cup" — НЕ "wine_glass"(40), НЕ "bottle"(39)
CONF_THRESHOLD  = 0.45                # минимум уверенности
DEPTH_PATCH_PX  = 5                   # окрестность вокруг центра bbox для замера глубины (N x N)
DEPTH_MAX_M     = 2.5                 # всё что дальше — игнорим (чашка слишком далеко)
DEPTH_MIN_M     = 0.15                # ближе — игнорим (прозрачные шумы RealSense)
TARGET_DIST_M   = 0.30                # целевое расстояние до чашки для захвата
TOLERANCE_M     = 0.05                # ± допуск по дистанции
HFOV_DEG        = 69.0                # горизонтальный угол обзора D435 (RGB-модуль)

# Цветовой фильтр (опц.) — если на демо детектит ложные чашки.
# По умолчанию выключен. Включить если надо отличать кофейную чашку от чайной.
USE_COLOR_FILTER = False
COFFEE_HSV_LOW   = (0, 30, 30)
COFFEE_HSV_HIGH  = (180, 255, 220)


# -----------------------------------------------------------------------------
# Data classes
# -----------------------------------------------------------------------------
@dataclass
class CupDetection:
    detected: bool
    bbox_xyxy: Optional[list[float]] = None    # [x1,y1,x2,y2] в пикселях
    center_xy: Optional[tuple[int, int]] = None
    distance_m: Optional[float] = None         # расстояние до центра чашки
    confidence: Optional[float] = None
    # Навигационные подсказки для LocoClient.Move()
    offset_x_px: Optional[int] = None          # смещение от центра кадра (px)
    offset_angle_deg: Optional[float] = None   # смещение в градусах (для vyaw)
    approach_cmd: Optional[dict] = None        # {vx, vy, vyaw} готовая команда
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# -----------------------------------------------------------------------------
# RealSense wrapper
# -----------------------------------------------------------------------------
class RealSenseCamera:
    """Тонкая обёртка над pyrealsense2 — RGB + align depth."""

    def __init__(self, width: int = 640, height: int = 480, fps: int = 15):
        self.width = width
        self.height = height
        self.fps = fps
        self.pipeline = None
        self.align = None
        self._started = False

    def start(self) -> bool:
        try:
            import pyrealsense2 as rs
        except ImportError:
            log.error("pyrealsense2 не установлен. Ставим: pip install pyrealsense2")
            return False

        self.pipeline = rs.pipeline()
        cfg = rs.config()
        cfg.enable_stream(rs.stream.color, self.width, self.height, rs.format.bgr8, self.fps)
        cfg.enable_stream(rs.stream.depth, self.width, self.height, rs.format.z16, self.fps)

        try:
            self.pipeline.start(cfg)
        except Exception as e:
            log.error(f"Не удалось запустить RealSense pipeline: {e}")
            return False

        self.align = rs.align(rs.stream.color)
        self._started = True
        log.info(f"RealSense запущен: {self.width}x{self.height}@{self.fps}")
        return True

    def get_frames(self) -> Optional[tuple[np.ndarray, np.ndarray]]:
        """Возвращает (color_bgr, depth_m) или None."""
        if not self._started:
            return None
        import pyrealsense2 as rs
        try:
            frames = self.pipeline.wait_for_frames(timeout_ms=2000)
            aligned = self.align.process(frames)
            color_frame = aligned.get_color_frame()
            depth_frame = aligned.get_depth_frame()
            if not color_frame or not depth_frame:
                return None
            color = np.asanyarray(color_frame.get_data())  # BGR
            depth = np.asanyarray(depth_frame.get_data()).astype(np.float32) / 1000.0  # в метры
            return color, depth
        except Exception as e:
            log.warning(f"get_frames failed: {e}")
            return None

    def stop(self):
        if self._started and self.pipeline:
            try:
                self.pipeline.stop()
            except Exception:
                pass
        self._started = False


# -----------------------------------------------------------------------------
# YOLOv8 wrapper
# -----------------------------------------------------------------------------
class CupDetector:
    """YOLOv8 детектор класса 'cup'."""

    def __init__(self, model_path: str = YOLO_MODEL_NAME):
        self.model = None
        try:
            from ultralytics import YOLO
            self.model = YOLO(model_path)
            log.info(f"YOLO загружена: {model_path}")
        except ImportError:
            log.error("ultralytics не установлен. Ставим: pip install ultralytics")
        except Exception as e:
            log.error(f"Не удалось загрузить YOLO: {e}")

    def detect_cups(self, color_bgr: np.ndarray) -> list[dict]:
        """Возвращает список bbox'ов для класса 'cup', отсортированных по уверенности."""
        if self.model is None:
            return []
        try:
            results = self.model(color_bgr, verbose=False, conf=CONF_THRESHOLD)
        except Exception as e:
            log.warning(f"YOLO inference failed: {e}")
            return []

        cups = []
        for r in results:
            boxes = r.boxes
            for i in range(len(boxes)):
                cls_id = int(boxes.cls[i].item())
                if cls_id != CUP_CLASS_ID:
                    continue
                conf = float(boxes.conf[i].item())
                xyxy = boxes.xyxy[i].tolist()
                cups.append({
                    "bbox": xyxy,
                    "confidence": conf,
                })
        cups.sort(key=lambda c: c["confidence"], reverse=True)
        return cups


# -----------------------------------------------------------------------------
# Главный класс
# -----------------------------------------------------------------------------
class CoffeeVision:
    """Связка RealSense + YOLOv8 = детекция чашки с расстоянием."""

    def __init__(self):
        self.cam = RealSenseCamera()
        self.detector = CupDetector()
        self._ready = False

    def init(self) -> bool:
        cam_ok = self.cam.start()
        yolo_ok = self.detector.model is not None
        self._ready = cam_ok and yolo_ok
        if not self._ready:
            log.warning(f"CoffeeVision не полностью готов: cam={cam_ok} yolo={yolo_ok}")
        return self._ready

    def detect_once(self) -> CupDetection:
        """Один цикл: захват → детект → замер глубины → вернуть результат."""
        if not self._ready:
            return CupDetection(detected=False, message="CoffeeVision не инициализирован")

        frames = self.cam.get_frames()
        if frames is None:
            return CupDetection(detected=False, message="Не удалось получить кадр с RealSense")
        color, depth = frames

        cups = self.detector.detect_cups(color)
        if not cups:
            return CupDetection(detected=False, message="Чашек не найдено")

        # Берём самую уверенную
        best = cups[0]
        x1, y1, x2, y2 = best["bbox"]
        cx = int((x1 + x2) / 2)
        cy = int((y1 + y2) / 2)
        conf = best["confidence"]

        # Замер глубины — медиана по NxN патчу вокруг центра bbox
        h, w = depth.shape
        patch = depth[
            max(0, cy - DEPTH_PATCH_PX):min(h, cy + DEPTH_PATCH_PX + 1),
            max(0, cx - DEPTH_PATCH_PX):min(w, cx + DEPTH_PATCH_PX + 1),
        ]
        valid = patch[(patch > DEPTH_MIN_M) & (patch < DEPTH_MAX_M)]
        if len(valid) == 0:
            return CupDetection(
                detected=True,
                bbox_xyxy=[x1, y1, x2, y2],
                center_xy=(cx, cy),
                distance_m=None,
                confidence=conf,
                message="Чашка найдена, но глубина некорректна (ближе 15см или дальше 2.5м)",
            )
        distance_m = float(np.median(valid))

        # Смещение от центра кадра (для поворота)
        frame_cx = w // 2
        offset_x_px = cx - frame_cx
        # Перевод смещения в градусы (приблизительно, для HFOV)
        offset_angle_deg = (offset_x_px / w) * HFOV_DEG

        # Команда приближения
        approach = self._compute_approach(distance_m, offset_angle_deg)

        return CupDetection(
            detected=True,
            bbox_xyxy=[x1, y1, x2, y2],
            center_xy=(cx, cy),
            distance_m=distance_m,
            confidence=conf,
            offset_x_px=offset_x_px,
            offset_angle_deg=offset_angle_deg,
            approach_cmd=approach,
            message=f"Чашка: dist={distance_m:.2f}м offset={offset_angle_deg:+.1f}° conf={conf:.2f}",
        )

    def _compute_approach(self, distance_m: float, offset_angle_deg: float) -> dict:
        """
        Сформировать команду {vx, vy, vyaw} для LocoClient.Move().
        Логика:
          - Если далеко (dist > TARGET + TOL) — двигаемся вперёд
          - Если близко (dist < TARGET - TOL) — чуть назад
          - Если в допуске — стоп
          - vyaw корректирует угол к чашке (медленно)
        """
        if distance_m > TARGET_DIST_M + TOLERANCE_M:
            vx = 0.20   # вперёд медленно
        elif distance_m < TARGET_DIST_M - TOLERANCE_M:
            vx = -0.10  # чуть назад
        else:
            vx = 0.0

        # Поворот к чашке
        if abs(offset_angle_deg) > 5.0:
            vyaw = 0.3 * (1 if offset_angle_deg > 0 else -1)
        else:
            vyaw = 0.0

        return {"vx": vx, "vy": 0.0, "vyaw": vyaw}

    def track_for(self, duration_s: float, interval_s: float = 0.3, cb=None) -> list[CupDetection]:
        """Трекинг чашки указанное время. cb(det, idx) — колбэк."""
        results = []
        t0 = time.time()
        i = 0
        while time.time() - t0 < duration_s:
            det = self.detect_once()
            results.append(det)
            if cb:
                cb(det, i)
            i += 1
            time.sleep(interval_s)
        return results

    def stop(self):
        self.cam.stop()


# -----------------------------------------------------------------------------
# HTTP сервер (опц.)
# -----------------------------------------------------------------------------
def run_server(port: int = 8005):
    """FastAPI сервер /detect для интеграции с coffee_delivery.py."""
    try:
        from fastapi import FastAPI
        import uvicorn
    except ImportError:
        log.error("fastapi/uvicorn не установлены. Ставим: pip install fastapi uvicorn")
        return

    app = FastAPI(title="Coffee Vision")
    vision = CoffeeVision()

    @app.on_event("startup")
    def _startup():
        ok = vision.init()
        log.info(f"CoffeeVision init: {ok}")

    @app.on_event("shutdown")
    def _shutdown():
        vision.stop()

    @app.get("/detect")
    def detect():
        det = vision.detect_once()
        return det.to_dict()

    @app.get("/health")
    def health():
        return {"ready": vision._ready}

    log.info(f"Starting CoffeeVision server on :{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["single", "track", "server"], default="single")
    p.add_argument("--port", type=int, default=8005)
    p.add_argument("--duration", type=float, default=10.0, help="для mode=track")
    args = p.parse_args()

    if args.mode == "server":
        run_server(args.port)
        return

    vision = CoffeeVision()
    if not vision.init():
        log.error("Не удалось инициализировать CoffeeVision. Реальные камера/YOLO недоступны?")
        # всё равно продолжаем чтобы показать что код работает

    if args.mode == "single":
        det = vision.detect_once()
        print(json.dumps(det.to_dict(), indent=2, ensure_ascii=False))
    elif args.mode == "track":
        def cb(d, i):
            if d.detected:
                print(f"[{i:3d}] {d.message}")
            else:
                print(f"[{i:3d}] нет чашки: {d.message}")
        vision.track_for(args.duration, cb=cb)

    vision.stop()


if __name__ == "__main__":
    main()
