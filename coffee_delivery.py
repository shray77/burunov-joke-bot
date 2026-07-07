"""
coffee_delivery.py v2 — оркестрация сценария "Принеси кофе".

Связывает:
  - Lidar (lidar_obstacle.ObstacleMonitor) — безопасность движения
  - Vision (yolo_coffee.CoffeeVision) — поиск чашки кофе
  - Hand (force_sensor.GripController) — захват с контролем силы
  - LocoClient (unitree_sdk2) — движение
  - AudioClient (unitree_sdk2) — голос + LED
  - RAG/TTS — HTTP к localhost:8000 (RAG) и localhost:8001 (TTS)

Сценарий:
  1. Голос Бурунова: "Угу, щас, Олег Тарасыч..."
  2. LED: синий → зелёный
  3. Move(vx=0.25) вперёд, пока YOLO не найдёт чашку или не упрётся в препятствие
  4. Подъехать к чашке по approach_cmd (vx/vyaw коррекция)
  5. На расстоянии ~0.3м — стоп, захват
  6. GripController.close_hand_safe(target=бумажный стакан)
  7. Если не взял — голос + return
  8. Разворот 180° (vyaw=0.5, 3.5 сек)
  9. Move обратно (vx=0.25, 3 сек)
 10. Hand open — поставить
 11. Голос: "Вот ваш кофе, Олег. Не обожгись, бля."
 12. Squat → StandUp
 13. LED off

Поднимается как FastAPI на :8002:
  POST /coffee {"recipient": "Олег"} → запускает deliver_coffee()
  POST /stop — аварийная остановка
  GET  /health — статус подсистем

Запуск:
  python3 coffee_delivery.py --port 8002
"""
from __future__ import annotations

import os
import sys
import time
import json
import logging
import asyncio
import threading
from dataclasses import dataclass
from typing import Optional

import requests

# Локальные модули — предполагаем что рядом в той же директории
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lidar_obstacle import LidarSource, ObstacleMonitor, ObstacleState
from yolo_coffee import CoffeeVision, CupDetection
from force_sensor import HandController, GripController, FORCE_GRIP_LIGHT, FORCE_TOO_HARD, GripState

log = logging.getLogger("coffee_delivery")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")


# -----------------------------------------------------------------------------
# Конфигурация (мини config.py — если есть config.py, можно убрать)
# -----------------------------------------------------------------------------
RAG_URL     = os.environ.get("RAG_URL", "http://127.0.0.1:8000")
TTS_URL     = os.environ.get("TTS_URL", "http://127.0.0.1:8001")
G1_INTERFACE = os.environ.get("G1_INTERFACE", "eth0")

# Скорости движения (безопасные для демо)
VX_FORWARD   = 0.25   # м/с вперёд
VX_BACKWARD  = 0.15   # м/с назад
VYAW_TURN    = 0.5    # рад/с поворот
TIMEOUT_FIND_CUP_S   = 30.0   # максимум времени на поиск чашки
TIMEOUT_APPROACH_S   = 15.0   # максимум на подъезд к чашке
TIMEOUT_RETURN_S     = 20.0   # максимум на возврат

# LED цвета (RGB 0-255)
LED_OFF     = (0, 0, 0)
LED_THINK   = (0, 0, 255)       # синий — думает
LED_GO      = (0, 255, 0)       # зелёный — идёт
LED_WARN    = (255, 255, 0)     # жёлтый — осторожно
LED_ERROR   = (255, 0, 0)       # красный — ошибка
LED_GRIPPED = (255, 0, 255)     # пурпурный — взял


