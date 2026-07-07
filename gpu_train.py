"""
gpu_train.py
────────────
One-shot скрипт для обучения TTS на GPU-сервере (A100 40GB).

Запускать НА GPU-СЕРВЕРЕ (не на G1, не на ноуте):
  ssh root@<server_ip>
  cd /root/burunov-workspace/burunov-joke-bot
  source /root/burunov-workspace/venv/bin/activate
  python gpu_train.py

Что делает:
  1. Подготовка аудио Бурунова (Whisper + demucs)
  2. Fine-tune GPT-SoVITS → burunov.ckpt + burunov.pth
  3. Обучение Piper → burunov.onnx + burunov.onnx.json
  4. Упаковка моделей в models.zip для скачивания

────────────────────────────────────────────────────────────────────────
ТРЕБОВАНИЯ:
  - Аудио Бурунова в /root/burunov-workspace/burunov_raw/ (mp4/wav/mp3)
  - GPU с 16+ ГБ VRAM (A100 40GB — идеально)
  - 30-60 минут для GPT-SoVITS, 2-3 часа для Piper
"""
import os
import sys
import subprocess
import shutil
import time
from pathlib import Path

# ─── Конфигурация ────────────────────────────────────────────────────
WORKSPACE = Path("/root/burunov-workspace")
RAW_AUDIO_DIR = WORKSPACE / "burunov_raw"
TRAINING_DIR = WORKSPACE / "burunov_training"
OUTPUT_DIR = WORKSPACE / "models_output"
GPT_SOVITS_DIR = WORKSPACE / "GPT-SoVITS"
REPO_DIR = WORKSPACE / "burunov-joke-bot"

# Сколько эпох обучать
GPT_SOVITS_EPOCHS = 10     # 8-12 норма для клонирования
PIPER_EPOCHS = 3000        # VITS — тысячи шагов

# Сколько аудио в минутах нужно минимум
MIN_AUDIO_MIN = 5
IDEAL_AUDIO_MIN = 15


def log(msg: str, level: str = "INFO"):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def run(cmd: list[str] | str, cwd: Path = None, check: bool = True):
    """Запуск команды с логированием."""
    if isinstance(cmd, str):
        log(f"$ {cmd}")
        shell = True
    else:
        log(f"$ {' '.join(cmd)}")
        shell = False
    result = subprocess.run(
        cmd, cwd=cwd, shell=shell, check=check,
        capture_output=False,
    )
    return result.returncode == 0


def check_environment():
    """Проверка окружения."""
    log("=== Проверка окружения ===")

    # GPU
    import torch
    if not torch.cuda.is_available():
        log("❌ CUDA недоступна! Проверь nvidia-smi", "ERROR")
        sys.exit(1)
    log(f"✅ GPU: {torch.cuda.get_device_name(0)}")
    log(f"   VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")

    # Аудио
    audio_files = list(RAW_AUDIO_DIR.glob("*"))
    audio_files = [f for f in audio_files if f.suffix.lower() in
                   (".mp4", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".mkv", ".mov")]
    if not audio_files:
        log(f"❌ Нет аудио в {RAW_AUDIO_DIR}", "ERROR")
        log("   Положи туда mp4/mp3/wav с голосом Бурунова", "ERROR")
        sys.exit(1)
    log(f"✅ Аудио файлов: {len(audio_files)}")
    for f in audio_files:
        log(f"   - {f.name} ({f.stat().st_size / 1e6:.1f} MB)")


def stage_1_prepare_audio():
    """Этап 1: Подготовка аудио через audio_prep.py."""
    log("\n" + "="*60)
    log("ЭТАП 1: Подготовка аудио Бурунова")
    log("="*60)

    # Запускаем audio_prep.py из нашего репо
    os.chdir(REPO_DIR)

    # Создаём символьную ссылку чтобы audio_prep.py нашёл аудио
    # audio_prep.py ожидает data/burunov_raw/
    raw_link = REPO_DIR / "data" / "burunov_raw"
    raw_link.parent.mkdir(parents=True, exist_ok=True)
    if not raw_link.exists():
        raw_link.symlink_to(RAW_AUDIO_DIR)

    log("Запуск audio_prep.py (Whisper + demucs, ~15-30 мин)...")
    t0 = time.time()
    ok = run("python audio_prep.py", cwd=REPO_DIR, check=False)
    elapsed = time.time() - t0
    log(f"audio_prep.py завершён за {elapsed/60:.1f} мин (ok={ok})")

    if not ok:
        log("⚠️ audio_prep.py завершился с ошибкой, но продолжаем", "WARN")

    # Проверяем результат
    training_files = list((REPO_DIR / "data" / "burunov_training").glob("*.wav"))
    log(f"Подготовлено wav-кусков: {len(training_files)}")

    if len(training_files) < 20:
        log("❌ Слишком мало кусков (<20). Нужно больше аудио Бурунова", "ERROR")
        sys.exit(1)

    # Считаем общую длительность
    total_sec = 0
    import wave
    for wav in training_files:
        try:
            with wave.open(str(wav), "rb") as w:
                total_sec += w.getnframes() / w.getframerate()
        except Exception:
            pass
    log(f"Общая длительность: {total_sec/60:.1f} мин")

    return training_files


