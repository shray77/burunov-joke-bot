# FIRST_CONTACT.md — как первый раз зайти на G1 и запустить всё

> Если ты открыл этот файл в первый раз — у тебя есть SSH-доступ к G1 и 2 дня до демо.
> Делай по шагам, не пропусти ничего.

---

## 0. Что вообще происходит

G1 — это Ubuntu-мини-ПК на ногах. Доступ через SSH как к любому линукс-серверу.
Сервисы (RAG, TTS, оркестратор) — обычные Python FastAPI процессы, слушают порты
на самом роботе. Телефон подключается к WiFi робота и дёргает HTTP API.

```
[Телефон/ноут] --WiFi G1--> [G1 :8000 RAG, :8001 TTS, :8002 Coffee]
                              |
                              +-- LocoClient (движение)
                              +-- AudioClient (динамик Stanley)
                              +-- HandClient (кисти RH56DFTP)
                              +-- RealSense D435
                              +-- Livox MID360
```

---

## 1. Включить G1 и подключиться к его WiFi

1. Включи G1 — кнопка питания на спине у основания, держать 2-3 сек пока RGB не загорится
2. Подожди 30-40 сек (загрузка Ubuntu)
3. На ноуте найди WiFi сеть `Unitree-G1-XXXX` (последние цифры серийника)
4. Пароль по умолчанию:
   - `00000000` (8 нулей) — самый частый
   - или `12345678`
   - или спроси у организаторов хакатона
5. Ноут получит IP в подсети `192.168.123.0/24`

Если WiFi не раздаётся:
- G1 мог загрузиться в безопасном режиме → перезагрузи
- Или используй Ethernet (RJ45 на G1) напрямую к ноуту, IP ноута вручную: `192.168.123.222/24`

---

## 2. Найти IP робота

Основной мини-ПК G1 обычно на **`192.168.123.161`**.

Проверь:
```bash
ping 192.168.123.161
```

Не пингуется — просканируй:
```bash
# macOS / Linux
nmap -sn 192.168.123.0/24
# или
arp -a
```

Альтернативные IP G1: `192.168.123.162`, `192.168.123.10`. Зависит от конфигурации.

---

## 3. Зайти по SSH

```bash
ssh unitree@192.168.123.161
```

Пароль (попробуй по очереди):
- `123`
- `000000`
- `Unitree0408`
- спроси у организаторов

Попал внутрь — увидишь `unitree@ubuntu:~$`. Ты в Ubuntu на основном мини-ПК робота.

---

## 4. Перекинуть файлы с ноута

В **отдельном** терминале на ноуте (НЕ в SSH):

```bash
# Скачать репо на ноут
git clone https://github.com/shray77/burunov-joke-bot.git

# Залить на G1
scp -r burunov-joke-bot unitree@192.168.123.161:~/
```

Если репо уже скачан и модифицирован — просто обнови:
```bash
cd burunov-joke-bot && git pull
scp *.py unitree@192.168.123.161:~/burunov-joke-bot/
```

Большие файлы (модели 3-5 ГБ) лучше через USB-флешку — WiFi G1 медленный.

---

## 5. Прогнать диагностику (ПЕРВЫМ ДЕЛОМ)

В SSH-сессии:
```bash
cd ~/burunov-joke-bot
chmod +x diag_v2.sh
bash diag_v2.sh | tee diag.log
```

**Что смотреть в выводе:**
- Раздел 1 (System): RAM ≥ 16GB? CPU ≥ 4 ядра?
- Раздел 3 (USB): RealSense и Livox видны?
- Раздел 4 (Firmware): СВЕРИТЬ с минимальными версиями:
  - Vui_Service ≥ 2.0.3.8
  - Vui_Module ≥ 2.0.0.3
  - Vul_Service ≥ 2.0.4.4
  - Webrtc_Bridge ≥ 1.0.7.5
  - Audio_Hub ≥ 1.0.1.0
  - Firmware ≥ 1.3.0
- Раздел 6: `unitree_sdk2py` импортируется?
- Раздел 8: RealSense отдаёт кадры?

Скачать лог на ноут:
```bash
scp unitree@192.168.123.161:~/burunov-joke-bot/diag.log ./
```

Скинь мне содержимое — проставлю точные SDK-импорты в коде.

---

## 6. Установить зависимости

В SSH на G1:
```bash
cd ~/burunov-joke-bot

# Основной requirements
pip install -r requirements.txt

# Доп. пакеты для новых скриптов (CV/лидар/зрение)
pip install ultralytics pyrealsense2 fastapi uvicorn pydantic

# Ollama для локального LLM
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gemma3:4b   # ~3-4 ГБ, качаться будет долго через WiFi G1
```

Если pip ругается на права — `pip install --user ...` или `sudo pip ...`.

---

## 7. Использовать tmux (обязательно!)

Если закрыть SSH — все запущенные процессы умрут. Чтобы этого не было — `tmux`:

```bash
sudo apt install tmux -y

# Создать сессию
tmux new -s rag
# Запустил python3 api.py
# Ctrl+B, потом D — отсоединился, процесс работает в фоне

# Вернуться
tmux attach -s rag

# Создать ещё одну
tmux new -s tts

# Список сессий
tmux ls
```

---