# -----------------------------------------------------------------------------
# Обёртки над unitree_sdk2 (LocoClient + AudioClient) — TODO_SDK
# -----------------------------------------------------------------------------
class G1Mover:
    """
    Движение G1 через LocoClient из Sport Services.

    TODO_SDK: проверить импорт под G1 EDU Ultimate. Возможные варианты:
      from unitree_sdk2py.g1.loco.g1_loco_client import G1LocoClient
      from unitree_sdk2py.go2.sport.sport_client import SportClient

    Свериться с unitree_docs/.
    """

    def __init__(self, interface: str = G1_INTERFACE):
        self.interface = interface
        self._client = None
        self._initialised = False

    def init(self) -> bool:
        try:
            # TODO_SDK: реальный импорт и инициализация
            # from unitree_sdk2py.core.channel import ChannelFactoryInitialize
            # from unitree_sdk2py.g1.loco.g1_loco_client import G1LocoClient
            # ChannelFactoryInitialize(0, self.interface)
            # self._client = G1LocoClient()
            # self._client.Init()
            # self._client.Start()  # вход в main operation control
            log.warning("G1Mover.init() — STUB. Реальный LocoClient не подключён.")
            self._initialised = False
            return False
        except Exception as e:
            log.error(f"G1Mover init failed: {e}")
            return False

    def stand_up(self) -> bool:
        # TODO_SDK: return self._client.StandUp() == 0
        log.info("STUB: stand_up()")
        return True

    def sit(self) -> bool:
        # TODO_SDK: return self._client.Sit() == 0
        log.info("STUB: sit()")
        return True

    def squat(self) -> bool:
        # TODO_SDK: return self._client.Squat() == 0
        log.info("STUB: squat()")
        return True

    def balance_stand(self) -> bool:
        # TODO_SDK: return self._client.BalanceStand() == 0
        log.info("STUB: balance_stand()")
        return True

    def move(self, vx: float, vy: float, vyaw: float, duration_s: float, monitor: Optional[ObstacleMonitor] = None) -> bool:
        """
        Двигаться duration_s секунд с заданными скоростями.
        Если передан monitor — проверяет препятствия перед каждым шагом и стопает если STOP.
        """
        if not self._initialised:
            log.info(f"STUB: move(vx={vx}, vy={vy}, vyaw={vyaw}, {duration_s}s)")
            time.sleep(min(duration_s, 0.1))
            return True

        steps = max(1, int(duration_s * 10))  # шаги по 0.1 сек
        dt = duration_s / steps
        for i in range(steps):
            if monitor is not None:
                state = monitor.get_state()
                if state.state == ObstacleState.STOP and vx > 0:
                    # стопаем если едем вперёд и препятствие близко
                    # TODO_SDK: self._client.StopMove()
                    log.warning(f"Move прерван: препятствие {state.nearest_m:.2f}м")
                    return False
            # TODO_SDK: self._client.Move(vx, vy, vyaw)
            time.sleep(dt)
        # TODO_SDK: self._client.StopMove()
        return True

    def stop_move(self) -> bool:
        # TODO_SDK: self._client.StopMove()
        log.info("STUB: stop_move()")
        return True

    def damp(self) -> bool:
        """Аварийный режим — обмякнуть (для безопасности)."""
        # TODO_SDK: self._client.Damp()
        log.warning("STUB: damp()")
        return True