def stage_2_train_gpt_sovits(training_files):
    """Этап 2: Fine-tune GPT-SoVITS."""
    log("\n" + "="*60)
    log("ЭТАП 2: Fine-tune GPT-SoVITS")
    log("="*60)

    os.chdir(GPT_SOVITS_DIR)

    # GPT-SoVITS использует WebUI для обучения, но можно через API.
    # Самый надёжный путь — запустить WebUI и нажать кнопки.
    # Но для автоматизации используем прямой вызов скриптов.

    # 1. Подготовка датасета для GPT-SoVITS
    log("1. Конвертация датасета в формат GPT-SoVITS...")
    gpt_dataset = GPT_SOVITS_DIR / "dataset" / "burunov"
    gpt_dataset.mkdir(parents=True, exist_ok=True)

    # Копируем подготовленные wav-ки
    for wav in training_files:
        txt = wav.with_suffix(".txt")
        shutil.copy(wav, gpt_dataset / wav.name)
        if txt.exists():
            shutil.copy(txt, gpt_dataset / txt.name)

    # 2. Запуск WebUI в headless-режиме (или прямые скрипты)
    # GPT-SoVITS WebUI запускается на :9874
    # Можно либо открыть в браузере (через SSH-туннель), либо использовать API
    log("\n2. Запуск GPT-SoVITS WebUI...")
    log("   Открой http://<server_ip>:9874 в браузере через SSH-туннель:")
    log("   ssh -L 9874:localhost:9874 root@<server_ip>")
    log("")
    log("   В WebUI:")
    log("   1. Укажи dataset: /root/burunov-workspace/GPT-SoVITS/dataset/burunov/")
    log(f"   2. Epochs: {GPT_SOVITS_EPOCHS}")
    log("   3. Batch size: 12 (A100 40GB тянет)")
    log("   4. Запусти обучение SoVITS, потом GPT")
    log("")
    log("   Или дождись автоматического запуска ниже...")
    log("")

    # Пробуем запустить WebUI в фоне
    log("Запускаю GPT-SoVITS WebUI в фоне (порт 9874)...")
    webui_proc = subprocess.Popen(
        ["python", "webui.py"],
        cwd=GPT_SOVITS_DIR,
        stdout=open("/var/log/gpt_sovits_webui.log", "w"),
        stderr=subprocess.STDOUT,
    )

    log(f"WebUI PID: {webui_proc.pid}")
    log("Открой http://localhost:9874 (через SSH-туннель) для обучения вручную.")
    log("")
    log("Или жди — будет попытка автоматического обучения через API...")
    time.sleep(30)  # даём WebUI подняться

    # TODO: автоматическое обучение через API GPT-SoVITS
    # Пока что — ручное обучение через WebUI, потому что API нестабилен

    log("\nПосле обучения в WebUI скопируй модели:")
    log("  GPT_weights/burunov-e10.ckpt → /root/burunov-workspace/models_output/")
    log("  SoVITS_weights/burunov-e10.pth → /root/burunov-workspace/models_output/")
    log("")
    log("Нажми Enter когда закончишь обучение в WebUI...")
    input()

    # Проверяем что модели появились
    gpt_ckpt = list((GPT_SOVITS_DIR / "GPT_weights").glob("burunov*.ckpt"))
    sovits_pth = list((GPT_SOVITS_DIR / "SoVITS_weights").glob("burunov*.pth"))

    if not gpt_ckpt or not sovits_pth:
        log("❌ Модели GPT-SoVITS не найдены после обучения", "ERROR")
        return False

    # Копируем в output
    shutil.copy(gpt_ckpt[0], OUTPUT_DIR / "burunov.ckpt")
    shutil.copy(sovits_pth[0], OUTPUT_DIR / "burunov.pth")
    log(f"✅ GPT-SoVITS модели скопированы в {OUTPUT_DIR}")
    return True


