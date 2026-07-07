# Edge-деплой на Unitree G1

Инструкция по запуску всей системы прямо на роботе G1 — без облака,
без WiFi-зависимости. Идеально для демо на хакатоне.

## Архитектура (ПРАВИЛЬНАЯ под unitree_sdk2)

```
┌─────────────────────────────────────────────────────┐
│ Unitree G1 — Main PC (Ubuntu)                       │
│                                                     │
│  Docker compose:                                    │
│   • rag:8000    (Gemma + ChromaDB + FastAPI)        │
│   • tts:8001    (Piper ONNX, CPU-only)              │
│     ↑ /synthesize_pcm → PCM 16kHz mono 16-bit       │
│                                                     │
│  Robot controller (вне docker):                     │
│   • robot_controller.py                             │
│   • unitree_audio.py    → AudioClient.PlayStream()  │
│   • unitree_gestures.py → LocoClient.Sit/StandUp    │
│   • unitree_hands.py    → HandClient (RH56DFTP)     │
│                                                     │
│  Ollama (host, не в docker):                        │
│   • gemma3:4b                                        │
│                                                     │
│  Аудио: встроенный динамик Stanley (8Ω 3W)          │
│  Микрофон: 4-мик решётка (для интерактива)          │
│  RGB-лента: 256 цветов (синхрон с речью)            │
└─────────────────────────────────────────────────────┘
```

## КРИТИЧНО: требования к прошивке G1

Перед стартом проверить через SSH на G1:

| Компонент | Мин версия | Зачем |
|---|---|---|
| **Vui_Service** | ≥ 2.0.3.8 | AudioClient API (PlayStream, LedControl) |
| **Vui Module** | ≥ 2.0.0.3 | ASR + TTS модуль |
| **Vul Service** | ≥ 2.0.4.4 | Audio playback через VuiClient |
| **Webrtc Bridge** | ≥ 1.0.7.5 | Streaming |
| **Audio Hub** | ≥ 1.0.1.0 | Audio routing |
| **Firmware (общая)** | ≥ 1.3.0 | GPT voice assistant |

Если версия ниже → AudioClient.PlayStream() не сработает.
Просить техподдержку Unitree обновить.

## Чек-лист перед стартом

Подключись к G1 по SSH и проверь:

```bash
ssh unitree@192.168.123.161   # IP по умолчанию

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

# 6. Unitree SDK2 Python
pip3 list | grep -i unitree

# 7. Сетевой интерфейс к G1
ip addr | grep "192.168.123"

# 8. Версии сервисов (если есть доступ к diag)
# Зависит от firmware — может быть в /opt/unitree/ или через app
ls /opt/unitree/ 2>/dev/null
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

### 1.3. Unitree SDK2 Python (КРИТИЧНО)
```bash
git clone https://github.com/unitreerobotics/unitree_sdk2_python.git
cd unitree_sdk2_python
pip3 install -e .
# Проверка:
python3 -c "from unitree_sdk2py.g1.audio.audio_client import AudioClient; print('OK')"
```

Если импорт не сработал — структура SDK может отличаться.
Проверь: `pip3 show unitree_sdk2_python` и путь к g1-модулям.

### 1.4. System-зависимости
```bash
sudo apt update
sudo apt install -y \
    python3-pip \
    ffmpeg \
    alsa-utils \
    portaudio19-dev
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
G1_IP=192.168.123.161

# Код
scp -r *.py Dockerfile docker-compose.yml requirements.txt \
    unitree@$G1_IP:~/burunov/

# Данные (ChromaDB + piper-модель)
scp -r data/chroma_db unitree@$G1_IP:~/burunov/data/
scp -r models/burunov.onnx models/burunov.onnx.json \
    unitree@$G1_IP:~/burunov/models/
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

### 3.3. Проверка AudioClient
```bash
# Тест SDK и AudioClient
python3 unitree_audio.py
# Должен:
#   - инициализировать AudioClient
#   - мигнуть RGB-лентой (синий → зелёный → красный)
#   - установить громкость 100
```

