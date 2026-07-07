"""
audio_prep.py
─────────────
Подготовка аудио Сергея Бурунова для fine-tune GPT-SoVITS.

Пайплайн:
  1. Берёт сырые аудио/видео из data/burunov_raw/
  2. Нарезает на куски 5-30 сек (через VAD — silence-based)
  3. Выделяет вокал (demucs убирает музыку/шум) — если есть ffmpeg
  4. Нормализует: моно, 16kHz, 16-bit wav
  5. Транскрибирует через Whisper (medium для RU)
  6. Сохраняет в data/burunov_training/ с .wav + .txt (транскрипт)

Итоговая структура:
  data/burunov_training/
    001.wav / 001.txt
    002.wav / 002.txt
    ...

Эту папку потом скармливаешь GPT-SoVITS WebUI → fine-tune.

────────────────────────────────────────────────────────────────────────
Установка (тяжёлые зависимости, ставь отдельно):
  pip install openai-whisper torch torchaudio
  pip install demucs          # для выделения вокала
  # ffmpeg должен стоять в системе

Запуск:
  python audio_prep.py
"""
import json
import subprocess
from pathlib import Path

import config

RAW_DIR = config.DATA_DIR / "burunov_raw"
OUT_DIR = config.DATA_DIR / "burunov_training"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def check_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def extract_vocal(input_path: Path, output_path: Path) -> bool:
    """
    Demucs: htdemucs — модель для разделения на vocal/drums/bass/other.
    Возвращает True если вокал извлечён.
    """
    print(f"  → demucs: {input_path.name}")
    try:
        subprocess.run(
            [
                "demucs",
                "--two-stems", "vocals",   # вокал / всё остальное
                "--name", "htdemucs",
                "-o", str(OUT_DIR / "_demucs_tmp"),
                str(input_path),
            ],
            check=True,
            capture_output=True,
        )
        # demucs кладёт вокал в _demucs_tmp/htdemucs/<name>/vocals.wav
        vocal = OUT_DIR / "_demucs_tmp" / "htdemucs" / input_path.stem / "vocals.wav"
        if vocal.exists():
            vocal.rename(output_path)
            return True
    except subprocess.CalledProcessError as e:
        print(f"  demucs failed: {e.stderr.decode()[:200]}")
    return False


