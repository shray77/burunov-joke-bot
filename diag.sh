#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
# diag.sh — полная диагностика Unitree G1 для хакатона Burunov Bot
# ─────────────────────────────────────────────────────────────────────
# Запуск на G1 по SSH:
#   bash diag.sh            → вывод в терминал
#   bash diag.sh > diag.txt → вывод в файл (скидывай мне потом)
#
# Что собирает:
#   1. ОС, ядро, архитектура
#   2. Железо: CPU, RAM, GPU/NPU, диск
#   3. Сеть: интерфейсы, IP, доступность хостов
#   4. Python и пакеты (особенно unitree_sdk2)
#   5. Docker
#   6. Ollama
#   7. Аудио устройства
#   8. Версии Unitree сервисов (если доступны)
#   9. Тест импортов SDK
#  10. Процессы Unitree
#  11. Проверка доступности RAG/TTS если запущены
# ─────────────────────────────────────────────────────────────────────

echo "════════════════════════════════════════════════════════════════"
echo "  DIAG: Unitree G1 — $(date)"
echo "  Host: $(hostname) | User: $(whoami)"
echo "════════════════════════════════════════════════════════════════"

echo ""
echo "━━━━━ 1. ОПЕРАЦИОННАЯ СИСТЕМА ━━━━━"
echo "— uname -a —"
uname -a
echo ""
echo "— lsb_release —"
lsb_release -a 2>/dev/null || cat /etc/os-release 2>/dev/null
echo ""
echo "— uptime —"
uptime
echo ""
echo "— locale —"
locale 2>/dev/null | head -5

echo ""
echo "━━━━━ 2. ЖЕЛЕЗО ━━━━━"
echo "— CPU —"
lscpu | grep -E "Model name|Architecture|^CPU\(s\)|Thread|Core|MHz" 2>/dev/null
echo ""
echo "— RAM —"
free -h
echo ""
echo "— Disk —"
df -h / /home 2>/dev/null
echo ""
echo "— GPU (NVIDIA) —"
nvidia-smi 2>/dev/null || echo "  (нет NVIDIA)"
echo ""
echo "— NPU / другие ускорители —"
ls /dev/npu* 2>/dev/null || echo "  /dev/npu* — нет"
ls /dev/davinci* 2>/dev/null || echo "  /dev/davinci* — нет (Ascend)"
echo ""
echo "— USB устройства —"
lsusb 2>/dev/null | head -20

echo ""
echo "━━━━━ 3. СЕТЬ ━━━━━"
echo "— Интерфейсы —"
ip -br addr 2>/dev/null || ifconfig 2>/dev/null
echo ""
echo "— Маршрут по умолчанию —"
ip route 2>/dev/null | head -5
echo ""
echo "— DNS —"
cat /etc/resolv.conf 2>/dev/null | grep -v "^#"
echo ""
echo "— Пинг до 192.168.123.161 (внутренний IP G1) —"
ping -c 2 -W 1 192.168.123.161 2>&1 | tail -4
echo ""
echo "— Пинг до 8.8.8.8 (внешний интернет) —"
ping -c 2 -W 2 8.8.8.8 2>&1 | tail -4
echo ""
echo "— Пинг до github.com —"
ping -c 2 -W 2 github.com 2>&1 | tail -4
echo ""
echo "— Multicast-подписка (для AudioClient) —"
ip maddr 2>/dev/null | grep -A1 "239.168.123" || echo "  multicast 239.168.123.* — не найден"
echo ""
echo "— Открытые порты (слушают) —"
ss -tlnp 2>/dev/null | head -20 || netstat -tlnp 2>/dev/null | head -20