def stage_3_train_piper(training_files):
    """Этап 3: Обучение Piper (edge fallback)."""
    log("\n" + "="*60)
    log("ЭТАП 3: Обучение Piper VITS (edge fallback)")
    log("="*60)

    os.chdir(REPO_DIR)

    # 1. Конвертация в LJSpeech-формат через piper_train_prep.py
    log("1. Конвертация в LJSpeech-формат...")
    ok = run("python piper_train_prep.py", cwd=REPO_DIR, check=False)
    if not ok:
        log("⚠️ piper_train_prep.py завершился с ошибкой", "WARN")

    piper_dataset = REPO_DIR / "data" / "piper_dataset"
    if not piper_dataset.exists():
        log("❌ Датасет Piper не создан", "ERROR")
        return False

    # 2. Обучение Piper
    log("\n2. Запуск обучения Piper...")
    log(f"   Epochs: {PIPER_EPOCHS}")
    log("   Это займёт 2-3 часа на A100")

    # Piper обучается через команду piper train
    # Сначала генерируем конфиг
    log("Генерация конфига Piper...")
    run(
        f"piper-phonemize --dataset {piper_dataset} --language ru_RU",
        cwd=REPO_DIR, check=False
    )

    # Обучение
    log("Запуск обучения (лог: /var/log/piper_train.log)...")
    train_cmd = [
        "piper", "train",
        "--dataset-dir", str(piper_dataset),
        "--config", "ru_RU-default.conf",
        "--quality", "medium",
        "--epochs", str(PIPER_EPOCHS),
    ]
    with open("/var/log/piper_train.log", "w") as logf:
        proc = subprocess.run(
            train_cmd, cwd=REPO_DIR,
            stdout=logf, stderr=subprocess.STDOUT,
        )

    if proc.returncode != 0:
        log("❌ Обучение Piper не удалось", "ERROR")
        log("   См. /var/log/piper_train.log", "ERROR")
        return False

    # Ищем выходные модели
    onnx_files = list(piper_dataset.rglob("*.onnx"))
    onnx_json = list(piper_dataset.rglob("*.onnx.json"))

    if not onnx_files:
        log("❌ Piper .onnx не найден после обучения", "ERROR")
        return False

    shutil.copy(onnx_files[0], OUTPUT_DIR / "burunov.onnx")
    if onnx_json:
        shutil.copy(onnx_json[0], OUTPUT_DIR / "burunov.onnx.json")

    log(f"✅ Piper модель скопирована в {OUTPUT_DIR}")
    return True


def stage_4_pack_models():
    """Упаковка моделей в zip."""
    log("\n" + "="*60)
    log("ЭТАП 4: Упаковка моделей")
    log("="*60)

    import zipfile
    zip_path = WORKSPACE / "models.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in OUTPUT_DIR.iterdir():
            if f.is_file():
                zf.write(f, f.name)
                log(f"  + {f.name} ({f.stat().st_size/1e6:.1f} MB)")

    log(f"\n✅ Модели упакованы: {zip_path}")
    log(f"   Размер: {zip_path.stat().st_size/1e6:.1f} MB")
    log("")
    log("Скачай models.zip с сервера на свой ноут:")
    log(f"  scp root@<server_ip>:{zip_path} ./")
    log("")
    log("Потом закинь на G1:")
    log("  scp models.zip unitree@192.168.123.161:~/burunov/")
    log("  ssh unitree@192.168.123.161")
    log("  cd ~/burunov && unzip models.zip -d models/")


def main():
    log("╔══════════════════════════════════════════════════════════════╗")
    log("║  Burunov Bot — GPU Training Pipeline                       ║")
    log("║  Клон голоса Сергея Бурунова (GPT-SoVITS + Piper)         ║")
    log("╚══════════════════════════════════════════════════════════════╝")
    log("")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 0. Проверки
    check_environment()

    # 1. Подготовка аудио
    training_files = stage_1_prepare_audio()

    # 2. GPT-SoVITS (основная модель)
    gpt_ok = stage_2_train_gpt_sovits(training_files)

    # 3. Piper (edge fallback)
    piper_ok = stage_3_train_piper(training_files)

    # 4. Упаковка
    if gpt_ok or piper_ok:
        stage_4_pack_models()
        log("\n🎉 Обучение завершено!")
        log(f"   GPT-SoVITS: {'✅' if gpt_ok else '❌'}")
        log(f"   Piper:      {'✅' if piper_ok else '❌'}")
    else:
        log("\n❌ Обе модели не обучились", "ERROR")
        sys.exit(1)


if __name__ == "__main__":
    main()
