# TTS: клон голоса Сергея Бурунова

Это инструкция по обучению и запуску TTS-блока. RAG-пайплайн (текст) описан
в `README.md`, тут только голосовая часть.

## Архитектура

```
[RAG API /tell → text]
        ↓
[tts_server.py  (FastAPI:8001 + GPT-SoVITS)]
        ↓ POST /synthesize {text} → wav bytes
        ↓ POST /stream        → чанки wav по предложениям (real-time)
[tts_client.py  — играет через pyaudio / стримит на G1]
        ↓
[Unitree G1 — динамик]
```

## Этап 1. Подготовка аудио Бурунова

### Источники (что качать)
- Реклама Билайна (Бурунов в Стиляге)
- Сериалы: «Кухня», «Громовы», «Гранд»
- Интервью на YouTube
- Озвучка аудиокниг/фильмов

Цель: **10-20 минут чистого голоса** без музыки и шума.

### Пайплайн подготовки
Положи сырые файлы в `data/burunov_raw/` (mp4/mp3/wav/m4a и т.д.) и запусти:

```bash
pip install openai-whisper torch torchaudio demucs
# ffmpeg тоже должен стоять

python audio_prep.py
```

Скрипт сделает:
1. Выделит вокал (demucs убирает музыку)
2. Нарежет на куски 3-30 сек по паузам тишины
3. Нормализует: моно, 16kHz, 16-bit
4. Транскрибирует через Whisper medium (RU)
5. Сложит в `data/burunov_training/` как `001.wav + 001.txt, 002.wav + 002.txt, ...`

После подготовки у тебя должна быть папка с 50-200 парами wav+txt.

## Этап 2. Fine-tune GPT-SoVITS

### Установка GPT-SoVITS
```bash
git clone https://github.com/RVC-Boss/GPT-SoVITS
cd GPT-SoVITS
pip install -r requirements.txt
```

Скачай pretrained модели с [HuggingFace](https://huggingface.co/lj1995/GPT-SoVITS)
и положи в `GPT-SoVITS/GPT_SoVITS/pretrained_models/`.

### Запуск WebUI
```bash
cd GPT-SoVITS
python webui.py
```

Открой http://localhost:9874 и сделай:

1. **0-Tab: GPT-SoVITS-TTS**
   - Путь к датасету: укажи `data/burunov_training/`
   - Experiment name: `burunov`
   - Target audio processing: 16k
   - Нажми "1-GPU" → начнётся извлечение признаков

2. **1-Tab: 1-B-Text processing**
   - Запусти транскрибацию (если ещё не через Whisper)
   - Или сразу переходи к训练, если txt-файлы уже есть

3. **2-Tab: 1-C-Training**
   - batch_size: 6 (для 4GB VRAM) / 12 (для 8GB+) / 24 (для 24GB)
   - Total epochs: **8-10** (больше = переобучение)
   - Save every: 2
   - Запусти "1-SoVITS" и потом "1-GPT" — оба обучаются

4. **После обучения** появятся:
   - `GPT_weights/burunov-e10.ckpt`
   - `SoVITS_weights/burunov-e10.pth`

5. **Подготовь референс** (5-10 сек чистого Бурунова):
   - `reference/burunov_ref.wav`
   - `reference/burunov_ref.txt` — что он там говорит

### Время обучения (ориентир)
- 100 кусков / 10 минут аудио / 10 эпох / **T4 GPU** → ~30-45 мин
- На RTX 3090 → ~10 мин
- На CPU → не реально, ищи GPU

## Этап 3. Запуск TTS-сервера

Пропиши пути к моделям в начале `tts_server.py`:

```python
CKPT_PATH = .../GPT_weights/burunov-e10.ckpt
SOVITS_PATH = .../SoVITS_weights/burunov-e10.pth
REF_AUDIO = .../reference/burunov_ref.wav
REF_TEXT = "..."  # что сказано в референсе
```

Запуск:
```bash
python tts_server.py
# или
uvicorn tts_server:app --host 0.0.0.0 --port 8001
```

Проверка:
```bash
curl http://localhost:8001/health
```

Тест синтеза:
```bash
curl -X POST http://localhost:8001/synthesize \
  -H "Content-Type: application/json" \
  -d '{"text": "Штирлиц подошёл к окну. Из окна дуло."}' \
  --output test.wav
```

## Этап 4. Клиент → Unitree G1

### Локальное воспроизведение (для тестов)
```bash
pip install pyaudio
python tts_client.py "Штирлиц"
```

### Real-time на G1

У робота G1 нет встроенного GPU для TTS. Схема такая:

```
[Твой ноут с GPU] ←→ [G1 через Wi-Fi/SDK]
   - TTS-сервер         - отправка аудиопотока
   - GPT-SoVITS         - на динамик робота
```

Способы вывода на G1 (выбери один):
1. **PulseAudio over network** — настроить G1 как сетевой sink
2. **Bluetooth-колонка** внутри G1 (если поддерживает)
3. **Unitree SDK streaming** — если в SDK есть аудиоканал
4. **HTTP-стрим** — на G1 крутится минимальный клиент, который тянет wav с твоего TTS-сервера

Самый надёжный для хакатона — **вариант 4**: на G1 простой Python-скрипт,
который раз в секунду дёргает твой TTS-сервер. См. `stream_to_speaker()` в
`tts_client.py` — там каркас, допиливаешь под свой способ вывода.

## Комбинированный эндпоинт (для демо)

В `tts_server.py` есть `/tell_voice` — одна ручка на всё:

```bash
curl -X POST http://localhost:8001/tell_voice \
  -H "Content-Type: application/json" \
  -d '{"topic": "Штирлиц"}' \
  --output joke.wav
```

Это: тема → RAG (Gemma) → текст в стиле Бурунова → TTS → wav.

## Тюнинг под Бурунова

В `tts_server.py`:
- `SPEED = 0.9` — Бурунов говорит медленно, лениво. Опусти до 0.85 если звучит торопливо.
- `TEMPERATURE = 0.6` — ниже = стабильнее, выше = разнообразнее.
- `TOP_P = 0.7` — стабильность выбора токенов.

В референсе выбери кусок где Бурунов **лениво, с паузами** что-то говорит —
TTS будет копировать эту манеру.

## Типичные проблемы

| Симптом | Решение |
|---|---|
| Голос звучит как робот, не Бурунов | Мало данных. Добавь ещё 5-10 мин аудио, переобучи |
| Шум/хрип в синтезе | Плохая чистка референса. demucs + loudnorm |
| Слишком быстро/торопливо | `SPEED = 0.85`, проверь референс — там должно быть медленно |
| Ооочень медленный инференс | CPU вместо GPU. T4 = ~1 сек/сек аудио, CPU = ~5 сек/сек |
| Out of memory при обучении | batch_size 6 → 2, или gradient checkpointing |
| Синтез обрывается на длинном тексте | Юзай `/stream` — он бьёт по предложениям |