class G1Audio:
    """
    Голос + LED через AudioClient (VuiClient).

    TODO_SDK: проверить импорт. Обычно:
      from unitree_sdk2py.go2.audio.audio_client import AudioClient
    """

    def __init__(self, interface: str = G1_INTERFACE):
        self.interface = interface
        self._client = None
        self._initialised = False

    def init(self) -> bool:
        try:
            # TODO_SDK: реальный импорт
            # from unitree_sdk2py.core.channel import ChannelFactoryInitialize
            # from unitree_sdk2py.go2.audio.audio_client import AudioClient
            # ChannelFactoryInitialize(0, self.interface)
            # self._client = AudioClient()
            # self._client.Init()
            log.warning("G1Audio.init() — STUB.")
            self._initialised = False
            return False
        except Exception as e:
            log.error(f"G1Audio init failed: {e}")
            return False

    def set_led(self, rgb: tuple[int, int, int]) -> bool:
        r, g, b = rgb
        # TODO_SDK: self._client.LedControl(r, g, b)
        log.info(f"STUB: LED ({r},{g},{b})")
        return True

    def set_volume(self, vol: int) -> bool:
        # TODO_SDK: self._client.SetVolume(vol)  # 0..100
        log.info(f"STUB: volume={vol}")
        return True

    def play_pcm(self, pcm_bytes: bytes, sample_rate: int = 16000) -> bool:
        """
        Проиграть PCM 16kHz mono 16-bit на динамик Stanley.
        TODO_SDK: AudioClient.PlayStream(app_name, stream_id, pcm_data)
        """
        log.info(f"STUB: play_pcm {len(pcm_bytes)} bytes")
        # Имитация длины воспроизведения (16kHz * 2 bytes/sample)
        dur = len(pcm_bytes) / (sample_rate * 2)
        time.sleep(min(dur, 0.1))
        return True


# -----------------------------------------------------------------------------
# TTS / RAG клиенты (HTTP к локальным серверам)
# -----------------------------------------------------------------------------
def generate_burunov_phrase(topic_or_text: str, mode: str = "fixed") -> str:
    """
    Получить фразу голосом Бурунова (текст).
    mode:
      'fixed' — отдать как есть (для реплик типа "Угу, щас...")
      'rag'   — пустить через RAG-пайплайн (для анекдотов)
    """
    if mode == "fixed":
        return topic_or_text
    try:
        r = requests.post(f"{RAG_URL}/tell", json={"topic": topic_or_text}, timeout=10)
        r.raise_for_status()
        return r.json().get("text", topic_or_text)
    except Exception as e:
        log.warning(f"RAG failed: {e} — отдаём как есть")
        return topic_or_text


