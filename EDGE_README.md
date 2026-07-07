# Edge-деплой на Unitree G1

Инструкция по запуску всей системы прямо на роботе G1 — без облака,
без WiFi-зависимости. Идеально для демо на хакатоне.

## Архитектура

```
┌─────────────────────────────────────────────────────┐
│ Unitree G1 — Main PC (Ubuntu)                       │
│                                                     │
│  Docker compose:                                    │
│   • rag:8000    (Gemma + ChromaDB + FastAPI)        │
│   • tts:8001    (Piper ONNX, CPU-only)              │
│                                                     │
│  Robot controller (вне docker):                     │
│   • robot_controller.py                             │
│   • unitree_gestures.py                             │
│                                                     │
│  Ollama (host, не в docker):                        │
│   • gemma3:4b                                        │
│                                                     │
│  Аудио: USB-динамик + микрофон (у шеи)              │
└─────────────────────────────────────────────────────┘
```

## Чек-лист перед стартом

Подключись к G1 по SSH и проверь железо:

```bash
ssh unitree@<G1_IP>

# 1. ОС и архитектура
uname -a
lsb_release -a
lscpu | grep -E "Model name|Architecture"

# 2. RAM (нужно ≥8 ГБ, желательно 16+)
free -h

# 3. Python
python3 --version
pip3 --version

# 4. Docker
docker --version
docker compose version

# 5. Ollama (если нет — поставим ниже)
which ollama || echo "не установлена"

# 6. Аудиоустройства
arecord -l   # микрофоны
aplay -l     # динамики
pactl list short sinks

# 7. Unitree SDK
pip3 list | grep -i unitree

# 8. GPU (опционально)
nvidia-smi 2>/dev/null || echo "GPU не найден (норм для G1)"
```

Сохрани вывод — пригодится для дебага.

## Этап 1. Установка на G1 (один раз)

### 1.1. Docker
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Перелогинься
```

### 1.2. Ollama + Gemma
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gemma3:4b
# Проверка:
ollama run gemma3:4b "Привет"
```

### 1.3. System-зависимости для аудио
```bash
sudo apt update
sudo apt install -y \
    portaudio19-dev python3-pyaudio \
    espeak-ng ffmpeg \
    alsa-utils pulseaudio
```

### 1.4. Unitree SDK (если не установлен)
```bash
git clone https://github.com/unitreerobotics/unitree_sdk2_python.git
cd unitree_sdk2_python
pip3 install -e .
```

## Этап 2. Подготовка моделей (на твоём ноуте с GPU)

### 2.1. Аудио Бурунова → датасет
```bash
# Положить сырые аудио в data/burunov_raw/
pip install openai-whisper torch torchaudio demucs
python audio_prep.py
# → data/burunov_training/ с wav+txt парами
```

### 2.2. Конвертация под Piper
```bash
python piper_train_prep.py
# → data/piper_dataset/ с metadata.csv + audio_files/
```

### 2.3. Обучение Piper (нужен GPU, ~2-4 часа)
```bash
pip install piper-tts piper-phonemize

# Создать конфиг модели:
piper-phonemize --dataset data/piper_dataset --language ru_RU

# Обучить (подробности в доке Piper):
# https://github.com/rhasspy/piper/blob/master/TRAINING.md
piper train \
    --dataset-dir data/piper_dataset \
    --config ru_RU-default.conf \
    --quality medium \
    --epochs 5000

# На выходе: burunov.onnx + burunov.onnx.json
```

