#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
# cloud-init скрипт для Selectel GPU-сервера (A100 40GB)
# ─────────────────────────────────────────────────────────────────────
# Вставить в поле "User data" при создании сервера.
# Скрипт выполнится при первом запуске и поставит всё нужное.
#
# После создания сервера (через 5-7 минут) можно подключаться:
#   ssh -i ~/.ssh/selectel_key root@<PUBLIC_IP>
#
# Проверка что всё встало:
#   nvidia-smi
#   cd /root/burunov-joke-bot && python3 -c "import torch; print(torch.cuda.is_available())"
# ─────────────────────────────────────────────────────────────────────

# Логируем в /var/log/cloud-init-output.log
exec > >(tee -a /var/log/burunov_setup.log) 2>&1
echo "=== Burunov Bot setup started at $(date) ==="

# ─── 1. Системные пакеты ────────────────────────────────────────────
apt-get update -y
apt-get install -y \
    python3.11 python3.11-venv python3-pip \
    git ffmpeg espeak-ng \
    build-essential \
    wget curl unzip \
    htop tmux \
    portaudio19-dev

# Альтернативно: python3 по умолчанию может быть 3.10 на Ubuntu 24.04
# но нам нужна свежая — ставим 3.11
if ! command -v python3.11 &> /dev/null; then
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -y
    apt-get install -y python3.11 python3.11-venv python3.11-dev
fi

# ─── 2. Проверка GPU ────────────────────────────────────────────────
echo "=== GPU check ==="
nvidia-smi
if [ $? -ne 0 ]; then
    echo "❌ nvidia-smi не работает — драйвер не установлен"
    exit 1
fi

# ─── 3. Создание рабочей директории ─────────────────────────────────
mkdir -p /root/burunov-workspace
cd /root/burunov-workspace

# ─── 4. Python-окружение ────────────────────────────────────────────
python3.11 -m venv venv
source venv/bin/activate

# Обновляем pip
pip install --upgrade pip wheel setuptools

# ─── 5. PyTorch с CUDA ──────────────────────────────────────────────
echo "=== Installing PyTorch with CUDA ==="
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121

# Проверка
python3 -c "import torch; print(f'PyTorch {torch.__version__}, CUDA: {torch.cuda.is_available()}, Device: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"CPU\"}')"

# ─── 6. GPT-SoVITS ──────────────────────────────────────────────────
echo "=== Installing GPT-SoVITS ==="
cd /root/burunov-workspace
git clone https://github.com/RVC-Boss/GPT-SoVITS.git
cd GPT-SoVITS
pip install -r requirements.txt

# Скачиваем предобученные базовые модели (нужны для fine-tune)
echo "=== Downloading GPT-SoVITS pretrained models ==="
mkdir -p GPT_SoVITS/pretrained_models
cd GPT_SoVITS/pretrained_models

# Базовые модели GPT-SoVITS (примерно 1.5 ГБ)
wget -q https://huggingface.co/lj1995/GPT-SoVITS/resolve/main/gsv-v2final-pretrained/s1bert25hz-5kh-longer-epoch=12-step=369668.pth -O s1bert25hz-5kh-longer-epoch=12-step=369668.pth || echo "warn: download failed, check HF"
wget -q https://huggingface.co/lj1995/GPT-SoVITS/resolve/main/gsv-v2final-pretrained/s2G488k.pth -O s2G488k.pth || echo "warn: download failed"
wget -q https://huggingface.co/lj1995/GPT-SoVITS/resolve/main/gsv-v2final-pretrained/s2D488k.pth -O s2D488k.pth || echo "warn: download failed"

cd /root/burunov-workspace

# ─── 7. Подготовка аудио: Whisper + demucs ──────────────────────────
echo "=== Installing Whisper and demucs ==="
pip install openai-whisper demucs

# ─── 8. Piper для второго трека (edge fallback) ─────────────────────
echo "=== Installing Piper TTS ==="
pip install piper-tts piper-phonemize

# ─── 9. Наш репозиторий ────────────────────────────────────────────
echo "=== Cloning burunov-joke-bot ==="
cd /root/burunov-workspace
git clone https://github.com/shray77/burunov-joke-bot.git
cd burunov-joke-bot
pip install -r requirements.txt

# ─── 10. Jupyter Lab для удобства ───────────────────────────────────
echo "=== Installing Jupyter Lab ==="
pip install jupyterlab

# ─── 11. Создание рабочей структуры ─────────────────────────────────
mkdir -p /root/burunov-workspace/burunov_raw
mkdir -p /root/burunov-workspace/burunov_training
mkdir -p /root/burunov-workspace/models_output

# ─── 12. Информационный файл ────────────────────────────────────────
cat > /root/burunov-workspace/README.md << 'EOF'
# Burunov Bot — рабочее окружение

## Что установлено
- Python 3.11 + venv в /root/burunov-workspace/venv
- PyTorch с CUDA 12.1
- GPT-SoVITS в /root/burunov-workspace/GPT-SoVITS
- Whisper + demucs (подготовка аудио)
- Piper TTS (edge fallback)
- Jupyter Lab

## Что делать дальше
1. Залить аудио Бурунова в /root/burunov-workspace/burunov_raw/
2. Активировать venv: source /root/burunov-workspace/venv/bin/activate
3. Запустить gpu_train.py из репо burunov-joke-bot

## Jupyter Lab
jupyter lab --ip=0.0.0.0 --port=8888 --allow-root --no-browser
EOF

# ─── 13. Финальная проверка ────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  УСТАНОВКА ЗАВЕРШЕНА: $(date)"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "GPU:"
nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv
echo ""
echo "Python:"
python3 -c "import torch; print(f'PyTorch: {torch.__version__}, CUDA: {torch.cuda.is_available()}')"
echo ""
echo "Disk usage:"
df -h / | tail -1
echo ""
echo "Готов к работе. См. /root/burunov-workspace/README.md"
echo "════════════════════════════════════════════════════════════════"
