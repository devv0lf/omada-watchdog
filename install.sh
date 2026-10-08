#!/usr/bin/env bash
# ==============================================================================
# Omada ER605 Watchdog LXC Installer for Proxmox VE
# Obsługa zmiennych ENV inline + bezpieczny fallback interaktywny
# ==============================================================================
set -euo pipefail

# Przywrócenie poprawnego stanu konsoli (na wypadek wcześniejszego zacięcia)
stty sane 2>/dev/null || true

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

if ! command -v pveversion >/dev/null 2>&1; then
    echo -e "${RED}Błąd: Uruchom ten skrypt bezpośrednio w powłoce Proxmox VE (PVE Shell)!${NC}"
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo -e "${RED}Błąd: Skrypt wymaga uprawnień roota.${NC}"
    exit 1
fi

clear
echo -e "${CYAN}${BOLD}"
cat << "EOF"
   ___                     _         __      __     _       _         _             
  / _ \ _ __ ___   __ _  __| | __ _  \ \    / /__ _| |_ ___| |__   __| | ___   __ _ 
 | | | | '_ ` _ \ / _` |/ _` |/ _` |  \ \/\/ / _` | __/ __| '_ \ / _` |/ _ \ / _` |
 | |_| | | | | | | (_| | (_| | (_| |   \  /\  (_| | || (__| | | | (_| | (_) | (_| |
  \___/|_| |_| |_|\__,_|\__,_|\__,_|    \/  \__,_|\__\___|_| |_|\__,_|\___/ \__, |
                                                                             |___/  
EOF
echo -e "${NC}${GREEN}Autoinstalator LXC dla Omada ER605 Watchdog${NC}\n"

prompt_val() {
    local var_name="$1"
    local prompt_text="$2"
    local default_val="${3:-}"
    local current_val="${!var_name:-}"

    # Jeśli zmienna została już przekazana przed uruchomieniem skryptu, użyj jej
    if [ -n "$current_val" ]; then
        echo -e "${BOLD}${prompt_text}:${NC} ${GREEN}${current_val}${NC} (ze środowiska)"
        return
    fi

    local input=""
    while true; do
        if [ -n "$default_val" ]; then
            echo -ne "${BOLD}${prompt_text}${NC} [${YELLOW}${default_val}${NC}]: "
        else
            echo -ne "${BOLD}${prompt_text}${NC}: "
        fi
        read -r input || true
        input=$(echo "$input" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

        if [ -z "$input" ] && [ -n "$default_val" ]; then
            printf -v "$var_name" "%s" "$default_val"
            break
        elif [ -n "$input" ]; then
            printf -v "$var_name" "%s" "$input"
            break
        fi
        echo -e "${RED}To pole jest wymagane!${NC}"
    done
}

echo -e "${BLUE}--- [1/4] Parametry kontenera LXC ---${NC}"
NEXT_ID=$(pvesh get /cluster/nextid)
CTID="${CTID:-}"
prompt_val CTID "Numer ID nowego kontenera" "$NEXT_ID"
HOSTNAME="${HOSTNAME:-}"
prompt_val HOSTNAME "Nazwa hosta" "omada-watchdog"
RAM="${RAM:-}"
prompt_val RAM "Pamięć RAM w MB" "512"
DISK="${DISK:-}"
prompt_val DISK "Rozmiar dysku w GB" "2"
WEB_PORT="${WEB_PORT:-}"
prompt_val WEB_PORT "Port Web UI" "8080"

STORAGE="local-lvm"
if ! pvesm status -storage "$STORAGE" &>/dev/null; then
    STORAGE="local-zfs"
    if ! pvesm status -storage "$STORAGE" &>/dev/null; then
        STORAGE="local"
    fi
fi
TARGET_STORAGE="${TARGET_STORAGE:-}"
prompt_val TARGET_STORAGE "Storage dla rootfs" "$STORAGE"

echo -e "\n${BLUE}--- [2/4] Konfiguracja Omada Open API ---${NC}"

while true; do
    OMADA_URL="${OMADA_URL:-}"
    prompt_val OMADA_URL "Adres URL kontrolera" "https://192.168.0.4"
    CLIENT_ID="${CLIENT_ID:-}"
    prompt_val CLIENT_ID "Omada Client ID" ""
    CLIENT_SECRET="${CLIENT_SECRET:-}"
    prompt_val CLIENT_SECRET "Omada Client Secret" ""

    ER605_MAC="${ER605_MAC:-}"
    while true; do
        if [ -n "$ER605_MAC" ]; then
            CLEAN_MAC=$(echo "$ER605_MAC" | tr -d '[:space:]' | tr '[:lower:]' '[:upper:]' | tr ':' '-')
            if [[ "$CLEAN_MAC" =~ ^([0-9A-F]{2}-){5}[0-9A-F]{2}$ ]]; then
                ER605_MAC="$CLEAN_MAC"
                echo -e "${BOLD}MAC routera ER605:${NC} ${GREEN}${ER605_MAC}${NC}"
                break
            fi
        fi
        echo -ne "${BOLD}Adres MAC routera ER605 (np. AA-BB-CC-DD-EE-FF)${NC}: "
        read -r RAW_MAC || true
        CLEAN_MAC=$(echo "$RAW_MAC" | tr -d '[:space:]' | tr '[:lower:]' '[:upper:]' | tr ':' '-')
        if [[ "$CLEAN_MAC" =~ ^([0-9A-F]{2}-){5}[0-9A-F]{2}$ ]]; then
            ER605_MAC="$CLEAN_MAC"
            break
        fi
        echo -e "${RED}Nieprawidłowy format. Podaj MAC w formacie XX-XX-XX-XX-XX-XX lub XX:XX:XX:XX:XX:XX${NC}"
    done

    echo -e "\n${YELLOW}>> Sprawdzanie połączenia z kontrolerem i weryfikacja routera...${NC}"

    VALIDATION_RESULT=$(python3 - "$OMADA_URL" "$CLIENT_ID" "$CLIENT_SECRET" "$ER605_MAC" << 'PYCHECK' 2>&1 || true
import sys, json, urllib.request, ssl

url = sys.argv[1].rstrip('/')
client_id = sys.argv[2]
client_secret = sys.argv[3]
target_mac = sys.argv[4].upper().replace(':', '-')

ctx = ssl._create_unverified_context()

try:
    req = urllib.request.Request(f"{url}/api/info")
    with urllib.request.urlopen(req, context=ctx, timeout=8) as r:
        info = json.loads(r.read().decode())
    omadac_id = info.get("result", {}).get("omadacId")
    ctrl_name = info.get("result", {}).get("controllerName", "Omada")
    if not omadac_id:
        print(f"ERR:Brak pola omadacId pod adresem {url}")
        sys.exit(0)
except Exception as e:
    print(f"ERR:Nie można połączyć się z kontrolerem {url}: {e}")
    sys.exit(0)

try:
    auth_data = json.dumps({"omadacId": omadac_id, "client_id": client_id, "client_secret": client_secret}).encode()
    req = urllib.request.Request(f"{url}/openapi/authorize/token?grant_type=client_credentials", data=auth_data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=8) as r:
        token_res = json.loads(r.read().decode())
    token = token_res.get("result", {}).get("accessToken")
    if not token:
        msg = token_res.get("msg", "Błędne poświadczenia")
        print(f"ERR:Błąd autoryzacji Open API ({msg}). Sprawdź Client ID i Secret.")
        sys.exit(0)
except Exception as e:
    print(f"ERR:Błąd zapytania o token: {e}")
    sys.exit(0)

try:
    req = urllib.request.Request(f"{url}/openapi/v1/{omadac_id}/sites?pageSize=20", headers={"Authorization": f"AccessToken={token}"})
    with urllib.request.urlopen(req, context=ctx, timeout=8) as r:
        sites_res = json.loads(r.read().decode())
    sites = sites_res.get("result", {}).get("data", []) or [{"siteId": "Default", "name": "Default"}]

    found_device = None
    all_devices = []

    for site in sites:
        s_id = site.get("siteId")
        d_req = urllib.request.Request(f"{url}/openapi/v1/{omadac_id}/sites/{s_id}/devices?pageSize=100", headers={"Authorization": f"AccessToken={token}"})
        with urllib.request.urlopen(d_req, context=ctx, timeout=8) as r:
            dev_res = json.loads(r.read().decode())
        devs = dev_res.get("result", {}).get("data", []) or dev_res.get("result", [])
        for d in devs:
            d_mac = d.get("mac", "").upper().replace(':', '-')
            d_model = d.get("model", d.get("deviceCategory", "Urządzenie"))
            d_name = d.get("name", d_model)
            all_devices.append(f"{d_name} ({d_model}) - MAC: {d_mac}")
            if d_mac == target_mac:
                found_device = (d_name, d_model, site.get("name", s_id))
                break
        if found_device:
            break

    if found_device:
        print(f"OK:{ctrl_name}|{found_device[0]}|{found_device[1]}|{found_device[2]}")
    else:
        dev_list = "\\n  - ".join(all_devices) if all_devices else "Brak urządzeń w kontrolerze."
        print(f"WARN:Nie znaleziono routera o MAC {target_mac}. Wykryte urządzenia:\\n  - {dev_list}")
except Exception as e:
    print(f"ERR:Błąd pobierania listy urządzeń: {e}")
PYCHECK
)

    if [[ "$VALIDATION_RESULT" =~ ^OK: ]]; then
        DETAILS=${VALIDATION_RESULT#OK:}
        IFS='|' read -r C_NAME D_NAME D_MODEL D_SITE <<< "$DETAILS"
        echo -e "${GREEN}${BOLD}✓ Połączenie z API powiodło się!${NC}"
        echo -e "  Kontroler: ${CYAN}${C_NAME}${NC}"
        echo -e "  Router:    ${GREEN}${BOLD}${D_NAME} (${D_MODEL})${NC} w witrynie [${D_SITE}]"
        echo -e "  MAC:       ${CYAN}${ER605_MAC}${NC}\n"
        break
    elif [[ "$VALIDATION_RESULT" =~ ^WARN: ]]; then
        echo -e "${YELLOW}${BOLD}! Uwaga:${NC} ${VALIDATION_RESULT#WARN:}\n"
        echo -ne "${BOLD}Czy chcesz kontynuować mimo to? (t/N)${NC}: "
        read -r CONFIRM || true
        if [[ "$CONFIRM" =~ ^[tTyY]$ ]]; then
            break
        fi
        CLIENT_ID=""
        CLIENT_SECRET=""
        ER605_MAC=""
    else
        ERR_MSG=${VALIDATION_RESULT#ERR:}
        echo -e "${RED}${BOLD}✗ Weryfikacja nie powiodła się:${NC} ${ERR_MSG}\n"
        echo -ne "${BOLD}Czy chcesz spróbować ponownie? (T/n)${NC}: "
        read -r RETRY || true
        if [[ "$RETRY" =~ ^[nN]$ ]]; then
            echo -e "${RED}Przerwano instalację.${NC}"
            exit 1
        fi
        CLIENT_ID=""
        CLIENT_SECRET=""
    fi
done

COOLDOWN="${COOLDOWN:-}"
prompt_val COOLDOWN "Czas cooldownu po restarcie (w godzinach)" "3"

echo -e "\n${BLUE}--- [3/4] Przygotowanie i tworzenie kontenera ---${NC}"

pveam update >/dev/null 2>&1 || true
TEMPLATE=$(pveam available -section system | awk '{print $2}' | grep -E 'debian-12-standard' | sort -V | tail -n 1 || true)
if [ -z "$TEMPLATE" ]; then
    TEMPLATE=$(pveam available -section system | awk '{print $2}' | grep -E 'debian' | sort -V | tail -n 1)
fi

echo -e "${YELLOW}>> Pobieranie szablonu OS (${TEMPLATE})...${NC}"
pveam download local "$TEMPLATE" >/dev/null 2>&1 || true

echo -e "${YELLOW}>> Tworzenie kontenera LXC ($CTID)...${NC}"
pct create "$CTID" "local:vztmpl/$TEMPLATE" \
    --hostname "$HOSTNAME" \
    --cores 1 \
    --memory "$RAM" \
    --swap 256 \
    --net0 name=eth0,bridge=vmbr0,ip=dhcp,firewall=1 \
    --storage "$TARGET_STORAGE" \
    --rootfs "${TARGET_STORAGE}:${DISK}" \
    --unprivileged 1 \
    --onboot 1 \
    --start 1

echo -e "${YELLOW}>> Oczekiwanie na sieć w kontenerze...${NC}"
for i in {1..20}; do
    if pct exec "$CTID" -- ping -c 1 -W 1 1.1.1.1 >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

echo -e "${YELLOW}>> Instalacja pakietów w LXC (python3, ping, curl)...${NC}"
pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq python3 iputils-ping curl ca-certificates" >/dev/null
pct exec "$CTID" -- mkdir -p /opt/omada-watchdog /etc

echo -e "\n${BLUE}--- [4/4] Bezpieczne wgrywanie konfiguracji i start serwisu ---${NC}"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT
TMP_CONFIG="${TMP_DIR}/config.json"
touch "$TMP_CONFIG"
chmod 600 "$TMP_CONFIG"

python3 - "$TMP_CONFIG" "$OMADA_URL" "$CLIENT_ID" "$CLIENT_SECRET" "$ER605_MAC" "$COOLDOWN" "$WEB_PORT" << 'PYGEN'
import sys, json

with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump({
        "OMADA_URL": sys.argv[2].rstrip('/'),
        "CLIENT_ID": sys.argv[3],
        "CLIENT_SECRET": sys.argv[4],
        "ER605_MAC": sys.argv[5],
        "COOLDOWN_HOURS": int(sys.argv[6]),
        "PORT": int(sys.argv[7])
    }, f, indent=2, ensure_ascii=False)
PYGEN

pct push "$CTID" "$TMP_CONFIG" /etc/omada-watchdog.json --perms 0600

TMP_APP="${TMP_DIR}/app.py"
cat << 'PYEOF' > "$TMP_APP"
#!/usr/bin/env python3
import http.server
import json
import os
import socketserver
import subprocess
import threading
import time
import urllib.request
import ssl
from datetime import datetime

CONFIG_PATH = '/etc/omada-watchdog.json'
try:
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        config = json.load(f)
except Exception as e:
    print(f'BŁĄD: Nie można wczytać {CONFIG_PATH}: {e}')
    raise SystemExit(1)

OMADA_URL = config.get('OMADA_URL', '').rstrip('/')
CLIENT_ID = config.get('CLIENT_ID', '')
CLIENT_SECRET = config.get('CLIENT_SECRET', '')
ER605_MAC = config.get('ER605_MAC', '')
COOLDOWN_HOURS = int(config.get('COOLDOWN_HOURS', 3))
PORT = int(config.get('PORT', 8080))

CHECK_IPS = ['1.1.1.1', '8.8.8.8', '9.9.9.9']

state = {
    'status': 'Oczekiwanie...',
    'last_check': 'Brak',
    'last_reboot': 'Brak',
    'last_reboot_ts': 0,
    'logs': []
}

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    entry = f'[{ts}] {msg}'
    print(entry, flush=True)
    state['logs'].insert(0, entry)
    if len(state['logs']) > 50:
        state['logs'].pop()

def ping_target(ip):
    try:
        res = subprocess.run(['ping', '-c', '2', '-W', '2', ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    except Exception:
        return False

def check_internet():
    for attempt in range(1, 4):
        for ip in CHECK_IPS:
            if ping_target(ip):
                return True
        if attempt < 3:
            time.sleep(15)
    return False

def get_omada_token(ctx):
    req = urllib.request.Request(f'{OMADA_URL}/api/info')
    with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
        info_data = json.loads(r.read().decode())
    omadac_id = info_data.get('result', {}).get('omadacId')
    if not omadac_id:
        raise Exception('Brak omadacId w kontrolerze.')

    auth_payload = json.dumps({'omadacId': omadac_id, 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET}).encode('utf-8')
    auth_req = urllib.request.Request(
        f'{OMADA_URL}/openapi/authorize/token?grant_type=client_credentials',
        data=auth_payload,
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(auth_req, context=ctx, timeout=10) as r:
        token_data = json.loads(r.read().decode())
    
    token = token_data.get('result', {}).get('accessToken')
    if not token:
        err = token_data.get('msg', 'Brak tokena')
        raise Exception(f'Błąd logowania Open API: {err}')
    return omadac_id, token

def reboot_er605():
    ctx = ssl._create_unverified_context()
    omadac_id, token = get_omada_token(ctx)

    site_req = urllib.request.Request(
        f'{OMADA_URL}/openapi/v1/{omadac_id}/sites?pageSize=1',
        headers={'Authorization': f'AccessToken={token}'}
    )
    with urllib.request.urlopen(site_req, context=ctx, timeout=10) as r:
        site_data = json.loads(r.read().decode())
    sites = site_data.get('result', {}).get('data', [])
    site_id = sites[0]['siteId'] if sites else 'Default'

    body = json.dumps({'deviceMacs': [ER605_MAC]}).encode('utf-8')
    reboot_req = urllib.request.Request(
        f'{OMADA_URL}/openapi/v1/{omadac_id}/sites/{site_id}/cmd/devices/reboot',
        data=body,
        headers={'Authorization': f'AccessToken={token}', 'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(reboot_req, context=ctx, timeout=10) as r:
        res = json.loads(r.read().decode())
    
    if res.get('errorCode') != 0:
        raise Exception(res.get('msg', f'Błąd API: {res.get(\"errorCode\")}'))

def run_watchdog(manual=False):
    prefix = ' (test ręczny)' if manual else ''
    log(f'Sprawdzanie połączenia{prefix}...')
    online = check_internet()
    state['last_check'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if online:
        state['status'] = 'ONLINE'
        log('Łączność aktywna (ONLINE).')
        return

    state['status'] = 'OFFLINE'
    log('KRYTYCZNE: Brak internetu po 3 próbach (OFFLINE)!')

    now = time.time()
    cooldown_sec = COOLDOWN_HOURS * 3600
    elapsed = now - state['last_reboot_ts']

    if elapsed < cooldown_sec and not manual:
        rem = int((cooldown_sec - elapsed) / 60)
        log(f'Cooldown aktywny. Restart routera wstrzymany na {rem} min.')
        return

    try:
        log(f'Wysyłanie polecenia restartu ER605 ({ER605_MAC})...')
        reboot_er605()
        state['last_reboot_ts'] = now
        state['last_reboot'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log('SUKCES: Router ER605 został zrestartowany przez API!')
    except Exception as e:
        log(f'BŁĄD restartu: {e}')

def scheduler_thread():
    time.sleep(5)
    while True:
        run_watchdog(manual=False)
        time.sleep(1800)

HTML_PAGE = '''<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Omada ER605 Watchdog</title>
<style>
  :root {{ --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --muted: #94a3b8; --border: #334155; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 24px; display: flex; justify-content: center; }}
  .container {{ width: 100%; max-width: 680px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.4); }}
  .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }}
  h1 {{ font-size: 1.25rem; margin: 0; font-weight: 600; }}
  .badge {{ padding: 5px 12px; border-radius: 999px; font-size: 0.8rem; font-weight: 700; text-transform: uppercase; }}
  .badge.ONLINE {{ background: #14532d; color: #86efac; border: 1px solid #22c55e; }}
  .badge.OFFLINE {{ background: #7f1d1d; color: #fca5a5; border: 1px solid #ef4444; }}
  .badge.Oczekiwanie\\.\\.\\. {{ background: #334155; color: #cbd5e1; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 20px; }}
  .stat {{ background: #0b1120; border: 1px solid var(--border); padding: 14px; border-radius: 8px; }}
  .stat span {{ display: block; font-size: 0.75rem; color: var(--muted); margin-bottom: 4px; text-transform: uppercase; }}
  .stat b {{ font-size: 0.95rem; font-weight: 600; }}
  .actions {{ display: flex; gap: 10px; margin-bottom: 20px; }}
  button {{ border: 0; padding: 9px 16px; border-radius: 6px; font-weight: 600; font-size: 0.85rem; cursor: pointer; }}
  button:hover {{ opacity: 0.9; }}
  .btn-primary {{ background: #2563eb; color: #fff; }}
  .btn-danger {{ background: #dc2626; color: #fff; }}
  .console {{ background: #020617; border: 1px solid var(--border); border-radius: 8px; padding: 14px; font-family: monospace; font-size: 0.75rem; color: #cbd5e1; max-height: 280px; overflow-y: auto; line-height: 1.5; white-space: pre-wrap; }}
</style>
</head>
<body>
<div class="container">
  <div class="card">
    <div class="header">
      <h1>Omada ER605 Watchdog</h1>
      <span class="badge {status}">{status}</span>
    </div>
    <div class="grid">
      <div class="stat"><span>Ostatnie sprawdzenie</span><b>{last_check}</b></div>
      <div class="stat"><span>Ostatni restart routera</span><b>{last_reboot}</b></div>
    </div>
    <div class="actions">
      <form method="POST" action="/check" style="margin:0;"><button type="submit" class="btn-primary">Sprawdź teraz</button></form>
      <form method="POST" action="/reboot" style="margin:0;" onsubmit="return confirm('Wymusić restart ER605?');"><button type="submit" class="btn-danger">Wymuś restart ER605</button></form>
    </div>
    <div style="font-size: 0.75rem; color: var(--muted); margin-bottom: 6px;">Dziennik operacji:</div>
    <div class="console">{logs}</div>
  </div>
</div>
</body>
</html>'''

class WebHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        page = HTML_PAGE.format(
            status=state['status'],
            last_check=state['last_check'],
            last_reboot=state['last_reboot'],
            logs='\\n'.join(state['logs'])
        )
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(page.encode('utf-8'))

    def do_POST(self):
        if self.path == '/check':
            threading.Thread(target=run_watchdog, args=(True,)).start()
        elif self.path == '/reboot':
            threading.Thread(target=lambda: [log('Ręczne wymuszenie restartu...'), reboot_er605()]).start()
        self.send_response(303)
        self.send_header('Location', '/')
        self.end_headers()

if __name__ == '__main__':
    threading.Thread(target=scheduler_thread, daemon=True).start()
    with socketserver.TCPServer(('', PORT), WebHandler) as server:
        server.serve_forever()
PYEOF

pct push "$CTID" "$TMP_APP" /opt/omada-watchdog/app.py --perms 0755

TMP_SVC="${TMP_DIR}/omada-watchdog.service"
cat << 'SVCEOF' > "$TMP_SVC"
[Unit]
Description=Omada ER605 Watchdog Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/omada-watchdog
ExecStart=/usr/bin/python3 /opt/omada-watchdog/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SVCEOF

pct push "$CTID" "$TMP_SVC" /etc/systemd/system/omada-watchdog.service --perms 0644

echo -e "${YELLOW}>> Aktywacja i start usługi w kontenerze...${NC}"
pct exec "$CTID" -- systemctl daemon-reload
pct exec "$CTID" -- systemctl enable --now omada-watchdog.service

LXC_IP=""
for i in {1..10}; do
    LXC_IP=$(pct exec "$CTID" -- ip -4 addr show eth0 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -n 1 || true)
    if [ -n "$LXC_IP" ]; then
        break
    fi
    sleep 1
done

echo -e "\n${GREEN}${BOLD}================================================================${NC}"
echo -e "${GREEN}${BOLD}  INSTALACJA ZAKOŃCZONA SUKCESEM!${NC}"
echo -e "${BOLD}  Kontener ID:${NC}       ${CYAN}${CTID}${NC}"
echo -e "${BOLD}  Adres IP:${NC}           ${CYAN}${LXC_IP:-DHCP}${NC}"
echo -e "${BOLD}  Panel Web UI:${NC}       ${YELLOW}http://${LXC_IP:-IP_KONTENERA}:${WEB_PORT}${NC}"
echo -e "${BOLD}  Router:${NC}             ${GREEN}${D_NAME:-ER605} (${ER605_MAC})${NC}"
echo -e "${GREEN}${BOLD}================================================================${NC}\n"