echo ""
echo "━━━━━ 4. PYTHON ━━━━━"
echo "— Версии Python —"
which python python3 2>/dev/null
python3 --version 2>&1
python --version 2>&1
echo ""
echo "— pip —"
which pip pip3 2>/dev/null
pip3 --version 2>&1
echo ""
echo "— Установленные пакеты (интересные) —"
pip3 list 2>/dev/null | grep -iE "unitree|fastapi|uvicorn|chromadb|sentence|httpx|piper|pyaudio|whisper|torch|numpy|ollama" || echo "  (ничего не найдено)"
echo ""
echo "— Virtualenvs (если есть) —"
ls -la ~/burunov*/venv 2>/dev/null
ls -la /opt/*/venv 2>/dev/null
echo ""

echo "━━━━━ 5. UNITREE SDK ━━━━━"
echo "— Поиск unitree_sdk2_python в системе —"
find / -name "unitree_sdk2_python" -type d 2>/dev/null | head -5
find / -name "unitree_sdk2py" -type d 2>/dev/null | head -5
find / -path "*unitree_sdk2*" -name "*.py" 2>/dev/null | head -10
echo ""
echo "— Проверка импортов —"
python3 -c "
try:
    from unitree_sdk2py.core.channel import ChannelFactory
    print('  ✅ unitree_sdk2py.core.channel.ChannelFactory — OK')
except ImportError as e:
    print(f'  ❌ ChannelFactory: {e}')

try:
    from unitree_sdk2py.g1.audio.audio_client import AudioClient
    print('  ✅ unitree_sdk2py.g1.audio.audio_client.AudioClient — OK')
except ImportError as e:
    print(f'  ❌ AudioClient: {e}')

try:
    from unitree_sdk2py.g1.loco.loco_client import LocoClient
    print('  ✅ unitree_sdk2py.g1.loco.loco_client.LocoClient — OK')
except ImportError as e:
    print(f'  ❌ LocoClient: {e}')

try:
    from unitree_sdk2py.g1.hand.hand_client import HandClient
    print('  ✅ unitree_sdk2py.g1.hand.hand_client.HandClient — OK')
except ImportError as e:
    print(f'  ❌ HandClient: {e}')
" 2>&1
echo ""
echo "— Поиск альтернативных путей в SDK —"
find / -path "*unitree*g1*" -type d 2>/dev/null | head -10
find / -path "*unitree*audio*" -name "*.py" 2>/dev/null | head -10
find / -path "*unitree*loco*" -name "*.py" 2>/dev/null | head -10

echo ""
echo "━━━━━ 6. DOCKER ━━━━━"
echo "— Docker —"
which docker 2>/dev/null && docker --version
echo ""
echo "— Docker compose —"
docker compose version 2>&1 | head -1
echo ""
echo "— Запущенные контейнеры —"
docker ps 2>/dev/null || echo "  (docker не запущен или нет прав)"
echo ""
echo "— Все контейнеры (вкл. остановленные) —"
docker ps -a 2>/dev/null | head -10
echo ""
echo "— Docker images —"
docker images 2>/dev/null | head -10

echo ""
echo "━━━━━ 7. OLLAMA ━━━━━"
echo "— Binary —"
which ollama 2>/dev/null && ollama --version 2>&1
echo ""
echo "— Сервис —"
systemctl is-active ollama 2>/dev/null || echo "  systemd: не активен"
pgrep -a ollama 2>/dev/null || echo "  процесс не запущен"
echo ""
echo "— API —"
curl -s --max-time 3 http://localhost:11434/api/tags 2>&1 | head -50 || echo "  API недоступен на :11434"
echo ""
echo "— Установленные модели —"
ollama list 2>/dev/null

echo ""
echo "━━━━━ 8. АУДИО ━━━━━"
echo "— ALSA playback devices —"
aplay -l 2>&1 | head -20
echo ""
echo "— ALSA capture devices —"
arecord -l 2>&1 | head -20
echo ""
echo "— PulseAudio sinks —"
pactl list short sinks 2>/dev/null || echo "  PulseAudio: нет"
echo ""
echo "— PulseAudio sources —"
pactl list short sources 2>/dev/null || echo "  PulseAudio: нет"
echo ""
echo "— Громкость —"
amixer 2>/dev/null | head -20 || echo "  amixer недоступен"

echo ""
echo "━━━━━ 9. UNITREE СЕРВИСЫ / ПРОШИВКА ━━━━━"
echo "— /opt/unitree —"
ls -la /opt/unitree/ 2>/dev/null || echo "  /opt/unitree — нет"
echo ""
echo "— /etc/unitree —"
ls -la /etc/unitree/ 2>/dev/null || echo "  /etc/unitree — нет"
echo ""
echo "— Unitree-процессы —"
ps aux 2>/dev/null | grep -iE "unitree|vui|vul|webrtc|audio_hub|loco|sport" | grep -v grep | head -20
echo ""
echo "— Поиск файлов версий сервисов —"
find / -name "*vui*version*" 2>/dev/null | head -5
find / -name "*vul*version*" 2>/dev/null | head -5
find / -name "*audio_hub*" 2>/dev/null | head -5
find / -name "version*.txt" -path "*unitree*" 2>/dev/null | head -5
find /opt/unitree -name "*.json" 2>/dev/null | head -10
echo ""
echo "— DDS-топики (если есть cyclonedds) —"
which ros2 2>/dev/null && ros2 topic list 2>&1 | head -20 || echo "  ROS2 не установлен"
echo ""
echo "— Поиск libdds —"
find / -name "libddsc*" 2>/dev/null | head -5
find / -name "libcyclonedds*" 2>/dev/null | head -5

echo ""
echo "━━━━━ 10. ФАЙЛЫ ПРОЕКТА ━━━━━"
echo "— Поиск burunov / проекта —"
find / -name "burunov*" -type d 2>/dev/null | head -5
find ~ -name "robot_controller.py" 2>/dev/null | head -5
ls -la ~/burunov/ 2>/dev/null
echo ""
echo "— Содержимое если есть —"
if [ -d ~/burunov ]; then
    ls -la ~/burunov/*.py 2>/dev/null
    echo ""
    echo "— config.py (если есть) —"
    cat ~/burunov/config.py 2>/dev/null | head -30
fi

echo ""
echo "━━━━━ 11. ЗАПУЩЕННЫЕ СЕРВИСЫ ━━━━━"
echo "— RAG API (8000) —"
curl -s --max-time 3 http://localhost:8000/health 2>&1 || echo "  не отвечает"
echo ""
echo "— TTS API (8001) —"
curl -s --max-time 3 http://localhost:8001/health 2>&1 || echo "  не отвечает"
echo ""
echo "— Robot controller (8002) —"
curl -s --max-time 3 http://localhost:8002/health 2>&1 || echo "  не отвечает"

echo ""
echo "━━━━━ 12. СИСТЕМНЫЕ РЕСУРСЫ ━━━━━"
echo "— Top процессов по CPU —"
ps aux --sort=-%cpu 2>/dev/null | head -10
echo ""
echo "— Top процессов по RAM —"
ps aux --sort=-%mem 2>/dev/null | head -10
echo ""
echo "— dmesg (последние 20 строк, ошибки железа) —"
dmesg 2>/dev/null | tail -20 || echo "  (нужен sudo для dmesg)"

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  ДИАГНОСТИКА ЗАВЕРШЕНА"
echo "════════════════════════════════════════════════════════════════"
