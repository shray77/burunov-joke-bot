#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
# setup_xtts_jetson.sh — установка XTTS v2 (голос Бурунова) на борту G1.
# ─────────────────────────────────────────────────────────────────────
# Бортовой ПК G1 — NVIDIA Jetson (kernel *-tegra, ARM64), а НЕ x86, как
# считалось в изначальном плане (см. diag_v2.sh на 2026-07-08: kernel
# 5.10.104-tegra → L4T R35.x → JetPack 5.1.x). Это значит:
#   - на борту РЕАЛЬНО ЕСТЬ CUDA-совместимый GPU — XTTS может идти live,
#     без нужды в облегчённой CPU-only замене.
#   - НО обычный `pip install torch` с PyPI НЕ ВСТАНЕТ — там x86_64-сборки.
#     Нужен ARM64-wheel от NVIDIA под конкретную версию JetPack.
#
# Работает под обычным пользователем (НЕ root) — все пути через $HOME,
# apt через sudo. Проверено на реальном железе: L4T R35.3.1 (JetPack
# 5.1.1), python3.8, CUDA 11.4 установлена, но системный torch 2.4.1
# оказался CPU-only сборкой с PyPI (без суффикса +nv24.xx) — этот
# скрипт ставит правильный CUDA-torch в отдельный venv, не трогая
# системный python (там висит ROS2/YOLO26, не хотим ничего ломать).
#
# Запуск на самом G1 (после ssh):
#   bash setup_xtts_jetson.sh
# ─────────────────────────────────────────────────────────────────────
set -e
WORKDIR="$HOME/burunov-workspace"
LOGFILE="$HOME/burunov_xtts_jetson_setup.log"
exec > >(tee -a "$LOGFILE") 2>&1
echo "=== Burunov XTTS Jetson setup started at $(date) (user: $(whoami), home: $HOME) ==="

# ─── 0. Какой на самом деле JetPack/L4T ─────────────────────────────
echo "=== JetPack / L4T версия ==="
if [ -f /etc/nv_tegra_release ]; then
    cat /etc/nv_tegra_release
else
    echo "⚠️ /etc/nv_tegra_release не найден — точно ли это Jetson? Прерываю."
    exit 1
fi
PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Системный python3: $PYVER"
echo ""
echo "⚠️ ПРОВЕРЬ ВРУЧНУЮ: сверь L4T-версию выше со списком тут —"
echo "   https://developer.download.nvidia.com/compute/redist/jp/"
echo "   и поправь TORCH_INDEX_JP ниже, если версия не v51 (JetPack 5.1.x)."
echo "   Wheel собран под конкретный python3.X (cp3X) — если у тебя не 3.8,"
echo "   ищи соответствующий wheel в том же индексе, не ставь v51/cp38 вслепую."

# ─── 1. Системные пакеты (через sudo — мы НЕ root) ───────────────────
sudo apt-get update -y
sudo apt-get install -y \
    python3-pip python3-venv \
    libopenblas-dev \
    git ffmpeg espeak-ng build-essential wget curl

mkdir -p "$WORKDIR"
cd "$WORKDIR"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip wheel setuptools

# ─── 2. PyTorch — ARM64 wheel от NVIDIA (НЕ с обычного PyPI) ─────────
# Официальная дока: https://docs.nvidia.com/deeplearning/frameworks/install-pytorch-jetson-platform/
# jp/v51 = JetPack 5.1.x, cu114 = CUDA 11.4 (подтверждено на этом железе).
TORCH_INDEX_JP="v51"
echo "=== Установка PyTorch (JetPack index: $TORCH_INDEX_JP) ==="
# numpy==1.26.1 (из доки NVIDIA) требует Python>=3.9 — на этом железе
# python3.8.10 (JetPack 5.1.1), доступный максимум для 3.8 — 1.24.x.
pip install "numpy<1.25,>=1.22"