def synthesize_burunov_pcm(text: str) -> Optional[bytes]:
    """
    Синтезировать PCM 16kHz mono 16-bit голосом Бурунова.
    Обращается к edge_tts_server.py или f5_tts_server.py на :8001.
    """
    try:
        r = requests.post(f"{TTS_URL}/synthesize_pcm", json={"text": text}, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception as e:
        log.error(f"TTS failed: {e}")
        return None


def speak(audio: G1Audio, text: str, led_during: tuple[int,int,int] = LED_GO) -> bool:
    """Синтез + проигрывание на динамике G1."""
    audio.set_led(led_during)
    pcm = synthesize_burunov_pcm(text)
    if pcm is None:
        audio.set_led(LED_ERROR)
        log.error(f"TTS сломался на фразе: {text}")
        return False
    audio.play_pcm(pcm)
    return True


# -----------------------------------------------------------------------------
# Оркестратор
# -----------------------------------------------------------------------------
class CoffeeDelivery:
    """Главный класс сценария."""

    def __init__(self):
        self.mover = G1Mover()
        self.audio = G1Audio()
        self.lidar = LidarSource()
        self.monitor = ObstacleMonitor(self.lidar)
        self.vision = CoffeeVision()
        self.hand = HandController()
        self.grip = GripController(self.hand, hand_used="right")
        self._abort = threading.Event()
        self._busy = threading.Lock()

    def init_all(self) -> dict:
        """Инициализация всех подсистем. Возвращает dict статусов."""
        m_ok = self.mover.init()
        a_ok = self.audio.init()
        l_ok = self.lidar.init()
        v_ok = self.vision.init()
        h_ok = self.hand.init()

        if l_ok:
            self.monitor.start_background()

        # Громкость на максимум (дока G1 рекомендует 100 для Stanley)
        if a_ok:
            self.audio.set_volume(100)

        # Встать если можем
        if m_ok:
            self.mover.stand_up()

        return {
            "mover": m_ok,
            "audio": a_ok,
            "lidar": l_ok,
            "vision": v_ok,
            "hand": h_ok,
        }

    def abort(self):
        """Аварийная остановка."""
        log.warning("ABORT requested")
        self._abort.set()
        try:
            self.mover.stop_move()
        except Exception:
            pass
        try:
            self.grip.release()
        except Exception:
            pass
        try:
            self.audio.set_led(LED_ERROR)
        except Exception:
            pass

    def deliver_coffee(self, recipient: str = "Олег") -> dict:
        """
        Основной сценарий. Запускается синхронно (можно в отдельном потоке).
        Возвращает dict с результатом.
        """
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "error": "уже выполняется другая доставка"}

        self._abort.clear()
        result = {"ok": False, "stage": "init", "message": ""}
        try:
            result = self._run_scenario(recipient)
        except Exception as e:
            log.exception("deliver_coffee failed")
            result = {"ok": False, "stage": "exception", "message": str(e)}
            try:
                self.mover.damp()
            except Exception:
                pass
        finally:
            self._busy.release()
        return result

    def _run_scenario(self, recipient: str) -> dict:
        # ----- 1. Голос: реплика Бурунова -----
        self.audio.set_led(LED_THINK)
        if not self._check_abort(): return {"ok": False, "stage": "abort", "message": "aborted"}

        # Префраз Бурунова (без RAG — это реплика, не анекдот)
        intro = f"Угу, щас, {recipient} Тарасыч... кофеварку найду..."
        speak(self.audio, intro, led_during=LED_THINK)

        # ----- 2. LED зелёный, начинаем движение -----
        self.audio.set_led(LED_GO)
        self.mover.stand_up()

        # ----- 3. Идём вперёд и ищем чашку через YOLO -----
        log.info("Поиск чашки...")
        cup_found = False
        t_find_start = time.time()

        while time.time() - t_find_start < TIMEOUT_FIND_CUP_S:
            if self._check_abort(): return {"ok": False, "stage": "abort", "message": "aborted"}

            # Проверка препятствий
            obs = self.monitor.get_state()
            if obs.state == ObstacleState.STOP:
                # Что-то прямо перед нами
                self.mover.stop_move()
                self.audio.set_led(LED_WARN)
                speak(self.audio, "Бля, тут кто-то стоит... дай пройти.", led_during=LED_WARN)
                # Подождать 2 сек — может уберут
                if not self.monitor.wait_until_clear(timeout_s=5.0):
                    # Не убрали — попробовать объехать (повернуть чуть)
                    self.mover.move(vx=0, vy=0, vyaw=VYAW_TURN, duration_s=1.5, monitor=self.monitor)
                continue

            # Один шаг вперёд
            self.mover.move(vx=VX_FORWARD, vy=0, vyaw=0, duration_s=0.5, monitor=self.monitor)

            # Сканируем
            det: CupDetection = self.vision.detect_once()
            if det.detected and det.distance_m is not None:
                log.info(f"Чашка найдена: {det.message}")
                cup_found = True
                break

        if not cup_found:
            speak(self.audio, f"Не вижу никакой чашки, {recipient}. Где кофе-то?", led_during=LED_ERROR)
            self.audio.set_led(LED_ERROR)
            return {"ok": False, "stage": "find_cup", "message": "чашка не найдена за отведённое время"}

        # ----- 4. Подъезжаем к чашке по approach_cmd -----
        log.info("Подъезд к чашке...")
        t_approach_start = time.time()
        approach_ok = False

        while time.time() - t_approach_start < TIMEOUT_APPROACH_S:
            if self._check_abort(): return {"ok": False, "stage": "abort", "message": "aborted"}

            obs = self.monitor.get_state()
            if obs.state == ObstacleState.STOP and obs.nearest_m < 0.25:
                # Уже очень близко к чему-то — стоп
                self.mover.stop_move()
                break

            det = self.vision.detect_once()
            if not det.detected or det.distance_m is None or det.approach_cmd is None:
                # Потеряли чашку — чуть проехать вперёд и пересканировать
                self.mover.move(vx=VX_FORWARD*0.5, vy=0, vyaw=0, duration_s=0.3, monitor=self.monitor)
                continue

            cmd = det.approach_cmd
            # Если в допуске — стоп, готовы хватать
            if cmd["vx"] == 0.0 and cmd["vyaw"] == 0.0:
                self.mover.stop_move()
                approach_ok = True
                log.info(f"В позиции для захвата (dist={det.distance_m:.2f}м)")
                break

            # Иначе двигаемся по команде (короткий шаг 0.3 сек)
            self.mover.move(vx=cmd["vx"], vy=cmd["vy"], vyaw=cmd["vyaw"], duration_s=0.3, monitor=self.monitor)

        if not approach_ok:
            speak(self.audio, "Чё-то не подьезжается... ну ладно, попробую тут.", led_during=LED_WARN)

        # ----- 5. Захват чашки -----
        log.info("Захват чашки...")
        self.audio.set_led(LED_THINK)

        # Целевая сила — для бумажного стакана легче, для фарфора плотнее
        # На демо по умолчанию берём лёгкий (бумага/картон). Если упругая — GripController сам усилит.
        grip_result = self.grip.close_hand_safe(
            target_force_g=FORCE_GRIP_LIGHT,
            max_force_g=FORCE_TOO_HARD,
            progress_cb=lambda r, i: log.debug(f"grip step {i}: {r.max_force_g:.1f}g"),
        )

        if not grip_result.success:
            self.audio.set_led(LED_ERROR)
            speak(self.audio, f"Не могу взять, {recipient}. Руке не хватает чего-то... {grip_result.message}")
            return {"ok": False, "stage": "grip", "message": grip_result.message, "grip": grip_result.__dict__}

        log.info(f"Захват успешен: {grip_result.message}")
        self.audio.set_led(LED_GRIPPED)
        speak(self.audio, "Взял. Сейчас принесу.", led_during=LED_GRIPPED)

        # ----- 6. Разворот 180° -----
        log.info("Разворот...")
        # Проверка что чашку всё ещё держим
        if not self.grip.check_grip_alive():
            speak(self.audio, "Ой... выронил, бля.", led_during=LED_ERROR)
            return {"ok": False, "stage": "drop_during_turn", "message": "выронил при развороте"}

        self.mover.move(vx=0, vy=0, vyaw=VYAW_TURN, duration_s=3.5, monitor=self.monitor)

        if not self.grip.check_grip_alive():
            speak(self.audio, "Ой... выронил, бля.", led_during=LED_ERROR)
            return {"ok": False, "stage": "drop_after_turn", "message": "выронил после разворота"}

        # ----- 7. Возврат к Олегу -----
        log.info("Возврат...")
        t_return_start = time.time()
        returned = False
        while time.time() - t_return_start < TIMEOUT_RETURN_S:
            if self._check_abort(): return {"ok": False, "stage": "abort", "message": "aborted"}
            if not self.grip.check_grip_alive():
                speak(self.audio, "Ой... выронил, бля.", led_during=LED_ERROR)
                return {"ok": False, "stage": "drop_during_return", "message": "выронил на обратном пути"}

            obs = self.monitor.get_state()
            if obs.state == ObstacleState.STOP:
                self.mover.stop_move()
                speak(self.audio, "Стою, кто-то на пути.", led_during=LED_WARN)
                self.monitor.wait_until_clear(timeout_s=3.0)
                continue

            # Идём вперёд 0.5 сек, потом проверяем
            self.mover.move(vx=VX_FORWARD, vy=0, vyaw=0, duration_s=0.5, monitor=self.monitor)

            # Простая эвристика остановки: прошли достаточно времени — стоп
            if time.time() - t_return_start > 3.0:
                self.mover.stop_move()
                returned = True
                break

        if not returned:
            self.mover.stop_move()

        # ----- 8. Поставить чашку -----
        log.info("Постановка чашки...")
        # Замерить силу до отпускания
        before_release = self.grip.hand.read_force().max_force_g
        self.grip.release()
        time.sleep(0.5)
        after_release = self.grip.hand.read_force().max_force_g

        if after_release > before_release * 0.7:
            # Сила не упала — возможно чашка застряла в руке
            speak(self.audio, "Чё-то не отпускается...", led_during=LED_WARN)
        else:
            log.info("Чашка поставлена (сила упала после release)")

        # ----- 9. Финальная фраза -----
        self.audio.set_led(LED_GO)
        speak(self.audio, f"Вот ваш кофе, {recipient}. Не обожгись, бля.", led_during=LED_GO)

        # ----- 10. Squat от смеха -----
        try:
            self.mover.squat()
            time.sleep(0.5)
            self.mover.stand_up()
        except Exception as e:
            log.warning(f"squat failed: {e}")

        # ----- 11. Завершение -----
        self.audio.set_led(LED_OFF)
        return {"ok": True, "stage": "done", "message": f"кофе доставлен: {recipient}"}

    def _check_abort(self) -> bool:
        """False если был запрос на abort."""
        if self._abort.is_set():
            log.warning("Abort detected — выходим из сценария")
            return False
        return True


