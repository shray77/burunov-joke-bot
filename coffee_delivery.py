"""
coffee_delivery.py v3 — оркестрация сценария "Принеси кофе".

ОБНОВЛЕНО под реальные импорты из unitree_docs/ и существующего unitree_hands.py.

Импорты:
  - LocoClient: from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
      Методы: Start, Damp, Squat, Sit, StandUp, Move(vx,vy,vyaw), StopMove,
              BalanceStand, ContinuousGait, SetVelocity
  - AudioClient: from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
      Методы: TtsMaker(text, speaker_id), SetVolume(volume),
              LedControl(R,G,B), PlayStream(app_name, stream_id, pcm_data),
              PlayStop(app_name)
  - HandClient: from unitree_sdk2py.g1.hand.hand_client import HandClient
      (используется через force_sensor.GripController)

PlayStream PCM формат: 16kHz, mono, 16-bit, без заголовка.
"""
from __future__ import annotations

import os
import sys
import time
import json
import logging
import threading
from typing import Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lidar_obstacle import CompositeObstacleSource, ObstacleMonitor, ObstacleState
from yolo_coffee import CoffeeVision, CupDetection
from force_sensor import HandController, GripController, FORCE_GRIP_LIGHT, FORCE_TOO_HARD, GripState
from path_recorder import OdomSource, PathReplayer, Track

log = logging.getLogger("coffee_delivery")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")


# -----------------------------------------------------------------------------
# Конфигурация
# -----------------------------------------------------------------------------
RAG_URL      = os.environ.get("RAG_URL", "http://127.0.0.1:8000")
TTS_URL      = os.environ.get("TTS_URL", "http://127.0.0.1:8001")
G1_INTERFACE = os.environ.get("G1_INTERFACE", "eth0")

# Максимально простой сценарий "по меткам": один раз вручную (пультом) проводим
# робота туда и обратно, path_recorder.py пишет трек. Дальше просто повторяем
# записанное вместо того чтобы "искать путь" вживую — самое надёжное для демо.
# Если файлов нет — сценарий сам падает на старое поведение (вслепую вперёд +
# поиск чашки по камере), см. _run_scenario ниже.
TRACKS_DIR       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracks")
TRACK_THERE_PATH = os.path.join(TRACKS_DIR, "there.json")
TRACK_BACK_PATH  = os.path.join(TRACKS_DIR, "back.json")

VX_FORWARD   = 0.25
VX_BACKWARD  = 0.15
VYAW_TURN    = 0.5
TIMEOUT_FIND_CUP_S   = 30.0
TIMEOUT_APPROACH_S   = 15.0
TIMEOUT_RETURN_S     = 20.0

# LED цвета
LED_OFF     = (0, 0, 0)
LED_THINK   = (0, 0, 255)
LED_GO      = (0, 255, 0)
LED_WARN    = (255, 255, 0)
LED_ERROR   = (255, 0, 0)
LED_GRIPPED = (255, 0, 255)

# Имя приложения для PlayStream (для управления состоянием воспроизведения)
AUDIO_APP_NAME = "burunov_bot"
AUDIO_STREAM_ID = "burunov_default"


