#!/usr/bin/env bash
# ==============================================================================
# Omada ER605 Watchdog LXC Installer for Proxmox VE
# Style: Proxmox Community Helper-Scripts
# ==============================================================================
set -euo pipefail

# Kolory konsoli
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# Sprawdzenie środowiska Proxmox VE
if ! command -v pveversion >/dev/null 2>&1; then
    echo -e "${RED}Błąd: Ten skrypt musi być uruchomiony bezpośrednio w powłoce Proxmox VE!${NC}"
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
echo -e "${NC}${GREEN}Autoinstalator kontenera LXC z monitorem łącza i Web UI dla ER605${NC}\n"

# Funkcja bezpiecznego czytania z /dev/tty (kluczowa dla curl | bash)
prompt() {
    local var_name="$1"
    local prompt_text="$2"
    local default_val="$3"
    local input
    
    echo -ne "${BOLD}${prompt_text}${NC} [${YELLOW}${default_val}${NC}]: "
    read -r input </dev/tty || true
    if [ -z "$input" ]; then
        eval "$var_name=\"$default_val\""
    else
        eval "$var_name=\"$input\""
    fi
}

prompt_secret() {
    local var_name="$1"
    local prompt_text="$2"
    local input
    
    while true; do
        echo -ne "${BOLD}${prompt_text}${NC}: "
        read -r -s input </dev/tty || true
        echo ""
        if [ -n "$input" ]; then
            eval "$var_name=\"$input\""
            break
        fi
        echo -e "${RED}To pole jest wymagane!${NC}"
    done
}

echo -e "${BLUE}--- [1/3] Konfiguracja kontenera LXC ---${NC}"
NEXT_ID=$(pvesh get /cluster/nextid)
prompt CTID "Numer ID kontenera" "$NEXT_ID"
prompt HOSTNAME "Nazwa hosta" "omada-watchdog"
prompt RAM "Pamięć RAM (MB)" "512"
prompt DISK "Rozmiar dysku (GB)" "2"
prompt WEB_PORT "Port Web UI" "8080"

# Wykrywanie dostępnego magazynu danych
STORAGE="local-lvm"
if ! pvesm status -storage "$STORAGE" &>/dev/null; then
    STORAGE="local-zfs"
    if ! pvesm status -storage "$STORAGE" &>/dev/null; then
        STORAGE="local"
    fi
fi
prompt TARGET_STORAGE "Storage dla rootfs" "$STORAGE"

echo -e "\n${BLUE}--- [2/3] Konfiguracja Omada Open API ---${NC}"
prompt OMADA_URL "Adres URL kontrolera" "https://192.168.0.4"
prompt_secret CLIENT_ID "Omada Client ID"
prompt_secret CLIENT_SECRET "Omada Client Secret"

while true; do
    echo -ne "${BOLD}Adres MAC routera ER605 (np. AA-BB-CC-DD-EE-FF)${NC}: "
    read -r ER605_MAC </dev/tty || true
    ER605_MAC=$(echo "$ER605_MAC" | tr '[:lower:]' '[:upper:]' | tr ':' '-')
    if [[ "$ER605_MAC" =~ ^([0-9A-F]{2}-){5}[0-9A-F]{2}$ ]]; then
        break
    fi
    echo -e "${RED}Nieprawidłowy format MAC. Użyj formatu XX-XX-XX-XX-XX-XX lub XX:XX:XX:XX:XX:XX${NC}"
done

prompt COOLDOWN "Cooldown restartu (godziny)" "3"

echo -e "\n${BLUE}--- [3/3] Przygotowanie i tworzenie kontenera ---${NC}"

# Sprawdzenie i pobranie szablonu Debiana 12
echo -e "${YELLOW}>> Aktualizacja listy szablonów PVE...${NC}"
pveam update >/dev/null 2>&1 || true

