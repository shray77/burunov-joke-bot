"""
preset_audio.py
────────────────
Доступ к готовым wav-пресетам голоса Бурунова (XTTS v2, см. colab_xtts_v2.ipynb).

Пресеты лежат в data/preset_wav/*.wav — уже 16kHz/mono/16-bit PCM (см.
manifest.json, поле format), ресэмплены под AudioClient.PlayStream. Это
ЕДИНСТВЕННЫЙ способ говорить голосом Бурунова сейчас: XTTS слишком тяжёлый
для live-синтеза на борту G1, поэтому фиксированный набор фраз синтезирован
заранее на GPU и просто проигрывается с диска.

⚠️ Это значит: голосом Бурунова можно сказать ТОЛЬКО фразы из PRESETS ниже.
Для любого другого текста (например, анекдот из RAG на тему не из этого
списка) Бурунова-голоса нет — нужен либо live TTS сервер (не готов), либо
показывать текст/использовать другой голос как fallback.
"""
from __future__ import annotations

import json
import random
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PRESET_DIR = Path(__file__).parent / "data" / "preset_wav"


@dataclass
class Preset:
    name: str
    text: str
    duration_s: float
    path: Path
    topic: Optional[str] = None


def _load_manifest() -> dict:
    manifest_path = PRESET_DIR / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Нет {manifest_path}. Распакуй burunov_presets.tar.gz в data/preset_wav/."
        )
    return json.loads(manifest_path.read_text(encoding="utf-8"))


_manifest_cache: Optional[dict] = None


def _manifest() -> dict:
    global _manifest_cache
    if _manifest_cache is None:
        _manifest_cache = _load_manifest()
    return _manifest_cache


def list_presets() -> list[Preset]:
    """Все доступные пресеты (имя, текст, длительность, путь к wav)."""
    m = _manifest()
    out = []
    for p in m["presets"]:
        path = PRESET_DIR / p["file"]
        out.append(Preset(name=p["name"], text=p["text"], duration_s=p["duration_s"],
                           path=path, topic=p.get("topic")))
    return out


def get_preset(name: str) -> Optional[Preset]:
    for p in list_presets():
        if p.name == name:
            return p
    return None


def get_preset_pcm(name: str) -> bytes:
    """PCM-байты пресета БЕЗ WAV-заголовка — родной формат AudioClient.PlayStream."""
    preset = get_preset(name)
    if preset is None:
        raise KeyError(f"Нет пресета {name!r}. Доступны: {[p.name for p in list_presets()]}")
    with wave.open(str(preset.path), "rb") as wf:
        if wf.getframerate() != 16000 or wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise ValueError(
                f"{preset.path}: ожидали 16kHz/mono/16-bit, "
                f"получили {wf.getframerate()}Hz/{wf.getnchannels()}ch/{wf.getsampwidth()*8}bit"
            )
        return wf.readframes(wf.getnframes())


# Темы анекдотов, для которых реально есть готовый intro+joke пресет голосом
# Бурунова (см. PRESET_JOKES.md). RAG может сгенерить текст на любую другую
# тему из 27k базы, но озвучить её голосом Бурунова нечем.
JOKE_TOPIC_PRESETS = {
    "Штирлиц": ("shtirlits_intro", "shtirlits_joke"),
    "Вовочка": ("volodka_intro", "volodka_joke"),
    "Ржевский": ("rzhevsky_intro", "rzhevsky_joke"),
    "Новые русские": ("new_russians_intro", "new_russians_joke"),
    "Чапаев": ("chapaev_intro", "chapaev_joke"),
}

# Фразы сценария "принеси кофе" (см. coffee_delivery.py::_run_scenario).
COFFEE_PRESETS = {
    "intro": "coffee_intro",
    "obstacle": "coffee_obstacle",
    "no_cup": "coffee_no_cup",
    "got_it": "coffee_got_it",
    "dropped": "coffee_dropped",
    "done": "coffee_done",
}


def topics_available() -> dict[str, list[str]]:
    """
    Динамическая группировка "тема -> список имён пресетов с готовым звуком",
    построенная из manifest.json (поле "topic"), а не из хардкода.

    Изначально (5 тем × 1 анекдот) это то же самое что JOKE_TOPIC_PRESETS.
    После scripts/select_curated_jokes.py + Colab-озвучки +
    scripts/add_curated_presets_to_manifest.py тут появляются темы с
    несколькими анекдотами — тогда get_random_preset_for_topic() начинает
    реально выбирать между ними, а не всегда отдавать один и тот же файл.
    Исключаем "*_intro" — это вступительные реплики, не сами анекдоты.
    """
    out: dict[str, list[str]] = {}
    for p in list_presets():
        if p.topic is None or p.name.endswith("_intro"):
            continue
        out.setdefault(p.topic, []).append(p.name)
    return out


def get_random_preset_for_topic(topic: str) -> Optional[str]:
    """Случайный анекдот-пресет для темы (реальная вариативность, если их
    несколько), либо None если под тему нет готового звука."""
    names = topics_available().get(topic)
    if not names:
        return None
    return random.choice(names)


if __name__ == "__main__":
    presets = list_presets()
    print(f"Доступно {len(presets)} пресетов:\n")
    for p in presets:
        pcm = get_preset_pcm(p.name)
        print(f"  {p.name:20s} {p.duration_s:5.1f}s  {len(pcm):>7} bytes PCM  \"{p.text[:50]}\"")