# -----------------------------------------------------------------------------
# G1Mover — обёртка над LocoClient (РЕАЛЬНЫЕ ИМПОРТЫ)
# -----------------------------------------------------------------------------
class G1Mover:
    """
    Движение G1 через LocoClient из Sport Services.
    Сверено с unitree_docs/sport_services.json.
    """

    def __init__(self, interface: str = G1_INTERFACE):
        self.interface = interface
        self._client = None
        self._initialised = False

    def init(self) -> bool:
        try:
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
            ChannelFactoryInitialize(0, self.interface)

            from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
            self._client = LocoClient()
            self._client.Init()
            # Войти в main operation control
            ret = self._client.Start()
            if ret not in (0, None):
                log.error(f"LocoClient.Start() failed: {ret}")
                return False
            self._initialised = True
            log.info("G1Mover инициализирован (LocoClient)")
            return True
        except ImportError as e:
            log.error(f"LocoClient импорт недоступен: {e}")
            log.error("Проверить что unitree_sdk2py установлен и G1 доступен по сети eth0")
            return False
        except Exception as e:
            log.error(f"G1Mover init failed: {e}")
            return False

    def stand_up(self) -> bool:
        if not self._initialised: return True
        try:
            return self._client.StandUp() == 0
        except Exception as e:
            log.warning(f"StandUp failed: {e}")
            return False

    def sit(self) -> bool:
        if not self._initialised: return True
        try:
            return self._client.Sit() == 0
        except Exception as e:
            log.warning(f"Sit failed: {e}")
            return False

    def squat(self) -> bool:
        if not self._initialised: return True
        try:
            return self._client.Squat() == 0
        except Exception as e:
            log.warning(f"Squat failed: {e}")
            return False

    def balance_stand(self) -> bool:
        if not self._initialised: return True
        try:
            return self._client.BalanceStand() == 0
        except Exception as e:
            log.warning(f"BalanceStand failed: {e}")
            return False

    def move(self, vx: float, vy: float, vyaw: float, duration_s: float,
             monitor: Optional[ObstacleMonitor] = None) -> bool:
        """
        Двигаться duration_s секунд с заданными скоростями.
        LocoClient.Move() по умолчанию действует 1 секунду, поэтому вызываем
        его в цикле пока не пройдёт duration_s.
        """
        if not self._initialised:
            log.info(f"STUB: move(vx={vx}, vy={vy}, vyaw={vyaw}, {duration_s}s)")
            time.sleep(min(duration_s, 0.1))
            return True

        # Включаем continuous gait чтобы Move() действовал дольше 1 сек
        try:
            self._client.ContinuousGait(True)
        except Exception:
            pass

        steps = max(1, int(duration_s * 10))
        dt = duration_s / steps
        success = True
        for i in range(steps):
            if monitor is not None:
                state = monitor.get_state()
                if state.state == ObstacleState.STOP and vx > 0:
                    self._client.StopMove()
                    log.warning(f"Move прерван: препятствие {state.nearest_m:.2f}м")
                    success = False
                    break
            try:
                self._client.Move(vx, vy, vyaw)
            except Exception as e:
                log.warning(f"Move step failed: {e}")
            time.sleep(dt)

        try:
            self._client.StopMove()
            self._client.ContinuousGait(False)
        except Exception:
            pass
        return success

    def stop_move(self) -> bool:
        if not self._initialised: return True
        try:
            return self._client.StopMove() == 0
        except Exception:
            return False

    def damp(self) -> bool:
        """Аварийный режим — обмякнуть."""
        if not self._initialised: return True
        try:
            return self._client.Damp() == 0
        except Exception as e:
            log.warning(f"Damp failed: {e}")
            return False