## 8. Запустить сервисы (4 отдельных tmux-сессии)

### 8.1. RAG (LLM + анекдоты), порт :8000
```bash
tmux new -s rag -d
# Внутри:
cd ~/burunov-joke-bot
python3 api.py
# Ctrl+B, D — отсоединиться
```

Проверка:
```bash
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/tell -H "Content-Type: application/json" -d '{"topic":"Штирлиц"}'
```

### 8.2. TTS (голос Бурунова), порт :8001

**Вариант A — F5-TTS (если есть референс аудио и CPU тянет):**
```bash
tmux new -s tts -d
cd ~/burunov-joke-bot
python3 f5_tts_server.py --port 8001 \
  --ref-audio ~/burunov-joke-bot/data/burunov_ref.wav \
  --ref-text "Привет, я Сергей Бурунов"
```

**Вариант B — Piper (если F5 медленный):**
```bash
tmux new -s tts -d
cd ~/burunov-joke-bot
python3 edge_tts_server.py
```

Проверка:
```bash
curl -X POST http://127.0.0.1:8001/synthesize_pcm \
  -H "Content-Type: application/json" \
  -d '{"text":"Привет, я Бурунов"}' \
  --output test.pcm
ls -lh test.pcm   # должен быть ненулевой
```

### 8.3. Coffee Delivery (оркестратор), порт :8002
```bash
tmux new -s coffee -d
cd ~/burunov-joke-bot
python3 coffee_delivery.py --mode server --port 8002
```

Проверка статуса:
```bash
curl http://127.0.0.1:8002/health
```

Должно вернуть JSON с подсистемами `mover`, `audio`, `lidar`, `vision`, `hand`.

### 8.4. Дебаг-терминал
Просто держи один tmux свободным для логов/тестов/убивания зависшего.

---

## 9. Дёрнуть с телефона

1. Телефон подключи к тому же WiFi G1
2. В браузере:
   ```
   http://192.168.123.161:8002/health
   ```
   Должен вернуться JSON со статусом.
3. Для команды "принеси кофе":
   - Установи Postman / Insomnia / или используй `curl` в Termux
   - `POST http://192.168.123.161:8002/coffee`
   - Тело: `{"recipient":"Олег"}`
4. Для аварийной остановки:
   - `POST http://192.168.123.161:8002/stop`

Простая HTML-страничка для управления с телефона:
```bash
# На G1 создай /var/www/index.html или используй SimpleHTTPServer
```
(можно написать позже — пока хватит Postman/curl)

---

## 10. Если что-то пошло не так

### G1 не раздаёт WiFi
- Перезагрузи (выкл/вкл кнопку питания, подожди 1 мин)
- Спроси у организаторов — может его перевели в client mode

### SSH не пускает
- Проверь пароли (`123`, `000000`, `Unitree0408`)
- Спроси у организаторов точные креды
- Может быть отключен SSH — попроси включить

### `unitree_sdk2py` не импортируется
```bash
pip install unitree_sdk2py
# или
pip3 install unitree_sdk2py
# если нет в PyPI — собрать из исходников
# https://github.com/unitreerobotics/unitree_sdk2_python
```

### RealSense не виден
- Проверь USB-кабель (Type-C, должен быть data+power)
- `lsusb` — должен быть "Intel Corp. RealSense D435"
- Перезагрузи G1 если не появляется

### Robot не двигается на Move()
- Проверь что G1 стоит на ногах в режиме `StandUp`
- Возможно включен безопасный режим — выключи через веб-интерфейс G1
- Проверь что `G1Mover.init()` вернул True (раздел health)

### Robot делает страшное
- АППАРАТНАЯ КНОПКА ОСТАНОВКИ на спине (красная) → нажать → он обмякнет
- Или `curl -X POST http://192.168.123.161:8002/stop`
- Или в SSH: `python3 -c "from unitree_sdk2py... import LocoClient; LocoClient().Damp()"`

---

## 11. Чеклист перед демо

- [ ] SSH работает, креды известны
- [ ] `diag_v2.sh` прогнан, лог сохранён
- [ ] Прошивки ≥ минимальных (раздел 4 diag)
- [ ] `unitree_sdk2py` импортируется
- [ ] RealSense отдаёт кадры
- [ ] Livox виден
- [ ] Ollama + Gemma 3 4B скачаны
- [ ] RAG (:8000) отвечает на /tell
- [ ] TTS (:8001) синтезирует PCM
- [ ] Coffee (:8002) /health отвечает ok
- [ ] Хотя бы один тестовый Move() без падения
- [ ] Хотя бы один тестовый AudioClient.PlayStream()
- [ ] Хотя бы один тестовый HandClient.close_hand()
- [ ] Аварийная остановка работает (/stop)
- [ ] Телефон достукивается до :8002 через WiFi G1
- [ ] Запасные wav на 5 тем лежат в /data/preset_wav/

---

## 12. Полезные команды

```bash
# Температура CPU (G1 греется)
sensors

# Свободная память
free -h

# Процессы Python
ps aux | grep python

# Убить зависший сервис
pkill -f "python3 api.py"
pkill -f "python3 coffee_delivery.py"

# Логи ядра (если USB отвалился)
dmesg | tail -50

# Перезагрузить G1
sudo reboot
```
