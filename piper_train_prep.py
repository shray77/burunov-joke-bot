"""
piper_train_prep.py
───────────────────
Конвертация вывода audio_prep.py в LJSpeech-формат для обучения Piper TTS.

Piper (VITS-based) требует:
  - audio_files/   — wav-ки 22050 Hz, моно, 16-bit
  - metadata.csv   — lines: <filename>|<text>|<text>

LJSpeech формат = id|text (без тегов), но Piper умеет с тегами через
piper-phonemize. Для русского используем espeak-ng backend.

────────────────────────────────────────────────────────────────────────
Установка:
  pip install piper-tts
  sudo apt install espeak-ng    # обязательно для русского
────────────────────────────────────────────────────────────────────────
"""
import csv
import json
import shutil
from pathlib import Path

import config

# Куда складываем датасет для Piper
PIPER_DIR = config.DATA_DIR / "piper_dataset"
AUDIO_DIR = PIPER_DIR / "audio_files"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# Piper хочет 22050 Hz (VITS стандарт). Если у тебя 16000 — пересемплируем.
TARGET_SR = 22050


def normalize_audio_for_piper(src: Path, dst: Path) -> bool:
    """Моно, 22050 Hz, 16-bit. Через ffmpeg."""
    import subprocess
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(src),
                "-ac", "1", "-ar", str(TARGET_SR),
                "-sample_fmt", "s16",
                str(dst),
            ],
            check=True, capture_output=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def main():
    manifest_path = config.DATA_DIR / "burunov_training" / "manifest.json"
    if not manifest_path.exists():
        print(f"ERROR: нет {manifest_path}")
        print("Сначала запусти audio_prep.py для подготовки аудио Бурунова.")
        return

    with manifest_path.open("r", encoding="utf-8") as f:
        items = json.load(f)

    print(f"Загружено из манифеста: {len(items)} кусков")
    if not items:
        print("Манифест пустой.")
        return

    metadata_rows = []
    skipped = 0

    for item in items:
        src = config.DATA_DIR / "burunov_training" / item["wav"]
        if not src.exists():
            print(f"  skip {item['wav']} (нет файла)")
            skipped += 1
            continue

        text = item["text"].strip()
        if len(text) < 5 or len(text) > 300:
            # Piper плохо переваривает слишком короткие/длинные фразы
            skipped += 1
            continue

        # Имя файла в LJSpeech-стиле: burunov_0001.wav
        new_name = f"burunov_{item['id']:04d}.wav"
        dst = AUDIO_DIR / new_name
        if not normalize_audio_for_piper(src, dst):
            print(f"  ffmpeg failed: {src.name}")
            skipped += 1
            continue

        # LJSpeech: filename|text|text  (дважды — стандарт)
        metadata_rows.append({
            "filename": new_name,
            "text": text,
        })

    # Сохраняем metadata.csv
    metadata_path = PIPER_DIR / "metadata.csv"
    with metadata_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="|", quoting=csv.QUOTE_MINIMAL)
        for row in metadata_rows:
            # LJSpeech формат: file|text|text
            writer.writerow([row["filename"], row["text"], row["text"]])

    print(f"\n{'=' * 50}")
    print(f"ГОТОВО. Датасет для Piper:")
    print(f"  Папка: {PIPER_DIR}")
    print(f"  Аудио: {AUDIO_DIR} ({len(metadata_rows)} файлов)")
    print(f"  Метадата: {metadata_path}")
    print(f"  Пропущено: {skipped}")
    print(f"\nДальше: обучай Piper на этом датасете.")
    print(f"См. EDGE_README.md → раздел «Обучение Piper».")


if __name__ == "__main__":
    main()