# -----------------------------------------------------------------------------
# FastAPI сервер
# -----------------------------------------------------------------------------
def run_server(port: int = 8002):
    try:
        from fastapi import FastAPI, HTTPException
        from pydantic import BaseModel
        import uvicorn
    except ImportError:
        log.error("pip install fastapi uvicorn pydantic")
        return

    app = FastAPI(title="Burunov Coffee Delivery", version="2.0")
    delivery = CoffeeDelivery()

    class CoffeeRequest(BaseModel):
        recipient: str = "Олег"

    @app.on_event("startup")
    def _startup():
        statuses = delivery.init_all()
        log.info(f"Подсистемы: {statuses}")

    @app.on_event("shutdown")
    def _shutdown():
        delivery.monitor.stop_background()
        delivery.vision.stop()

    @app.post("/coffee")
    def coffee(req: CoffeeRequest):
        # Запускаем в отдельном потоке чтобы не блокировать HTTP
        result_holder = {}
        def _run():
            result_holder["result"] = delivery.deliver_coffee(req.recipient)
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        # Возвращаем сразу — клиент может опрашивать /health
        return {"ok": True, "started": True, "recipient": req.recipient}

    @app.post("/stop")
    def stop():
        delivery.abort()
        return {"ok": True}

    @app.get("/health")
    def health():
        return {
            "busy": delivery._busy.locked() if delivery._busy else False,
            "mover": delivery.mover._initialised,
            "audio": delivery.audio._initialised,
            "lidar": delivery.lidar._initialised,
            "vision": delivery.vision._ready,
            "hand": delivery.hand._initialised,
            "obstacle": delivery.monitor.get_state().__dict__,
        }

    @app.post("/coffee_sync")
    def coffee_sync(req: CoffeeRequest):
        """Синхронная версия — ждём окончания (для тестов)."""
        result = delivery.deliver_coffee(req.recipient)
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result)
        return result

    log.info(f"Starting CoffeeDelivery server on :{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["server", "oneshot"], default="server")
    p.add_argument("--port", type=int, default=8002)
    p.add_argument("--recipient", default="Олег")
    args = p.parse_args()

    if args.mode == "server":
        run_server(args.port)
    else:
        delivery = CoffeeDelivery()
        statuses = delivery.init_all()
        log.info(f"Подсистемы: {statuses}")
        result = delivery.deliver_coffee(args.recipient)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        delivery.monitor.stop_background()
        delivery.vision.stop()


if __name__ == "__main__":
    main()