# -----------------------------------------------------------------------------
# G1Audio — обёртка над AudioClient (РЕАЛЬНЫЕ ИМПОРТЫ)
# -----------------------------------------------------------------------------
class G1Audio:
    """
    Голос + LED через AudioClient.
    Сверено с unitree_docs/vuiclient.json.
    """

    def __init__(self, interface: str = G1_INTERFACE):
        self.interface = interface
        self._client = None
        self._initialised = False

    def init(self) -> bool:
        try:
            # ChannelFactory уже инициализирован в G1Mover.init() — повторный
            # вызов тут был написан в расчёте на "безопасно повторно", но это
            # проверялось под неверным API (ChannelFactory.Initialize вместо
            # реального ChannelFactoryInitialize — см. фикс ниже). Повторный
            # вызов НЕ переверифицирован под правильную функцию — если тут
            # упадёт при тесте на роботе, это первое что проверять.
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
            ChannelFactoryInitialize(0, self.interface)

            from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
            self._client = AudioClient()
            self._client.Init()
            self._client.SetTimeout(10.0)
            self._initialised = True
            log.info("G1Audio инициализирован (AudioClient)")
            return True
        except ImportError as e:
            log.error(f"AudioClient импорт недоступен: {e}")
            return False
        except Exception as e:
            log.error(f"G1Audio init failed: {e}")
            return False

    def set_led(self, rgb: tuple) -> bool:
        """LedControl(R, G, B). Интервал вызовов > 200мс по доке."""
        if not self._initialised:
            log.info(f"STUB LED ({rgb})")
            return True
        r, g, b = rgb
        try:
            ret = self._client.LedControl(r, g, b)
            return ret == 0
        except Exception as e:
            log.warning(f"LedControl failed: {e}")
            return False

    def set_volume(self, vol: int) -> bool:
        """SetVolume(0-100). По доке для Stanley рекомендуется 100."""
        if not self._initialised: return True
        try:
            return self._client.SetVolume(vol) == 0
        except Exception as e:
            log.warning(f"SetVolume failed: {e}")
            return False

    def play_pcm(self, pcm_bytes: bytes, sample_rate: int = 16000) -> bool:
        """
        PlayStream(app_name, stream_id, pcm_data).
        PCM: 16kHz, mono, 16-bit (без заголовка WAV).
        """
        if not self._initialised:
            log.info(f"STUB play_pcm {len(pcm_bytes)} bytes")
            dur = len(pcm_bytes) / (sample_rate * 2)
            time.sleep(min(dur, 0.1))
            return True
        try:
            # PCM должен быть list[uint8] или bytes — зависит от версии SDK
            # В Python биндингах обычно принимает bytes или list[int]
            pcm_list = list(pcm_bytes)
            ret = self._client.PlayStream(AUDIO_APP_NAME, AUDIO_STREAM_ID, pcm_list)
            if ret != 0:
                log.error(f"PlayStream returned {ret}")
                return False
            # Ждём пока проиграется (оценка: 16kHz * 2 bytes/sample)
            dur = len(pcm_bytes) / (sample_rate * 2)
            time.sleep(dur + 0.1)
            return True
        except Exception as e:
            log.error(f"play_pcm failed: {e}")
            return False

    def play_stop(self) -> bool:
        if not self._initialised: return True
        try:
            return self._client.PlayStop(AUDIO_APP_NAME) == 0
        except Exception:
            return False

    def tts_builtin(self, text: str, speaker_id: int = 0) -> bool:
        """
        Встроенный TTS робота (только CN/EN — НЕ используем для Бурунова).
        Полезно для теста что динамик вообще работает.
        """
        if not self._initialised: return False
        try:
            return self._client.TtsMaker(text, speaker_id) == 0
        except Exception as e:
            log.warning(f"TtsMaker failed: {e}")
            return False


