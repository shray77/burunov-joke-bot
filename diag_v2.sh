#!/usr/bin/env bash
# =============================================================================
# diag.sh — первичная диагностика Unitree G1 EDU Ultimate D
# Запускать ПЕРВЫМ делом после получения SSH к роботу.
#
# Использование:
#   chmod +x diag.sh
#   ./diag.sh          # весь вывод в терминал
#   ./diag.sh | tee diag_$(date +%Y%m%d_%H%M%S).log   # с сохранением в файл
#
# Что проверяет:
#   1. Систему (OS, ядро, CPU, RAM, диск)
#   2. Сеть (WiFi, IP, пинг до шлюза)
#   3. USB-устройства (RealSense, возможные лидары)
#   4. Версии прошивки G1 (КРИТИЧНО — см. чек-лист ниже)
#   5. Docker / Python / pip пакеты
#   6. unitree_sdk2_python
#   7. Доступность сервисов G1 (Vui, Vul, Webrtc, Audio Hub)
#   8. Доступность видеокамеры RealSense
#   9. Тестовые API-вызовы (AudioClient.LedControl, LocoClient.StandUp)
# =============================================================================
set -u
BLUE='\033[0;34m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[OK]${NC}   $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERR]${NC}  $1"; }
hdr()  { echo -e "\n${BLUE}=== $1 ===${NC}"; }

# Минимальные версии прошивки G1 (из чек-листа проекта)
declare -A MIN_FW=(
  [Vui_Service]="2.0.3.8"
  [Vui_Module]="2.0.0.3"
  [Vul_Service]="2.0.4.4"
  [Webrtc_Bridge]="1.0.7.5"
  [Audio_Hub]="1.0.1.0"
  [Firmware]="1.3.0"
)