Если silent-режим (нет SDK или нет доступа к роботу) — увидишь
соответствующее сообщение, но код не упадёт.

### 3.4. Запуск оркестратора

```bash
# Один анекдот:
python3 robot_controller.py "Штирлиц"

# Интерактивный режим (ввод тем с клавиатуры):
python3 robot_controller.py --interactive

# HTTP-сервер для управления с телефона:
python3 robot_controller.py --http --port 8002
# Открой http://<G1_IP>:8002/docs с телефона
```

## Этап 4. Демо-режим

### Запуск как systemd service
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
ExecStart=/usr/bin/python3 robot_controller.py --http --port 8002
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable burunov
sudo systemctl start burunov
```

### HTTP-управление с телефона

После `--http` открывается API на 8002 порту:

```bash
# С телефона в той же WiFi-сети:
curl -X POST http://<G1_IP>:8002/tell \
  -H "Content-Type: application/json" \
  -d '{"topic": "Штирлиц"}'

# Стоп:
curl -X POST http://<G1_IP>:8002/stop

# Статус:
curl http://<G1_IP>:8002/health
```

## Этап 5. Fallback на случай сбоя

Если RAG или TTS упадут во время демо:

```bash
# Предгенерированные wav-ки на популярные темы (заранее):
mkdir -p fallback_audio
python3 generate_fallback.py   # (запланирован, ещё не написан)
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
| `unitree_sdk2_python` не импортируется | Переустановить: `cd unitree_sdk2_python && pip install -e .` |
| `AudioClient` не найден | Проверь `pip3 show unitree_sdk2_python`. Может называться иначе в новой версии |
| `PlayStream` возвращает ошибку | Проверь версии Vui_Service/Vul_Service (нужны новые) |
| Звук не идёт на динамик | Проверь `SetVolume(100)`, попробуй `TtsMaker("тест", 0)` для теста встроенного TTS |
| Лента не моргает | `LedControl` требует интервал > 200ms между вызовами |
| `Ollama connection refused` | `ollama serve` не запущен. `sudo systemctl start ollama` |
| Docker build OOM | RAM < 4 ГБ. Своп: `sudo fallocate -l 4G /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile` |
| Gemma медленная | Q4-квантизация: `ollama pull gemma3:4b-q4_0` |
| Русский текст кракозябрами | `export LANG=ru_RU.UTF-8` в окружении |
| LocoClient ошибки | Прошивка < 1.3.0 или робот в debug-режиме. `Start()` перед любым движением |
| Рука RH56DFTP не отвечает | Проверь питание, попробуй `HandClient.Init()` повторно |
| Длинная пауза перед речью | Gemma на CPU думает 10-20 сек. Юзай fallback на предгенерённые wav |

## Если совсем нет времени / не работает Piper

Switch на GPT-SoVITS в server-режиме (ноут с GPU рядом):

```python
# В config.py:
TTS_MODE = "server"   # вместо "edge"
TTS_HOST = "http://<твой_ноут_IP>:8001"
```

Робот дёргает TTS с твоего ноута по WiFi. Менее эффектно для жюри,
но надёжнее если Piper не успели обучить.

## Что читать в доках Unitree

Локально сохранено в `unitree_docs/`:
- `about_g1.json` — железо, степени свободы, руки
- `audio_playback.json` — спецификация WAV (16kHz, моно, ≤10МБ, ≤3 мин)
- `voice_assistant.json` — встроенный GPT-ассистент (можно не использовать)
- `vuiclient.json` — AudioClient API (PlayStream/LedControl/TtsMaker) — ГЛАВНОЕ
- `sport_services.json` — LocoClient (Sit/StandUp/Squat/Move)
- `dds_services.json` — DDS-транспорт

Онлайн: https://support.unitree.com/home/en/G1_developer/services_interface

