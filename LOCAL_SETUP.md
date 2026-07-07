# Гайд для запуска на твоём железе (RTX 4060 8GB)

> Без воды. Копируй команду → вставляй в консоль → жми Enter.

---

## ШАГ 1. Поставь Python 3.11+ (если нет)

Проверь, что есть:
```bash
python --version
```

Если пишет `Python 3.11.x` или выше — ок. Если нет — скачай с https://www.python.org/downloads/ и поставь галочку **"Add Python to PATH"**.

---

## ШАГ 2. Поставь Git (если нет)

Скачай с https://git-scm.com/download/win и поставь next-next-next.

Проверь:
```bash
git --version
```

---

## ШАГ 3. Скачай проект

Открой консоль (Win+R → `cmd` → Enter) в папке, куда хочешь положить проект, и выполни:

```bash
git clone https://github.com/shray77/burunov-joke-bot.git
cd burunov-joke-bot
```

---

## ШАГ 4. Создай виртуальное окружение

Это чтобы не засирать систему пакетами.

```bash
python -m venv venv
venv\Scripts\activate
```

После `activate` в начале строки появится `(venv)` — значит работает.

> Если PowerShell ругается на "выполнение сценариев запрещено" — открой PowerShell от администратора и выполни один раз: `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser`

---

## ШАГ 5. Поставь CUDA-torch (важно! для GPU)

⚠️ **Не запускай `pip install -r requirements.txt` напрямую!** Там обычный torch без CUDA.

Сначала ставим CUDA-версию torch (2.5 ГБ, качается ~5 минут):

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu121
```

Потом всё остальное:

```bash
pip install chromadb sentence-transformers fastapi uvicorn httpx
```

---

## ШАГ 6. Проверь, что GPU виден

```bash
python -c "import torch; print('CUDA:', torch.cuda.is_available()); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NONE')"
```

Должно вывести:
```
CUDA: True
GPU: NVIDIA GeForce RTX 4060
```

Если `CUDA: False` — ты где-то проебался в ШАГЕ 5. Удали torch и переустанови:
```bash
pip uninstall torch -y
pip install torch --index-url https://download.pytorch.org/whl/cu121
```

---

## ШАГ 7. Проверь, что данные на месте

После `git clone` у тебя уже должны быть:
- `data/jokes_clean.jsonl` — 27 326 анекдотов
- `data/chroma_db/` — 1500 embeddings (старая база, сейчас удалим)

Проверь:
```bash
python -c "import json; n=sum(1 for _ in open('data/jokes_clean.jsonl', encoding='utf-8')); print('Анекдотов в базе:', n)"
```

Должно написать: `Анекдотов в базе: 27326`

---

## ШАГ 8. Удали старую базу (1500 штук)

Мы сейчас пересоздадим её на полное 27k:

```bash
rmdir /s /q data\chroma_db
```

---

## ШАГ 9. Открой config.py и сними лимит

В файле `config.py` найди строку:
```python
MAX_JOKES_FOR_INDEX = 1500
```

Поменяй на:
```python
MAX_JOKES_FOR_INDEX = None
```

Это значит "индексируй ВСЕ 27k анекдотов".

---

## ШАГ 10. Запусти индексацию (3 минуты на GPU)

```bash
python build_vector_db.py
```

Должно пойти так:
```
Загружено анекдотов: 27326
Лимит None: выбрано 27326 (из 27326)
Загружаю эмбеддер intfloat/multilingual-e5-small ...
Считаю эмбеддинги (это разово, ~1-2 мин на 1000 анекдотов)...
Batches: 100%|██████████| 854/854 [03:12<00:00, 4.43it/s]
Готово за 192.4 сек
В коллекции 27326 анекдотов.
```

**3-5 минут и всё.** На CPU было бы 2.5 часа.

---

## ШАГ 11. Проверь, что поиск работает

```bash
python retriever.py "Штирлиц и Мюллер"
```

Должен выдать 5 анекдотов с косинусной похожестью 0.7-0.9.

Попробуй разные темы:
```bash
python retriever.py "Вовочка"
python retriever.py "Чапаев и Петька"
python retriever.py "Брежнев"
python retriever.py "советская очередь"
```

---

## ШАГ 12. (Опционально) Поставь Ollama для генерации

Сейчас бот работает в fallback-режиме (берёт топ-1 анекдот и оборачивает в «Ну, слушай...»).

Чтобы была полноценная генерация в стиле Бурунова, поставь Ollama:

1. Скачай с https://ollama.com/download
2. Установи (next-next-next)
3. Открой **новую** консоль (чтобы подхватился PATH) и выполни:
   ```bash
   ollama pull gemma3:4b
   ```
   (скачает ~2.5 ГБ, 5-10 минут)
4. Запусти Ollama-сервер:
   ```bash
   ollama serve
   ```
5. В **другой** консоли (в папке проекта, с активированным venv) проверь генерацию:
   ```bash
   python rag_pipeline.py "Штирлиц"
   ```

Должно выдать что-то типа:
```
ТЕМА: Штирлиц
FALLBACK: False
ИСТОЧНИКОВ: 5
ТЕКСТ ДЛЯ TTS:
Ну, слушай... Подходит как-то Мюллер к Борману...
```

---

## ШАГ 13. Запусти API-сервер

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

Открой в браузере: http://localhost:8000/

Должен показать healthcheck JSON:
```json
{
  "status": "ok",
  "model": "gemma3:4b",
  "embed_model": "intfloat/multilingual-e5-small",
  "collection": "jokes_1986"
}
```

Поиск анекдотов:
```
http://localhost:8000/search?q=Штирлиц&top_k=5
```

---

## Если что-то сломалось

| Симптом | Решение |
|---|---|
| `CUDA: False` | Переустанови torch с cu121 |
| `ModuleNotFoundError: No module named 'X'` | `pip install X` |
| `FileNotFoundError: data/jokes_clean.jsonl` | Ты не в папке проекта. `cd burunov-joke-bot` |
| Ollama не отвечает | `ollama serve` должно быть запущено в отдельной консоли |
| `chromadb.errors.NotFoundError` | `rmdir /s /q data\chroma_db` и заново `python build_vector_db.py` |
| В середине процесса зависло | Подожди. e5-small на GPU работает быстро, но первый раз качает модель ~120 МБ |

---

## Сколько времени занимает весь путь

| Шаг | Время |
|---|---|
| Шаги 1-2 (Python + Git) | 5 минут |
| Шаг 3 (clone) | 1 минута |
| Шаг 4 (venv) | 1 минута |
| Шаг 5 (pip install) | 8-10 минут |
| Шаг 6 (проверка GPU) | 30 секунд |
| Шаги 7-9 (подготовка данных) | 1 минута |
| Шаг 10 (индексация 27k) | **3-5 минут на GPU** |
| Шаг 11 (проверка поиска) | 30 секунд |
| Шаг 12 (Ollama, опционально) | 10 минут |
| Шаг 13 (API) | 30 секунд |

**Итого: ~30 минут на полный запуск с GPU.**

---

## Что дальше

Когда всё работает:
1. TTS — добавить голос Бурунова (GPT-SoVITS или Silero)
2. Telegram-бот — обернуть `api.py` в `python-telegram-bot`
3. Голосовой ввод — Whisper ( speech-to-text, тоже на GPU)