TEMPLATE=$(pveam available -section system | awk '{print $2}' | grep -E 'debian-12-standard' | sort -V | tail -n 1 || true)
if [ -z "$TEMPLATE" ]; then
    TEMPLATE=$(pveam available -section system | awk '{print $2}' | grep -E 'debian' | sort -V | tail -n 1)
fi

echo -e "${YELLOW}>> Pobieranie szablonu $TEMPLATE do storage 'local'...${NC}"
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

echo -e "${YELLOW}>> Oczekiwanie na uruchomienie sieci w LXC...${NC}"
for i in {1..15}; do
    if pct exec "$CTID" -- ping -c 1 -W 1 1.1.1.1 >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

echo -e "${YELLOW}>> Instalacja pakietów wewnątrz LXC (Python3, Ping, Curl, jq)...${NC}"
pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq python3 iputils-ping curl jq ca-certificates" >/dev/null

echo -e "${YELLOW}>> Bezpieczny zapis poświadczeń do /etc/omada-watchdog.env...${NC}"
pct exec "$CTID" -- mkdir -p /opt/omada-watchdog /etc

# Plik środowiskowy zabezpieczony chmod 600
pct exec "$CTID" -- bash -c "cat << 'ENVEOF' > /etc/omada-watchdog.env
OMADA_URL='${OMADA_URL}'
CLIENT_ID='${CLIENT_ID}'
CLIENT_SECRET='${CLIENT_SECRET}'
ER605_MAC='${ER605_MAC}'
COOLDOWN_HOURS='${COOLDOWN}'
PORT='${WEB_PORT}'
ENVEOF
chmod 600 /etc/omada-watchdog.env"

echo -e "${YELLOW}>> Wdrażanie aplikacji mikro-serwisu Pythona...${NC}"
pct exec "$CTID" -- bash -c "cat << 'PYEOF' > /opt/omada-watchdog/app.py
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