def normalize_audio(input_path: Path, output_path: Path) -> None:
    """Моно, 16kHz, 16-bit PCM wav — стандарт для GPT-SoVITS."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(input_path),
            "-ac", "1",            # моно
            "-ar", "16000",        # 16 kHz
            "-sample_fmt", "s16",  # 16-bit
            "-af", "loudnorm=I=-20:TP=-3:LRA=7",  # нормализация громкости
            str(output_path),
        ],
        check=True,
        capture_output=True,
    )


def transcribe_whisper(audio_path: Path) -> str:
    """
    Whisper medium — лучший баланс RU-качества/скорости.
    Если GPU нет — поставь 'small' или 'base'.
    """
    import whisper
    if not hasattr(transcribe_whisper, "_model"):
        transcribe_whisper._model = whisper.load_model("medium")
    result = transcribe_whisper._model.transcribe(
        str(audio_path),
        language="ru",
        task="transcribe",
        verbose=False,
    )
    text = result["text"].strip()
    # Убираем типичные артефакты whisper
    text = text.replace("[music]", "").replace("[Music]", "")
    text = text.replace("[аплодисменты]", "").replace("[Applause]", "")
    return text.strip()


def split_by_silence(input_path: Path, out_prefix: Path) -> list[Path]:
    """
    Нарезаем на куски через ffmpeg silencedetect.
    Порог: -30dB, мин пауза 0.5 сек.
    Целевая длина куска: 5-30 сек.
    """
    # 1. Найти тишины
    proc = subprocess.run(
        [
            "ffmpeg", "-i", str(input_path),
            "-af", "silencedetect=noise=-30dB:d=0.5",
            "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    starts, ends = [], []
    for line in proc.stderr.splitlines():
        if "silence_start" in line:
            starts.append(float(line.split("silence_start:")[1].strip()))
        elif "silence_end" in line:
            ends.append(float(line.split("silence_end:")[1].split()[0]))

    # 2. Сегменты = между концом одной тишины и началом следующей
    segments = []
    prev_end = 0.0
    for s, e in zip(starts, ends):
        if s - prev_end > 3.0:    # мин 3 сек
            segments.append((prev_end, s))
        prev_end = e
    # Хвост
    if prev_end < get_duration(input_path):
        segments.append((prev_end, get_duration(input_path)))

    # 3. Склеиваем слишком короткие, режем слишком длинные
    out_paths = []
    for i, (start, end) in enumerate(segments):
        duration = end - start
        if duration < 3 or duration > 35:
            continue
        out = out_prefix.parent / f"{out_prefix.name}_{i:03d}.wav"
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(input_path),
                "-ss", str(start), "-to", str(end),
                "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
                str(out),
            ],
            check=True, capture_output=True,
        )
        out_paths.append(out)
    return out_paths


def get_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def main():
    if not check_ffmpeg():
        print("ERROR: ffmpeg не установлен. Поставь: apt install ffmpeg / brew install ffmpeg")
        return

    raw_files = list(RAW_DIR.glob("*"))
    raw_files = [f for f in raw_files if f.suffix.lower() in
                 (".mp4", ".mkv", ".mov", ".mp3", ".wav", ".m4a", ".aac", ".flac")]
    if not raw_files:
        print(f"Положи сырые аудио/видео Бурунова в: {RAW_DIR}")
        print("Источники: реклама Билайна, 'Громовы', 'Кухня', интервью, озвучка")
        return

    print(f"Найдено файлов: {len(raw_files)}")
    print(f"Выходная папка: {OUT_DIR}\n")

    counter = 1
    manifest = []

    for raw in raw_files:
        print(f"\n=== {raw.name} ===")

        # 1. Выделяем вокал (если есть музыка — demucs)
        vocal_path = OUT_DIR / "_vocal_tmp" / f"{raw.stem}.wav"
        vocal_path.parent.mkdir(exist_ok=True)
        if not extract_vocal(raw, vocal_path):
            # Если demucs не сработал — берём как есть
            normalize_audio(raw, vocal_path)

        # 2. Нарезаем на куски
        pieces = split_by_silence(vocal_path, OUT_DIR / raw.stem)
        print(f"  Нарезано кусков: {len(pieces)}")

        # 3. Транскрибируем каждый кусок
        for piece in pieces:
            try:
                text = transcribe_whisper(piece)
            except Exception as e:
                print(f"  whisper failed on {piece.name}: {e}")
                piece.unlink()
                continue

            if len(text) < 10:
                piece.unlink()
                continue

            # Переименовываем в 001.wav, 002.wav, ...
            final_wav = OUT_DIR / f"{counter:03d}.wav"
            final_txt = OUT_DIR / f"{counter:03d}.txt"
            piece.rename(final_wav)
            final_txt.write_text(text, encoding="utf-8")

            manifest.append({
                "id": counter,
                "wav": str(final_wav.name),
                "text": text,
                "source": raw.name,
            })
            counter += 1

        # Чистим tmp
        vocal_path.unlink(missing_ok=True)

    # Сохраняем манифест
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Удаляем временные папки
    import shutil
    shutil.rmtree(OUT_DIR / "_demucs_tmp", ignore_errors=True)
    shutil.rmtree(OUT_DIR / "_vocal_tmp", ignore_errors=True)

    print(f"\n{'=' * 50}")
    print(f"ГОТОВО. Кусков для тренировки: {counter - 1}")
    print(f"Папка: {OUT_DIR}")
    print(f"Манифест: {OUT_DIR / 'manifest.json'}")
    print(f"\nДальше: закинь {OUT_DIR} в GPT-SoVITS WebUI для fine-tune.")


if __name__ == "__main__":
    main()