# -----------------------------------------------------------------------------
# TTS / RAG клиенты
# -----------------------------------------------------------------------------
def generate_burunov_phrase(topic_or_text: str, mode: str = "fixed") -> str:
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
    try:
        r = requests.post(f"{TTS_URL}/synthesize_pcm", json={"text": text}, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception as e:
        log.error(f"TTS failed: {e}")
        return None


def speak(audio: G1Audio, text: str, led_during: tuple = LED_GO) -> bool:
    audio.set_led(led_during)
    pcm = synthesize_burunov_pcm(text)
    if pcm is None:
        audio.set_led(LED_ERROR)
        log.error(f"TTS сломался на фразе: {text}")
        return False
    return audio.play_pcm(pcm)


# -----------------------------------------------------------------------------
# Оркестратор
# -----------------------------------------------------------------------------
class CoffeeDelivery:
    def __init__(self):
        self.mover = G1Mover()
        self.audio = G1Audio()
        self.lidar = CompositeObstacleSource()
        self.monitor = ObstacleMonitor(self.lidar)
        self.vision = CoffeeVision()
        self.hand = HandController()
        self.grip = GripController(self.hand, hand_used="right")
        self.odom = OdomSource()
        self.replayer: Optional[PathReplayer] = None
        self.track_there: Optional[Track] = None
        self.track_back: Optional[Track] = None
        self._abort = threading.Event()
        self._busy = threading.Lock()

    def init_all(self) -> dict:
        """Инициализация всех подсистем. ChannelFactory инициализируется ОДИН раз."""
        # Сначала mover (он инициализирует ChannelFactory)
        m_ok = self.mover.init()
        # Потом audio (ChannelFactory уже инициализирован, но в init вызываем ещё раз — безопасно)
        a_ok = self.audio.init()
        # Hand
        h_ok = self.hand.init()
        # Lidar
        l_ok = self.lidar.init()
        if l_ok:
            self.monitor.start_background()
        # Vision
        v_ok = self.vision.init()
        # Одометрия + записанные треки (см. path_recorder.py — "метки" из пульта)
        o_ok = self.odom.init()
        self.replayer = PathReplayer(self.mover, self.monitor, self.odom)
        self.track_there = self._load_track(TRACK_THERE_PATH)
        self.track_back = self._load_track(TRACK_BACK_PATH)

        if a_ok:
            self.audio.set_volume(100)
        if m_ok:
            self.mover.stand_up()

        return {
            "mover": m_ok, "audio": a_ok, "lidar": l_ok,
            "vision": v_ok, "hand": h_ok, "odom": o_ok,
            "track_there": self.track_there is not None,
            "track_back": self.track_back is not None,
        }

    @staticmethod
    def _load_track(path: str) -> Optional[Track]:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                track = Track.from_json(json.load(f))
            log.info(f"Трек загружен: {path} ({track.mode}, {len(track.waypoints)} точек)")
            return track
        except Exception as e:
            log.warning(f"Не удалось загрузить трек {path}: {e}")
            return None

    def abort(self):
        log.warning("ABORT requested")
        self._abort.set()
        try: self.mover.stop_move()
        except: pass
        try: self.grip.release()
        except: pass
        try: self.audio.set_led(LED_ERROR)
        except: pass

    def deliver_coffee(self, recipient: str = "Олег") -> dict:
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "error": "уже выполняется другая доставка"}
        self._abort.clear()
        result = {"ok": False, "stage": "init", "message": ""}
        try:
            result = self._run_scenario(recipient)
        except Exception as e:
            log.exception("deliver_coffee failed")
            result = {"ok": False, "stage": "exception", "message": str(e)}
            try: self.mover.damp()
            except: pass
        finally:
            self._busy.release()
        return result

    def _run_scenario(self, recipient: str) -> dict:
        # 1. Вступление Бурунова
        self.audio.set_led(LED_THINK)
        if not self._check_abort(): return {"ok": False, "stage": "abort", "message": "aborted"}
        intro = f"Угу, щас, {recipient} Тарасыч... кофеварку найду..."
        speak(self.audio, intro, led_during=LED_THINK)

        # 2. LED зелёный, движение
        self.audio.set_led(LED_GO)
        self.mover.stand_up()

        # 3. Едем к чашке — если есть записанный трек (tracks/there.json,
        # см. path_recorder.py), просто повторяем его: надёжнее чем "искать
        # путь" вслепую по камере. Без трека — старое поведение (вперёд+камера).
        cup_found = False
        if self.track_there is not None:
            log.info("Едем по записанному треку 'there'...")
            replay_result = self.replayer.replay(self.track_there)
            log.info(f"Трек 'there': {replay_result}")
            if not replay_result.get("ok"):
                speak(self.audio, f"Затупил по дороге, {recipient}. {replay_result.get('message', '')}",
                      led_during=LED_ERROR)
                return {"ok": False, "stage": "track_there", "message": replay_result.get("message", "")}
            # На месте — короткая проверка камерой, чашка должна быть уже рядом
            for _ in range(10):
                if self._check_abort(): return {"ok": False, "stage": "abort", "message": "aborted"}
                det = self.vision.detect_once()
                if det.detected and det.distance_m is not None:
                    cup_found = True
                    break
                time.sleep(0.3)
        else:
            log.info("Поиск чашки (трек не записан, идём вслепую+камера)...")
            t_find_start = time.time()
            while time.time() - t_find_start < TIMEOUT_FIND_CUP_S:
                if self._check_abort(): return {"ok": False, "stage": "abort", "message": "aborted"}
                obs = self.monitor.get_state()
                if obs.state == ObstacleState.STOP:
                    self.mover.stop_move()
                    self.audio.set_led(LED_WARN)
                    speak(self.audio, "Бля, тут кто-то стоит... дай пройти.", led_during=LED_WARN)
                    if not self.monitor.wait_until_clear(timeout_s=5.0):
                        self.mover.move(vx=0, vy=0, vyaw=VYAW_TURN, duration_s=1.5, monitor=self.monitor)
                    continue
                self.mover.move(vx=VX_FORWARD, vy=0, vyaw=0, duration_s=0.5, monitor=self.monitor)
                det = self.vision.detect_once()
                if det.detected and det.distance_m is not None:
                    log.info(f"Чашка найдена: {det.message}")
                    cup_found = True
                    break

        if not cup_found:
            speak(self.audio, f"Не вижу никакой чашки, {recipient}. Где кофе-то?", led_during=LED_ERROR)
            self.audio.set_led(LED_ERROR)
            return {"ok": False, "stage": "find_cup", "message": "чашка не найдена"}

        # 4. Подъезд к чашке
        log.info("Подъезд к чашке...")
        t_approach_start = time.time()
        approach_ok = False
        while time.time() - t_approach_start < TIMEOUT_APPROACH_S:
            if self._check_abort(): return {"ok": False, "stage": "abort", "message": "aborted"}
            obs = self.monitor.get_state()
            if obs.state == ObstacleState.STOP and obs.nearest_m < 0.25:
                self.mover.stop_move()
                break
            det = self.vision.detect_once()
            if not det.detected or det.distance_m is None or det.approach_cmd is None:
                self.mover.move(vx=VX_FORWARD*0.5, vy=0, vyaw=0, duration_s=0.3, monitor=self.monitor)
                continue
            cmd = det.approach_cmd
            if cmd["vx"] == 0.0 and cmd["vyaw"] == 0.0:
                self.mover.stop_move()
                approach_ok = True
                log.info(f"В позиции для захвата (dist={det.distance_m:.2f}м)")
                break
            self.mover.move(vx=cmd["vx"], vy=cmd["vy"], vyaw=cmd["vyaw"], duration_s=0.3, monitor=self.monitor)

        if not approach_ok:
            speak(self.audio, "Чё-то не подьезжается... ну ладно, попробую тут.", led_during=LED_WARN)

        # 5. Захват
        log.info("Захват чашки...")
        self.audio.set_led(LED_THINK)
        grip_result = self.grip.close_hand_safe(
            target_force_g=FORCE_GRIP_LIGHT,
            max_force_g=FORCE_TOO_HARD,
            progress_cb=lambda r, i: log.debug(f"grip step {i}: {r.max_force_g:.1f}g"),
        )
        if not grip_result.success:
            self.audio.set_led(LED_ERROR)
            speak(self.audio, f"Не могу взять, {recipient}. {grip_result.message}")
            return {"ok": False, "stage": "grip", "message": grip_result.message}

        log.info(f"Захват успешен: {grip_result.message}")
        self.audio.set_led(LED_GRIPPED)
        speak(self.audio, "Взял. Сейчас принесу.", led_during=LED_GRIPPED)

        # 6-7. Обратно — если есть записанный трек (tracks/back.json), он уже
        # включает нужный разворот (человек его так и провёл руками), просто
        # повторяем целиком. Без трека — старое поведение (жёсткий 180° + 3с вперёд).
        if not self.grip.check_grip_alive():
            speak(self.audio, "Ой... выронил, бля.", led_during=LED_ERROR)
            return {"ok": False, "stage": "drop_before_return", "message": "выронил перед возвратом"}

        if self.track_back is not None:
            log.info("Едем по записанному треку 'back'...")
            replay_result = self.replayer.replay(self.track_back)
            log.info(f"Трек 'back': {replay_result}")
            if not self.grip.check_grip_alive():
                speak(self.audio, "Ой... выронил, бля.", led_during=LED_ERROR)
                return {"ok": False, "stage": "drop_during_return", "message": "выронил на обратном пути"}
            if not replay_result.get("ok"):
                speak(self.audio, f"Затупил на обратном пути, {recipient}.", led_during=LED_ERROR)
                return {"ok": False, "stage": "track_back", "message": replay_result.get("message", "")}
        else:
            log.info("Разворот (трек не записан)...")
            self.mover.move(vx=0, vy=0, vyaw=VYAW_TURN, duration_s=3.5, monitor=self.monitor)
            if not self.grip.check_grip_alive():
                speak(self.audio, "Ой... выронил, бля.", led_during=LED_ERROR)
                return {"ok": False, "stage": "drop_after_turn", "message": "выронил после разворота"}

            log.info("Возврат (трек не записан, идём вслепую 3с)...")
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
                self.mover.move(vx=VX_FORWARD, vy=0, vyaw=0, duration_s=0.5, monitor=self.monitor)
                if time.time() - t_return_start > 3.0:
                    self.mover.stop_move()
                    returned = True
                    break
            if not returned:
                self.mover.stop_move()

        # 8. Поставить
        log.info("Постановка чашки...")
        before_release = self.grip.hand.read_force().max_force_g
        self.grip.release()
        time.sleep(0.5)
        after_release = self.grip.hand.read_force().max_force_g
        if after_release > before_release * 0.7:
            speak(self.audio, "Чё-то не отпускается...", led_during=LED_WARN)

        # 9. Финальная фраза
        self.audio.set_led(LED_GO)
        speak(self.audio, f"Вот ваш кофе, {recipient}. Не обожгись, бля.", led_during=LED_GO)

        # 10. Squat
        try:
            self.mover.squat()
            time.sleep(0.5)
            self.mover.stand_up()
        except Exception as e:
            log.warning(f"squat failed: {e}")

        # 11. Завершение
        self.audio.set_led(LED_OFF)
        return {"ok": True, "stage": "done", "message": f"кофе доставлен: {recipient}"}

    def _check_abort(self) -> bool:
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
        from fastapi.responses import HTMLResponse
        import uvicorn
    except ImportError:
        log.error("pip install fastapi uvicorn pydantic")
        return

    app = FastAPI(title="Burunov Coffee Delivery", version="3.0")
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
        try: delivery.vision.stop()
        except: pass

    @app.post("/coffee")
    def coffee(req: CoffeeRequest):
        result_holder = {}
        def _run():
            result_holder["result"] = delivery.deliver_coffee(req.recipient)
        t = threading.Thread(target=_run, daemon=True)
        t.start()
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
        result = delivery.deliver_coffee(req.recipient)
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result)
        return result

    # Веб-интерфейс для управления с телефона
    @app.get("/", response_class=HTMLResponse)
    def web_ui():
        return PHONE_UI_HTML

    log.info(f"Starting CoffeeDelivery server on :{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)


# -----------------------------------------------------------------------------
# Простой веб-интерфейс для телефона (HTML+JS, без зависимостей)
# -----------------------------------------------------------------------------
PHONE_UI_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Burunov Bot</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, system-ui, sans-serif; background: #1a1a1a; color: #fff;
         min-height: 100vh; padding: 16px; }
  h1 { text-align: center; margin: 16px 0; font-size: 22px; }
  .status { background: #2a2a2a; padding: 12px; border-radius: 8px; margin-bottom: 16px;
            font-family: monospace; font-size: 13px; line-height: 1.6; }
  .status .ok { color: #4ade80; }
  .status .err { color: #f87171; }
  .status .warn { color: #facc15; }
  button { width: 100%; padding: 16px; border: none; border-radius: 8px; font-size: 18px;
           font-weight: bold; margin-bottom: 12px; cursor: pointer; }
  .btn-coffee { background: #92400e; color: #fff; }
  .btn-coffee:active { background: #78350f; }
  .btn-joke { background: #1e40af; color: #fff; }
  .btn-joke:active { background: #1e3a8a; }
  .btn-stop { background: #dc2626; color: #fff; }
  .btn-stop:active { background: #b91c1c; }
  input { width: 100%; padding: 12px; border: 1px solid #444; border-radius: 8px;
          background: #2a2a2a; color: #fff; font-size: 16px; margin-bottom: 12px; }
  select { width: 100%; padding: 12px; border: 1px solid #444; border-radius: 8px;
           background: #2a2a2a; color: #fff; font-size: 16px; margin-bottom: 12px; }
  .log { background: #000; padding: 8px; border-radius: 8px; height: 200px; overflow-y: auto;
         font-family: monospace; font-size: 12px; color: #4ade80; }
</style>
</head>
<body>
  <h1>🤖 Burunov Bot</h1>

  <div class="status" id="status">Загрузка статуса...</div>

  <input type="text" id="recipient" value="Олег" placeholder="Имя получателя">

  <button class="btn-coffee" onclick="sendCoffee()">☕ Принеси кофе</button>

  <select id="joke_topic">
    <option value="Штирлиц">Штирлиц</option>
    <option value="Вовочка">Вовочка</option>
    <option value="Ржевский">Поручик Ржевский</option>
    <option value="Новые русские">Новые русские</option>
    <option value="Чапаев">Чапаев</option>
  </select>
  <button class="btn-joke" onclick="sendJoke()">🎭 Расскажи анекдот</button>

  <button class="btn-stop" onclick="sendStop()">🛑 АВАРИЙНАЯ ОСТАНОВКА</button>

  <div class="log" id="log"></div>

<script>
const ROBOT_IP = location.hostname;
const API = `http://${ROBOT_IP}:8002`;
const RAG = `http://${ROBOT_IP}:8000`;

async function fetchStatus() {
  try {
    const r = await fetch(`${API}/health`);
    const d = await r.json();
    let html = '<b>Подсистемы:</b><br>';
    html += `Mover: ${d.mover ? '<span class="ok">OK</span>' : '<span class="err">OFF</span>'}<br>`;
    html += `Audio: ${d.audio ? '<span class="ok">OK</span>' : '<span class="err">OFF</span>'}<br>`;
    html += `Lidar: ${d.lidar ? '<span class="ok">OK</span>' : '<span class="warn">OFF</span>'}<br>`;
    html += `Vision: ${d.vision ? '<span class="ok">OK</span>' : '<span class="err">OFF</span>'}<br>`;
    html += `Hand: ${d.hand ? '<span class="ok">OK</span>' : '<span class="err">OFF</span>'}<br>`;
    html += `Busy: ${d.busy ? '<span class="warn">ЗАНЯТ</span>' : '<span class="ok">свободен</span>'}<br>`;
    if (d.obstacle) {
      html += `Obstacle: ${d.obstacle.state} (${d.obstacle.nearest_m?.toFixed(2)}м)<br>`;
    }
    document.getElementById('status').innerHTML = html;
  } catch (e) {
    document.getElementById('status').innerHTML = '<span class="err">Не достучаться до :8002</span>';
  }
}

function log(msg) {
  const el = document.getElementById('log');
  const time = new Date().toLocaleTimeString();
  el.innerHTML = `[${time}] ${msg}<br>` + el.innerHTML;
}

async function sendCoffee() {
  const r = document.getElementById('recipient').value;
  log(`☕ POST /coffee recipient=${r}`);
  try {
    const resp = await fetch(`${API}/coffee`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({recipient: r})
    });
    const d = await resp.json();
    log(`→ ${JSON.stringify(d)}`);
  } catch (e) { log(`ERR: ${e}`); }
}

async function sendJoke() {
  const t = document.getElementById('joke_topic').value;
  log(`🎭 POST /tell topic=${t}`);
  try {
    const resp = await fetch(`${RAG}/tell`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({topic: t})
    });
    const d = await resp.json();
    log(`→ ${d.text?.substring(0, 100) || JSON.stringify(d)}`);
  } catch (e) { log(`ERR: ${e} — RAG может быть не запущен`); }
}

async function sendStop() {
  log('🛑 STOP');
  if (!confirm('Аварийная остановка?')) return;
  try {
    await fetch(`${API}/stop`, {method: 'POST'});
    log('→ STOPPED');
  } catch (e) { log(`ERR: ${e}`); }
}

fetchStatus();
setInterval(fetchStatus, 2000);
</script>
</body>
</html>
"""


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
        try: delivery.vision.stop()
        except: pass


if __name__ == "__main__":
    main()