OMADA_URL = os.environ.get('OMADA_URL', 'https://192.168.0.4').rstrip('/')
CLIENT_ID = os.environ.get('CLIENT_ID', '')
CLIENT_SECRET = os.environ.get('CLIENT_SECRET', '')
ER605_MAC = os.environ.get('ER605_MAC', '')
COOLDOWN_HOURS = int(os.environ.get('COOLDOWN_HOURS', '3'))
PORT = int(os.environ.get('PORT', '8080'))

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
    # 1. Pobranie omadacId
    req = urllib.request.Request(f'{OMADA_URL}/api/info')
    with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
        info_data = json.loads(r.read().decode())
    omadac_id = info_data.get('result', {}).get('omadacId')
    if not omadac_id:
        raise Exception('Nie udało się pobrać omadacId z kontrolera.')

    # 2. Pobranie tokena
    auth_body = json.dumps({'omadacId': omadac_id, 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET}).encode()
    auth_req = urllib.request.Request(
        f'{OMADA_URL}/openapi/authorize/token?grant_type=client_credentials',
        data=auth_body,
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(auth_req, context=ctx, timeout=10) as r:
        token_data = json.loads(r.read().decode())
    
    token = token_data.get('result', {}).get('accessToken')
    if not token:
        err = token_data.get('msg', 'Brak tokena')
        raise Exception(f'Błąd logowania API: {err}')
    return omadac_id, token

def reboot_er605():
    ctx = ssl._create_unverified_context()
    omadac_id, token = get_omada_token(ctx)

    # Pobranie siteId
    site_req = urllib.request.Request(
        f'{OMADA_URL}/openapi/v1/{omadac_id}/sites?pageSize=1',
        headers={'Authorization': f'AccessToken={token}'}
    )
    with urllib.request.urlopen(site_req, context=ctx, timeout=10) as r:
        site_data = json.loads(r.read().decode())
    sites = site_data.get('result', {}).get('data', [])
    site_id = sites[0]['siteId'] if sites else 'Default'

    # Wysłanie komendy restartu
    body = json.dumps({'deviceMacs': [ER605_MAC]}).encode()
    reboot_req = urllib.request.Request(
        f'{OMADA_URL}/openapi/v1/{omadac_id}/sites/{site_id}/cmd/devices/reboot',
        data=body,
        headers={'Authorization': f'AccessToken={token}', 'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(reboot_req, context=ctx, timeout=10) as r:
        res = json.loads(r.read().decode())
    
    if res.get('errorCode') != 0:
        raise Exception(res.get('msg', f'Kod błędu: {res.get(\"errorCode\")}'))

def run_watchdog(manual=False):
    prefix = ' (test ręczny)' if manual else ''
    log(f'Sprawdzanie stanu połączenia z internetem{prefix}...')
    online = check_internet()
    state['last_check'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if online:
        state['status'] = 'ONLINE'
        log('Łączność aktywna (ONLINE).')
        return

    state['status'] = 'OFFLINE'
    log('Brak internetu po 3 próbach (OFFLINE)!')

    now = time.time()
    cooldown_sec = COOLDOWN_HOURS * 3600
    elapsed = now - state['last_reboot_ts']

    if elapsed < cooldown_sec and not manual:
        rem = int((cooldown_sec - elapsed) / 60)
        log(f'Cooldown aktywny. Restart wstrzymany na jeszcze {rem} min.')
        return

    try:
        log(f'Wysyłanie polecenia restartu ER605 ({ER605_MAC})...')
        reboot_er605()
        state['last_reboot_ts'] = now
        state['last_reboot'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log('SUKCES: Polecenie restartu ER605 zostało zaakceptowane przez kontroler.')
    except Exception as e:
        log(f'BŁĄD procedury restartu: {e}')

def scheduler_thread():
    time.sleep(5)
    while True:
        run_watchdog(manual=False)
        time.sleep(1800)  # 30 minut

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
  .badge {{ padding: 5px 12px; border-radius: 999px; font-size: 0.8rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; }}
  .badge.ONLINE {{ background: #14532d; color: #86efac; border: 1px solid #22c55e; }}
  .badge.OFFLINE {{ background: #7f1d1d; color: #fca5a5; border: 1px solid #ef4444; }}
  .badge.Oczekiwanie\\.\\.\\. {{ background: #334155; color: #cbd5e1; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 20px; }}
  .stat {{ background: #0b1120; border: 1px solid var(--border); padding: 14px; border-radius: 8px; }}
  .stat span {{ display: block; font-size: 0.75rem; color: var(--muted); margin-bottom: 4px; text-transform: uppercase; }}
  .stat b {{ font-size: 0.95rem; font-weight: 600; }}
  .actions {{ display: flex; gap: 10px; margin-bottom: 20px; }}
  button {{ border: 0; padding: 9px 16px; border-radius: 6px; font-weight: 600; font-size: 0.85rem; cursor: pointer; transition: opacity 0.2s; }}
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
      <form method="POST" action="/reboot" style="margin:0;" onsubmit="return confirm('Wymusić natychmiastowy restart ER605?');"><button type="submit" class="btn-danger">Wymuś restart ER605</button></form>
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
        self.wfile.write(page.encode())

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
chmod +x /opt/omada-watchdog/app.py"

echo -e "${YELLOW}>> Konfiguracja i uruchomienie usługi systemd...${NC}"
pct exec "$CTID" -- bash -c "cat << 'SVCEOF' > /etc/systemd/system/omada-watchdog.service
[Unit]
Description=Omada ER605 Watchdog Service
After=network.target

[Service]
Type=simple
User=root
EnvironmentFile=/etc/omada-watchdog.env
WorkingDirectory=/opt/omada-watchdog
ExecStart=/usr/bin/python3 /opt/omada-watchdog/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SVCEOF
systemctl daemon-reload
systemctl enable --now omada-watchdog.service
"

# Odczytanie przydzielonego adresu IP
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
echo -e "${BOLD}  Plik konfiguracji:${NC}  ${CYAN}/etc/omada-watchdog.env${NC} (wewnątrz LXC)"
echo -e "${GREEN}${BOLD}================================================================${NC}\n"