Альтернатива — обучение через [piper-training](https://github.com/rhasspy/piper/blob/master/TRAINING.md) на Google Colab (бесплатный T4, ~6 часов).

### 2.4. RAG-датасет
```bash
# Попроси друга-скраппера положить data/jokes_raw.json
python prepare_jokes.py
python build_vector_db.py
# → data/chroma_db/
```

## Этап 3. Деплой на G1

### 3.1. Копирование на G1
```bash
# С твоего ноута:
G1_IP=192.168.1.100   # IP робота в сети

# Код
scp -r scripts/ unitree@$G1_IP:~/burunov/

# Данные (ChromaDB + piper-модель)
scp -r data/chroma_db unitree@$G1_IP:~/burunov/data/
scp -r models/burunov.onnx models/burunov.onnx.json unitree@$G1_IP:~/burunov/models/
```

### 3.2. Запуск на G1
```bash
ssh unitree@$G1_IP
cd ~/burunov

# Поднять RAG + TTS в docker
docker compose up -d --build
docker compose ps

# Проверить здоровье
curl http://localhost:8000/health
curl http://localhost:8001/health
```

### 3.3. Найти индекс USB-динамика
```bash
# Запустить без аргумента — покажет список устройств
python3 robot_controller.py --list-devices
```
Найди в списке USB-колонку (часто "USB Audio Device" или "Default Audio Device") и впиши её индекс в `OUTPUT_DEVICE_INDEX` в `robot_controller.py`.

### 3.4. Запуск оркестратора
```bash
# Тест с конкретной темой:
python3 robot_controller.py "Штирлиц и Мюллер"

# Интерактивный режим (через stdin):
python3 robot_controller.py --interactive
```

## Этап 4. Демо-режим

### Запуск как systemd service (чтобы жил между ребутами)
```bash
sudo tee /etc/systemd/system/burunov.service > /dev/null <<'EOF'
[Unit]
Description=Burunov Joke Bot
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=unitree
WorkingDirectory=/home/unitree/burunov
ExecStart=/usr/bin/python3 robot_controller.py --http
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable burunov
sudo systemctl start burunov
```

### HTTP-режим (для управления с телефона/ноута)
Раскомментируй в `robot_controller.py` секцию HTTP-сервера, подними на G1:8002.
С телефона в той же сети: `http://<G1_IP>:8002` → вводишь тему → жмёшь "Tell" → робот шутит.

## Этап 5. Fallback на случай сбоя

Если RAG или TTS упадут во время демо:

```bash
# Предгенерированные wav-ки на популярные темы (заранее):
mkdir -p fallback_audio
python3 generate_fallback.py   # см. ниже

# robot_controller при ошибке возьмёт случайный из fallback_audio/
```

Популярные темы для fallback (10 штук хватит):
- Штирлиц
- Вовочка
- Колбаса
- Очередь
- Горбачёв
- Перестройка
- Колхоз
- Профсоюз
- Дефицит
- Гаишник

## Частые проблемы

| Симптом | Решение |
|---|---|
| `Ollama connection refused` | `ollama serve` не запущен на G1. `sudo systemctl start ollama` |
| `audio device not found` | `python3 robot_controller.py --list-devices`, подбери индекс |
| Piper: `espeak not found` | `sudo apt install espeak-ng` |
| Docker build OOM | RAM < 4 ГБ. Увеличь swap: `sudo fallocate -l 4G /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile` |
| Gemma медленная | Поставь Q4-квантизацию через Ollama: `ollama pull gemma3:4b-q4_0` |
| Русский текст кракозябрами | `export LANG=ru_RU.UTF-8` в окружении |
| Жесты не работают | `unitree_sdk2_python` не установлен. Бот работает без жестов в silent-режиме |
| Длинная пауза перед речью | Gemma на CPU думает 10-20 сек. Юзай fallback на предгенерённые wav |

## Если совсем нет времени / не работает Piper

Switch на GPT-SoVITS в server-режиме (ноут с GPU рядом):

```bash
# В config.py:
TTS_MODE = "server"   # вместо "edge"

# В robot_controller.py:
TTS_HOST = "http://<твой_ноут_IP>:8001"
```

Робот дёргает TTS с твоего ноута по WiFi. Менее эффектно для жюри, но надёжнее если Piper не успели обучить.