# Сравнение версий вида X.Y.Z.W
ver_ge() {
  # $1 >= $2 ?
  local a="$1" b="$2"
  if [[ -z "$a" || -z "$b" ]]; then return 1; fi
  local IFS=.
  local i va=($a) vb=($b)
  for ((i=0; i<${#va[@]} || i<${#vb[@]}; i++)); do
    local na=${va[i]:-0} nb=${vb[i]:-0}
    if (( na > nb )); then return 0; fi
    if (( na < nb )); then return 1; fi
  done
  return 0
}

echo "Burunov Bot — G1 diagnostic"
echo "Date: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "Host: $(hostname)"

# ---------------------------------------------------------------- 1. Система
hdr "1. System"
echo "OS:       $(lsb_release -ds 2>/dev/null || cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d= -f2)"
echo "Kernel:   $(uname -r)"
echo "CPU:      $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | xargs)"
echo "CPU cores: $(nproc)"
echo "RAM:      $(free -h | awk '/^Mem:/ {print $2}') total"
echo "Disk /:   $(df -h / | awk 'NR==2 {print $2}') total, $(df -h / | awk 'NR==2 {print $4}') free"
echo "Uptime:   $(uptime -p)"
[[ $(nproc) -ge 4 ]] && ok "CPU cores >= 4" || warn "CPU cores < 4 — может быть тесно для Ollama+YOLO"
RAM_GB=$(free -g | awk '/^Mem:/ {print $2}')
[[ ${RAM_GB:-0} -ge 16 ]] && ok "RAM >= 16GB" || warn "RAM < 16GB — Ollama+ChromaDB будет тесно"

# ---------------------------------------------------------------- 2. Сеть
hdr "2. Network"
ip -br addr 2>/dev/null | awk '{print $1, $3}'
GW=$(ip route | awk '/default/ {print $3; exit}')
if [[ -n "$GW" ]]; then
  ping -c 2 -W 2 "$GW" >/dev/null 2>&1 && ok "ping gateway $GW" || warn "no ping to gateway $GW"
fi
# На демо WiFi не нужен, но для дебага полезно
ip -br link | awk '{print $1, $2}'

# ---------------------------------------------------------------- 3. USB
hdr "3. USB devices"
if lsusb >/dev/null 2>&1; then
  lsusb
  lsusb | grep -qi "Intel Corp.*RealSense" && ok "RealSense detected" || warn "RealSense not detected in lsusb"
  lsusb | grep -qi "Livox\|Mid360" && ok "Livox MID360 detected" || warn "Livox not in lsusb (may be on separate bus)"
else
  warn "lsusb not available"
fi

# ---------------------------------------------------------------- 4. Прошивки
hdr "4. Firmware versions (CRITICAL)"
echo "Минимальные требуемые версии:"
for k in Vui_Service Vui_Module Vul_Service Webrtc_Bridge Audio_Hub Firmware; do
  echo "  $k >= ${MIN_FW[$k]}"
done
echo
echo "Проверка установленных версий..."

# Способ 1: через REST API сервисного слоя G1 (обычно на порту 8081/80/443)
# Точные эндпоинты надо сверить с https://support.unitree.com/home/en/G1_developer/services_interface
# На практике организаторы дают доступ к веб-интерфейсу с версиями — там и смотрим.
echo "  -> Попытка получить версии через сервисный API..."
for port in 80 443 8080 8081 14444; do
  curl -sk --max-time 2 "https://127.0.0.1:$port/api/system/info" 2>/dev/null \
    | head -c 200 && echo "  (port $port)"
done
echo
warn "Если версии не вытянулись — открой веб-интерфейс G1 (обычно https://<robot-ip>) и вбей руками:"
warn "  Vui_Service, Vui_Module, Vul_Service, Webrtc Bridge, Audio Hub, Firmware"
warn "Сравни с минимальными выше. Если что-то ниже — сообщи ментору/организаторам."

# ---------------------------------------------------------------- 5. Docker / Python
hdr "5. Toolchain"
command -v docker >/dev/null && ok "docker $(docker --version)" || err "docker missing — нужен для деплоя"
command -v docker-compose >/dev/null && ok "docker-compose" || (command -v docker >/dev/null && docker compose version >/dev/null 2>&1 && ok "docker compose (plugin)" || warn "docker-compose missing")
command -v python3 >/dev/null && ok "python3 $(python3 --version)" || err "python3 missing"
command -v pip3 >/dev/null && ok "pip3" || warn "pip3 missing"
command -v nvidia-smi >/dev/null && ok "GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null)" || warn "No NVIDIA GPU (для G1 не обязательно, только для обучения)"

# ---------------------------------------------------------------- 6. unitree SDK
hdr "6. unitree_sdk2_python"
python3 -c "import unitree_sdk2py; print('unitree_sdk2py OK')" 2>/dev/null && ok "unitree_sdk2py import works" || warn "unitree_sdk2py not installed — ставь: pip install unitree_sdk2py"
python3 -c "from unitree_sdk2py.core.channel import ChannelPublisher; print('core.channel OK')" 2>/dev/null && ok "core.channel OK" || warn "core.channel import failed"

# ---------------------------------------------------------------- 7. Сервисы G1
hdr "7. G1 services (local ports)"
for svc in "Vui:8002" "Vul:8003" "AudioHub:8004"; do
  name="${svc%%:*}"; port="${svc##*:}"
  if ss -tln 2>/dev/null | grep -q ":$port "; then
    ok "$name listening on :$port"
  else
    warn "$name not on :$port (порт может быть другим — сверить с докой)"
  fi
done

# ---------------------------------------------------------------- 8. RealSense
hdr "8. RealSense camera"
python3 -c "
try:
    import pyrealsense2 as rs
    ctx = rs.context()
    devs = list(ctx.query_devices())
    print(f'RealSense devices: {len(devs)}')
    for d in devs:
        print(f'  - {d.get_info(rs.camera_info.name)} fw={d.get_info(rs.camera_info.firmware_version)}')
    if devs:
        print('REALSENSE_OK')
    else:
        print('REALSENSE_NO_DEVICES')
except ImportError:
    print('PYREALSENSE_MISSING')
except Exception as e:
    print(f'REALSENSE_ERR: {e}')
" 2>&1

# ---------------------------------------------------------------- 9. Диск под модели
hdr "9. Disk for models"
DATA_DIR="${BURUNOV_DATA:-/home/unitree/burunov-bot/data}"
echo "Planned data dir: $DATA_DIR"
mkdir -p "$DATA_DIR" 2>/dev/null && ok "dir created/exists" || warn "cannot create $DATA_DIR (permissions?)"
echo "Free space there: $(df -h "$DATA_DIR" 2>/dev/null | awk 'NR==2 {print $4}')"
echo "Models expected:"
echo "  - Gemma 3 4B Q4  (~3-4 GB)"
echo "  - multilingual-e5-small (~470 MB)"
echo "  - Piper burunov.onnx (~60 MB)"
echo "  - F5-TTS Russian (~300-500 MB)"
echo "  - YOLOv8n (~6 MB)"
echo "  - ChromaDB 27k jokes (~100-200 MB)"
echo "  Total: ~5-6 GB minimum"

# ---------------------------------------------------------------- 10. Финал
hdr "10. Quick API smoke test (optional, dangerous — uncomment if ready)"
cat <<'EOF'
# Раскомментируй только когда уверен что G1 стоит безопасно.
# Эти команды двигают роботом и мигают LED.

# python3 - <<'PY'
# from unitree_sdk2py.go2.audio.audio_client import AudioClient
# from unitree_sdk2py.core.channel import ChannelFactoryInitialize
# ChannelFactoryInitialize(0, "eth0")  # или нужный интерфейс
# ac = AudioClient()
# ac.LedControl(0, 255, 0)  # зелёный = OK
# print("LED green OK")
# PY
EOF

echo
echo "============================================"
echo "Diagnostic done. Если есть [ERR] или [WARN] — фиксим до деплоя."
echo "Особое внимание — раздел 4 (прошивки). Если что-то ниже минимума, демо поедет."
echo "============================================"