# Пытаемся через community-индекс jetson-ai-lab (проще, но не 100% что
# путь актуален для JP5 на момент реального запуска — если 404, см. fallback).
if pip install --index-url "https://pypi.jetson-ai-lab.io/jp5/cu114" torch torchaudio 2>/tmp/torch_install.log; then
    echo "✅ torch встал через jetson-ai-lab индекс"
else
    echo "⚠️ jetson-ai-lab индекс не сработал, пробуем официальный NVIDIA wheel напрямую"
    cat /tmp/torch_install.log
    echo "Найди актуальный .whl тут для своей связки JetPack+python3:"
    echo "  https://developer.download.nvidia.com/compute/redist/jp/${TORCH_INDEX_JP}/pytorch/"
    echo "и поставь руками:"
    echo "  pip install --no-cache <URL_НА_WHL_ФАЙЛ>"
    exit 1
fi

echo "=== Проверка CUDA ==="
python3 -c "import torch; print(f'torch {torch.__version__}, CUDA: {torch.cuda.is_available()}')"

# ─── 3. coqui-tts + фикс transformers (те же грабли, что в Colab) ────
echo "=== Установка coqui-tts ==="
pip uninstall -y TTS tts 2>&1 | tail -3 || true
pip install coqui-tts 2>&1 | tail -5
# coqui-tts 0.27.x требует transformers>=4.57, но >=5.0 сам ломает XTTS
# (isin_mps_friendly удалён). См. burunov-joke-bot/colab_xtts_v2.ipynb —
# ровно та же пара ограничений, что мы нашли и проверили на Colab.
pip install "transformers>=4.57,<5.0" 2>&1 | tail -5
pip install soundfile

# ─── 4. Смоук-тест: синтез на референсе Бурунова + замер RTF на Jetson ──
echo "=== Смоук-тест XTTS на Jetson ==="
DATA_DIR="$HOME/burunov-joke-bot-data"
git clone --depth 1 https://github.com/shray77/burunov-joke-bot.git "$DATA_DIR" 2>&1 | tail -3
REF_WAV=$(find "$DATA_DIR/data/preset_wav" -maxdepth 1 -name "-*.wav" | head -1)
echo "Референс: $REF_WAV"

WORKDIR="$WORKDIR" DATA_DIR="$DATA_DIR" python3 - <<'PYEOF'
import os, time, glob
os.environ['COQUI_TOS_AGREED'] = '1'
import torch
from TTS.api import TTS

data_dir = os.environ['DATA_DIR']
workdir = os.environ['WORKDIR']
ref = glob.glob(f'{data_dir}/data/preset_wav/-*.wav')[0]
device = 'cuda' if torch.cuda.is_available() else 'cpu'
print(f'Device: {device}')

print('Загрузка XTTS v2...')
t0 = time.time()
tts = TTS(model_name='tts_models/multilingual/multi-dataset/xtts_v2').to(device)
print(f'Модель загружена за {time.time()-t0:.1f}с')

text = 'Вот ваш кофе, Олег. Не обожгись, бля.'
out = f'{workdir}/smoke_test.wav'
t0 = time.time()
tts.tts_to_file(text=text, speaker_wav=ref, language='ru', file_path=out)
synth_time = time.time() - t0

import soundfile as sf
audio, sr = sf.read(out)
audio_dur = len(audio) / sr
rtf = synth_time / audio_dur
print(f'\n=== РЕЗУЛЬТАТ ===')
print(f'Синтез: {synth_time:.2f}с для {audio_dur:.2f}с аудио')
print(f'RTF на Jetson: {rtf:.2f}')
if rtf < 1.0:
    print('✅ real-time — можно юзать live на демо')
elif rtf < 2.0:
    print('🟡 небольшая задержка — терпимо, но лучше пресеты для длинных фраз')
else:
    print('🔴 медленно — оставайся на пресетах (data/preset_wav/), live не тянет')
print(f'Файл сохранён: {out}')
PYEOF

echo ""
echo "=== Готово. Проверь $WORKDIR/smoke_test.wav и RTF выше ==="
