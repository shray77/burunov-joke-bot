"""
scripts/add_curated_presets_to_manifest.py
────────────────────────────────────────────
Второй шаг после select_curated_jokes.py: скормил data/curated_jokes.json в
colab_xtts_v2.ipynb (та же схема, что уже дала 16 текущих пресетов), скачал
готовые wav (по одному на preset_id, например vovochka_02.wav) — кладёшь их
в одну папку и запускаешь этот скрипт. Он:
  1. Ресемплит 24kHz -> 16kHz mono 16-bit, если нужно (AudioClient.PlayStream
     ждёт именно этот формат, как и остальные 16 пресетов).
  2. Дописывает записи в data/preset_wav/manifest.json (с полем "topic" —
     чтобы api.py мог группировать несколько анекдотов на одну тему).
  3. Копирует готовые wav в data/preset_wav/.

Запуск:
  python scripts/add_curated_presets_to_manifest.py --wav-dir path/to/colab_output
"""
from __future__ import annotations

import argparse
import audioop
import json
import shutil
import wave
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PRESET_DIR = BASE_DIR / "data" / "preset_wav"
MANIFEST_PATH = PRESET_DIR / "manifest.json"
CURATED_PATH = BASE_DIR / "data" / "curated_jokes.json"

TARGET_RATE = 16000


def resample_to_target(src_path: Path, dst_path: Path) -> float:
    """16kHz/mono/16-bit copy. Возвращает длительность в секундах."""
    with wave.open(str(src_path), "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if sampwidth != 2:
        # приводим к 16-bit если пришло что-то другое
        frames = audioop.lin2lin(frames, sampwidth, 2)
        sampwidth = 2
    if n_channels == 2:
        frames = audioop.tomono(frames, sampwidth, 0.5, 0.5)
        n_channels = 1
    if framerate != TARGET_RATE:
        frames, _ = audioop.ratecv(frames, sampwidth, n_channels, framerate, TARGET_RATE, None)
        framerate = TARGET_RATE

    with wave.open(str(dst_path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(TARGET_RATE)
        out.writeframes(frames)

    duration_s = len(frames) / 2 / TARGET_RATE
    return duration_s


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--wav-dir", required=True, help="папка с wav от Colab, имена = preset_id.wav")
    args = p.parse_args()

    wav_dir = Path(args.wav_dir)
    if not wav_dir.exists():
        print(f"Нет папки {wav_dir}")
        return

    curated = {d["preset_id"]: d for d in json.loads(CURATED_PATH.read_text(encoding="utf-8"))}
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    existing_names = {p_["name"] for p_ in manifest["presets"]}

    added = 0
    skipped_no_text = 0
    for wav_file in sorted(wav_dir.glob("*.wav")):
        preset_id = wav_file.stem
        if preset_id in existing_names:
            print(f"  пропуск (уже в manifest): {preset_id}")
            continue
        info = curated.get(preset_id)
        if info is None:
            print(f"  ПРОПУСК: {preset_id}.wav не найден в curated_jokes.json — переименован?")
            skipped_no_text += 1
            continue

        dst = PRESET_DIR / f"{preset_id}.wav"
        duration_s = resample_to_target(wav_file, dst)

        manifest["presets"].append({
            "name": preset_id,
            "file": dst.name,
            "duration_s": round(duration_s, 3),
            "text": info["text"],
            "topic": info["topic"],
        })
        added += 1
        print(f"  + {preset_id} ({info['topic']}, {duration_s:.1f}s)")

    if added:
        MANIFEST_PATH.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(f"\nДобавлено {added} пресетов в manifest.json.")
    if skipped_no_text:
        print(f"Пропущено без совпадения текста: {skipped_no_text}")


if __name__ == "__main__":
    main()
