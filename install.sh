#!/usr/bin/env bash
# ==============================================================================
# Omada ER605 Watchdog - Autoinstalator LXC dla Proxmox VE (7 / 8 / 9)
# Uruchomienie: bash -c "$(curl -fsSL https://raw.githubusercontent.com/TWOJ_USER/omada-watchdog/main/install.sh)"
# ==============================================================================
set -euo pipefail

# Przywrócenie ustawień terminala
stty sane 2>/dev/null || true

# Sprawdzenie środowiska Proxmox
if [ ! -f /etc/pve/pve-root-ca.pem ]; then
    echo "[-] Błąd: Ten skrypt należy uruchomić bezpośrednio na hoście Proxmox VE." >&2
    exit 1
fi

clear
cat << "EOF"
  ___                     _         __      __     _       _         _             
  / _ \ _ __ ___   __ _  __| | __ _  \ \    / /__ _| |_ ___| |__   __| | ___   __ _ 
 | | | | '_ ` _ \ / _` |/ _` |/ _` |  \ \/\/ / _` | __/ __| '_ \ / _` |/ _ \ / _` |
 | |_| | | | | | | (_| | (_| | (_| |   \  /\  (_| | || (__| | | | (_| | (_) | (_| |
  \___/|_| |_| |_|\__,_|\__,_|\__,_|    \/  \__,_|\__\___|_| |_|\__,_|\___/ \__, |
                                                                             |___/  
Autoinstalator LXC dla Omada ER605 Watchdog [Cyberpunk / Matrix Edition]
EOF

prompt() {
    local var_name="$1"
    local question="$2"
    local default_val="${3:-}"
    local input=""

    while true; do
        if [ -n "$default_val" ]; then
            read -r -e -p "$question [$default_val]: " input </dev/tty || true
            input="${input:-$default_val}"
        else
            read -r -e -p "$question: " input </dev/tty || true
        fi
        input="$(echo -n "$input" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        if [ -n "$input" ]; then
            printf -v "$var_name" "%s" "$input"
            break
        fi
    done
}

echo -e "\n--- [1/4] Parametry kontenera LXC ---"
NEXT_ID=$(pvesh get /cluster/nextid 2>/dev/null || echo "107")
prompt CT_ID "Numer ID nowego kontenera" "$NEXT_ID"
prompt CT_HOSTNAME "Nazwa kontenera" "omada-watchdog"
prompt CT_RAM "Pamięć RAM w MB" "512"
prompt CT_DISK "Rozmiar dysku w GB" "2"
prompt WEB_PORT "Port Web UI" "8080"

DEFAULT_STORAGE="local-lvm"
if ! pvesm status -storage "$DEFAULT_STORAGE" &>/dev/null; then
    DEFAULT_STORAGE=$(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 {print $1; exit}')
    DEFAULT_STORAGE="${DEFAULT_STORAGE:-local}"
fi
prompt CT_STORAGE "Storage dla rootfs" "$DEFAULT_STORAGE"

echo -e "\n--- [2/4] Konfiguracja Omada Open API ---"
prompt OMADA_URL "Adres URL kontrolera" "https://192.168.0.4"
prompt OMADA_ID "Omada ID (z Open API Attributes)" ""
prompt CLIENT_ID "Omada Client ID" ""
prompt CLIENT_SECRET "Omada Client Secret" ""
prompt TARGET_MAC "Adres MAC routera ER605" ""
TARGET_MAC=$(echo "$TARGET_MAC" | tr '[:lower:]' '[:upper:]' | tr ':' '-')

echo ">> Weryfikacja połączenia i wyszukiwanie routera w Omada API..."
VERIFY_OUTPUT=$(python3 - "$OMADA_URL" "$OMADA_ID" "$CLIENT_ID" "$CLIENT_SECRET" "$TARGET_MAC" << 'EOF'
import sys, json, ssl, urllib.request

url, omadac_id, client_id, client_secret, target_mac = sys.argv[1:6]
ctx = ssl._create_unverified_context()

try:
    token_url = f"{url.rstrip('/')}/openapi/authorize/token?grant_type=client_credentials"
    auth_data = json.dumps({"omadacId": omadac_id, "client_id": client_id, "client_secret": client_secret}).encode()
    req = urllib.request.Request(token_url, data=auth_data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
        token_res = json.loads(r.read().decode())
    
    if token_res.get('errorCode') != 0:
        print(f"ERROR: Błąd autoryzacji: {token_res.get('msg', 'Nieznany błąd')}")
        sys.exit(1)
        
    token = token_res['result']['accessToken']
    headers = {"AccessToken": token, "Content-Type": "application/json"}

    sites_url = f"{url.rstrip('/')}/openapi/v1/{omadac_id}/sites?page=1&pageSize=100"
    req_sites = urllib.request.Request(sites_url, headers=headers)
    with urllib.request.urlopen(req_sites, context=ctx, timeout=10) as r:
        sites_res = json.loads(r.read().decode())
    
    sites = sites_res.get('result', {}).get('data', [])
    if not sites:
        print("ERROR: Brak witryn przypisanych do aplikacji w kontrolerze.")
        sys.exit(1)

    found_site = None
    device_info = None
    for s in sites:
        s_id = s.get('siteId')
        dev_url = f"{url.rstrip('/')}/openapi/v1/{omadac_id}/sites/{s_id}/devices?page=1&pageSize=100"
        req_dev = urllib.request.Request(dev_url, headers=headers)
        with urllib.request.urlopen(req_dev, context=ctx, timeout=10) as r:
            dev_res = json.loads(r.read().decode())
        
        for dev in dev_res.get('result', {}).get('data', []):
            d_mac = dev.get('mac', '').replace(':', '-').upper()
            if d_mac == target_mac:
                found_site = s
                device_info = dev
                break
        if found_site:
            break

    if found_site and device_info:
        print("SUCCESS")
        print(found_site.get('siteId'))
        print(found_site.get('name', 'Brak'))
        print(device_info.get('name', 'Brak'))
        print(device_info.get('model', 'ER605'))
    else:
        print(f"NOT_FOUND: Nie znaleziono urządzenia o MAC {target_mac}.")
        sys.exit(2)

except Exception as e:
    print(f"ERROR: Wyjątek podczas weryfikacji API: {e}")
    sys.exit(1)
EOF
) || true

STATUS=$(echo "$VERIFY_OUTPUT" | head -n 1)

if [ "$STATUS" == "SUCCESS" ]; then
    SITE_ID=$(echo "$VERIFY_OUTPUT" | sed -n '2p')
    SITE_NAME=$(echo "$VERIFY_OUTPUT" | sed -n '3p')
    DEV_NAME=$(echo "$VERIFY_OUTPUT" | sed -n '4p')
    DEV_MODEL=$(echo "$VERIFY_OUTPUT" | sed -n '5p')
    echo "✓ Połączenie z API powiodło się!"
    echo "  Router:  $DEV_NAME ($DEV_MODEL)"
    echo "  Witryna: $SITE_NAME (ID: $SITE_ID)"
else
    echo "[-] Ostrzeżenie weryfikacji:"
    echo "$VERIFY_OUTPUT"
    read -r -p "Kontynuować mimo to? (t/N): " FORCE_CONT </dev/tty || true
    if [[ ! "$FORCE_CONT" =~ ^[tTyY]$ ]]; then
        echo "Przerwano."
        exit 1
    fi
    SITE_ID=""
fi

prompt COOLDOWN_HOURS "Czas cooldownu po restarcie (w godzinach)" "3"

echo -e "\n--- [3/4] Pobieranie szablonu i tworzenie kontenera ---"
pveam update >/dev/null 2>&1 || true
TEMPLATE=$(pveam available -section system | grep "debian-12-standard" | awk '{print $2}' | sort -V | tail -n 1)

TEMPLATE_STORAGE="local"
if ! pvesm status -storage "$TEMPLATE_STORAGE" &>/dev/null; then
    TEMPLATE_STORAGE="$CT_STORAGE"
fi

if [ ! -f "/var/lib/vz/template/cache/$TEMPLATE" ]; then
    echo ">> Pobieranie szablonu OS ($TEMPLATE)..."
    pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

echo ">> Tworzenie kontenera LXC ($CT_ID)..."
pct create "$CT_ID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
    --hostname "$CT_HOSTNAME" \
    --cores 1 \
    --memory "$CT_RAM" \
    --swap 256 \
    --features nesting=1 \
    --net0 name=eth0,bridge=vmbr0,ip=dhcp \
    --storage "$CT_STORAGE" \
    --rootfs "${CT_STORAGE}:${CT_DISK}" \
    --onboot 1 \
    --unprivileged 1 \
    --start 1

echo ">> Oczekiwanie na sieć w kontenerze..."
for i in {1..20}; do
    CT_IP=$(pct exec "$CT_ID" -- ip -4 addr show eth0 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' || true)
    if [ -n "$CT_IP" ]; then
        break
    fi
    sleep 1
done

echo ">> Instalacja pakietów w LXC (python3, ping, curl)..."
pct exec "$CT_ID" -- bash -c "DEBIAN_FRONTEND=noninteractive apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 iputils-ping curl ca-certificates" >/dev/null

echo -e "\n--- [4/4] Konfiguracja aplikacji i start serwisu ---"
pct exec "$CT_ID" -- mkdir -p /opt/omada-watchdog

TMP_CONF=$(mktemp)
python3 - "$OMADA_URL" "$OMADA_ID" "$CLIENT_ID" "$CLIENT_SECRET" "$SITE_ID" "$TARGET_MAC" "$COOLDOWN_HOURS" "$WEB_PORT" "$TMP_CONF" << 'EOF'
import sys, json

data = {
    "omada_url": sys.argv[1].rstrip('/'),
    "omadac_id": sys.argv[2],
    "client_id": sys.argv[3],
    "client_secret": sys.argv[4],
    "site_id": sys.argv[5],
    "target_mac": sys.argv[6].replace(':', '-').upper(),
    "cooldown_hours": float(sys.argv[7]),
    "web_port": int(sys.argv[8]),
    "ping_hosts": ["1.1.1.1", "8.8.8.8", "9.9.9.9"],
    "check_interval_minutes": 30,
    "max_retries": 3,
    "retry_delay_seconds": 15
}

with open(sys.argv[9], 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=2)
EOF

pct push "$CT_ID" "$TMP_CONF" /opt/omada-watchdog/config.json
rm -f "$TMP_CONF"
pct exec "$CT_ID" -- chmod 0600 /opt/omada-watchdog/config.json

# Wgranie kodu aplikacji
TMP_APP=$(mktemp)
cat << 'EOF' > "$TMP_APP"
import os, sys, json, time, subprocess, urllib.request, ssl, threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

CONFIG_FILE = "/opt/omada-watchdog/config.json"
STATE_FILE = "/opt/omada-watchdog/state.json"

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

ctx = ssl._create_unverified_context()

state = {
    "last_check": None,
    "last_status": "Oczekiwanie...",
    "last_reboot": None,
    "last_latency_ms": None,
    "consecutive_failures": 0,
    "history_24h": [],
    "logs": []
}

if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state.update(json.load(f))
    except Exception:
        pass

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] {msg}"
    print(entry, flush=True)
    state["logs"].append(entry)
    if len(state["logs"]) > 100:
        state["logs"].pop(0)
    save_state()

def check_internet():
    for host in CONFIG.get("ping_hosts", ["1.1.1.1", "8.8.8.8", "9.9.9.9"]):
        try:
            t0 = time.time()
            res = subprocess.run(["ping", "-c", "1", "-W", "2", host], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            if res.returncode == 0:
                dt = (time.time() - t0) * 1000
                return True, round(dt, 1)
        except Exception:
            pass
    return False, None

def get_token():
    url = f"{CONFIG['omada_url']}/openapi/authorize/token?grant_type=client_credentials"
    data = json.dumps({
        "omadacId": CONFIG["omadac_id"],
        "client_id": CONFIG["client_id"],
        "client_secret": CONFIG["client_secret"]
    }).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
        res = json.loads(r.read().decode())
    if res.get("errorCode") != 0:
        err = res.get("errorCode")
        raise Exception(res.get("msg") or f"Błąd API tokena: {err}")
    return res["result"]["accessToken"]

def reboot_router():
    log(f"Inicjalizacja restartu routera {CONFIG['target_mac']}...")
    token = get_token()
    headers = {"AccessToken": token, "Content-Type": "application/json"}
    site_id = CONFIG.get("site_id")
    if not site_id:
        sites_url = f"{CONFIG['omada_url']}/openapi/v1/{CONFIG['omadac_id']}/sites?page=1&pageSize=100"
        req_sites = urllib.request.Request(sites_url, headers=headers)
        with urllib.request.urlopen(req_sites, context=ctx, timeout=10) as r:
            sites_res = json.loads(r.read().decode())
        sites = sites_res.get("result", {}).get("data", [])
        if sites:
            site_id = sites[0].get("siteId")

    reboot_url = f"{CONFIG['omada_url']}/openapi/v1/{CONFIG['omadac_id']}/sites/{site_id}/cmd/devices/{CONFIG['target_mac']}/reboot"
    req_reboot = urllib.request.Request(reboot_url, data=b"{}", headers=headers)
    with urllib.request.urlopen(req_reboot, context=ctx, timeout=10) as r:
        res = json.loads(r.read().decode())
    
    if res.get("errorCode") != 0:
        err = res.get("errorCode")
        raise Exception(res.get("msg") or f"Błąd API rebootu: {err}")
    
    state["last_reboot"] = datetime.now().isoformat()
    log("Komenda restartu wysłana pomyślnie do kontrolera Omada!")
    save_state()

def run_watchdog(manual=False):
    prefix = " [MANUAL]" if manual else ""
    log(f"Sprawdzanie łączności WAN{prefix}...")
    online, lat = check_internet()
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    state["last_check"] = now_str
    state["last_latency_ms"] = lat

    cutoff = (now - timedelta(hours=24)).isoformat()
    state["history_24h"] = [p for p in state.get("history_24h", []) if p.get("ts", "") > cutoff]
    state["history_24h"].append({
        "ts": now.isoformat(),
        "time": now.strftime("%H:%M"),
        "status": "ONLINE" if online else "OFFLINE",
        "ms": lat if online else 0
    })

    if online:
        state["last_status"] = "ONLINE"
        state["consecutive_failures"] = 0
        log(f"Status: ONLINE ({lat} ms){prefix}")
    else:
        state["last_status"] = "OFFLINE"
        state["consecutive_failures"] += 1
        log(f"ALERT: Brak WAN! Próba {state['consecutive_failures']}/{CONFIG.get('max_retries', 3)}")
        
        if state["consecutive_failures"] >= CONFIG.get("max_retries", 3):
            in_cooldown = False
            if state.get("last_reboot"):
                try:
                    lr = datetime.fromisoformat(state["last_reboot"])
                    if datetime.now() - lr < timedelta(hours=CONFIG.get("cooldown_hours", 3)):
                        in_cooldown = True
                except Exception:
                    pass
            if in_cooldown:
                log("Cooldown aktywny — pomijanie procedury restartu.")
            else:
                try:
                    reboot_router()
                    state["consecutive_failures"] = 0
                except Exception as e:
                    log(f"CRITICAL: Błąd restartu: {e}")
    save_state()

def watchdog_loop():
    while True:
        try:
            run_watchdog(manual=False)
        except Exception as e:
            log(f"Pętla błędu: {e}")
        time.sleep(CONFIG.get("check_interval_minutes", 30) * 60)

class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(state).encode())
            return
        
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

        cooldown_str = "BRAK"
        if state.get("last_reboot"):
            try:
                lr = datetime.fromisoformat(state["last_reboot"])
                rem = timedelta(hours=CONFIG.get("cooldown_hours", 3)) - (datetime.now() - lr)
                if rem.total_seconds() > 0:
                    mins = int(rem.total_seconds() // 60)
                    cooldown_str = f"AKTYWNY (~{mins} MIN)"
                else:
                    cooldown_str = "WYGASŁY"
            except Exception:
                pass

        history_json = json.dumps(state.get("history_24h", []))
        logs_html = "\n".join(reversed(state.get("logs", [])))

        html = f"""<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <title>OMADA_WATCHDOG // ROOT_ACCESS</title>
    <style>
        :root {{
            --matrix-green: #00ff66;
            --matrix-glow: rgba(0, 255, 102, 0.4);
            --matrix-dim: #003b14;
            --matrix-red: #ff3333;
            --mouse-x: 50vw;
            --mouse-y: 50vh;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            padding: 30px 20px;
            font-family: "Consolas", "Courier New", monospace;
            background-color: #030804;
            background-image: radial-gradient(700px circle at var(--mouse-x) var(--mouse-y), rgba(0, 255, 102, 0.08), transparent 70%);
            background-attachment: fixed;
            color: var(--matrix-green);
            min-height: 100vh;
        }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        .panel {{
            background: rgba(3, 14, 6, 0.82);
            border: 1px solid var(--matrix-green);
            box-shadow: 0 0 15px var(--matrix-dim), inset 0 0 10px rgba(0, 255, 102, 0.05);
            padding: 24px;
            margin-bottom: 24px;
            position: relative;
        }}
        .panel::before {{
            content: "// SECURE_SYSTEM_SHELL";
            position: absolute;
            top: -9px;
            left: 16px;
            background: #030804;
            padding: 0 8px;
            font-size: 11px;
            letter-spacing: 2px;
            color: #00ff66;
            opacity: 0.8;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px dashed var(--matrix-dim);
            padding-bottom: 16px;
            margin-bottom: 20px;
        }}
        h1 {{
            margin: 0;
            font-size: 20px;
            letter-spacing: 2px;
            text-shadow: 0 0 8px var(--matrix-glow);
        }}
        .badge {{
            padding: 4px 12px;
            font-size: 14px;
            letter-spacing: 2px;
            border: 1px solid currentColor;
            font-weight: bold;
        }}
        .badge.online {{
            color: #00ff66;
            border-color: #00ff66;
            box-shadow: 0 0 10px rgba(0, 255, 102, 0.5);
        }}
        .badge.offline {{
            color: var(--matrix-red);
            border-color: var(--matrix-red);
            box-shadow: 0 0 10px rgba(255, 51, 51, 0.5);
            animation: pulse 1s infinite alternate;
        }}
        @keyframes pulse {{
            from {{ opacity: 0.4; }}
            to {{ opacity: 1; }}
        }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
            margin-bottom: 20px;
        }}
        .stat {{
            background: rgba(0, 255, 102, 0.03);
            border: 1px solid var(--matrix-dim);
            padding: 12px;
        }}
        .stat-label {{
            font-size: 11px;
            color: #559966;
            letter-spacing: 1px;
            margin-bottom: 4px;
        }}
        .stat-value {{
            font-size: 16px;
            font-weight: bold;
            color: #fff;
            text-shadow: 0 0 5px var(--matrix-green);
        }}
        .actions {{ display: flex; gap: 12px; margin-top: 10px; }}
        .btn {{
            background: transparent;
            color: var(--matrix-green);
            border: 1px solid var(--matrix-green);
            padding: 10px 20px;
            font-family: inherit;
            font-size: 13px;
            letter-spacing: 1px;
            cursor: pointer;
            transition: all 0.2s ease;
            box-shadow: 0 0 8px rgba(0, 255, 102, 0.2);
        }}
        .btn:hover {{
            background: var(--matrix-green);
            color: #000;
            box-shadow: 0 0 16px var(--matrix-green);
        }}
        .btn-danger {{
            color: var(--matrix-red);
            border-color: var(--matrix-red);
            box-shadow: 0 0 8px rgba(255, 51, 51, 0.2);
        }}
        .btn-danger:hover {{
            background: var(--matrix-red);
            color: #000;
            box-shadow: 0 0 16px var(--matrix-red);
        }}
        .chart-box {{
            margin-top: 10px;
            background: rgba(0, 10, 3, 0.9);
            border: 1px solid var(--matrix-dim);
            padding: 12px;
        }}
        svg text {{
            font-family: Consolas, monospace;
            font-size: 10px;
            fill: #559966;
        }}
        pre {{
            margin: 0;
            padding: 12px;
            background: rgba(0, 5, 2, 0.95);
            border: 1px solid var(--matrix-dim);
            color: #88cc99;
            font-size: 12px;
            line-height: 1.5;
            max-height: 240px;
            overflow-y: auto;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="panel">
            <div class="header">
                <h1>&gt; OMADA_WATCHDOG [ER605]</h1>
                <span class="badge { 'online' if state['last_status'] == 'ONLINE' else 'offline' }">[ {state['last_status']} ]</span>
            </div>

            <div class="grid">
                <div class="stat">
                    <div class="stat-label">TARGET_DEVICE</div>
                    <div class="stat-value">{CONFIG.get('target_mac')}</div>
                </div>
                <div class="stat">
                    <div class="stat-label">LAST_LATENCY</div>
                    <div class="stat-value">{state.get('last_latency_ms') or '--'} ms</div>
                </div>
                <div class="stat">
                    <div class="stat-label">LAST_CHECK</div>
                    <div class="stat-value">{state.get('last_check') or 'N/A'}</div>
                </div>
                <div class="stat">
                    <div class="stat-label">COOLDOWN_STATE</div>
                    <div class="stat-value">{cooldown_str}</div>
                </div>
                <div class="stat">
                    <div class="stat-label">FAIL_COUNTER</div>
                    <div class="stat-value">{state.get('consecutive_failures', 0)} / {CONFIG.get('max_retries', 3)}</div>
                </div>
            </div>

            <div class="actions">
                <form method="POST" action="/check" style="margin:0;">
                    <button class="btn" type="submit">&gt; CHECK_NOW()</button>
                </form>
                <form method="POST" action="/reboot" style="margin:0;" onsubmit="return confirm('WARNING: Wymusić restart routera ER605 natychmiast?');">
                    <button class="btn btn-danger" type="submit">&gt; FORCE_REBOOT_ER605()</button>
                </form>
            </div>
        </div>

        <div class="panel">
            <div class="stat-label" style="margin-bottom:8px;">// LATENCY & STATUS TIMELINE (LAST 24 HOURS)</div>
            <div class="chart-box">
                <svg id="chart" viewBox="0 0 800 160" width="100%" height="160"></svg>
            </div>
        </div>

        <div class="panel">
            <div class="stat-label" style="margin-bottom:8px;">// SYSTEM_EVENT_LOG</div>
            <pre>{logs_html}</pre>
        </div>
    </div>

    <script>
        document.addEventListener('mousemove', (e) => {{
            document.documentElement.style.setProperty('--mouse-x', e.clientX + 'px');
            document.documentElement.style.setProperty('--mouse-y', e.clientY + 'px');
        }});

        const historyData = {history_json};
        const svg = document.getElementById('chart');

        function drawChart() {{
            svg.innerHTML = '';
            const w = 800, h = 160, pad = 30;
            const innerW = w - pad * 2, innerH = h - pad * 2;

            svg.innerHTML += `<line x1="${{pad}}" y1="${{pad}}" x2="${{w-pad}}" y2="${{pad}}" stroke="#003b14" stroke-dasharray="4" />`;
            svg.innerHTML += `<line x1="${{pad}}" y1="${{pad + innerH/2}}" x2="${{w-pad}}" y2="${{pad + innerH/2}}" stroke="#003b14" stroke-dasharray="4" />`;
            svg.innerHTML += `<line x1="${{pad}}" y1="${{h-pad}}" x2="${{w-pad}}" y2="${{h-pad}}" stroke="#00ff66" stroke-width="1.5" />`;

            if (!historyData || historyData.length === 0) {{
                svg.innerHTML += `<text x="${{w/2}}" y="${{h/2}}" text-anchor="middle" fill="#559966">// Oczekiwanie na próbki danych z kolejnych sprawdzeń...</text>`;
                return;
            }}

            const maxMs = Math.max(50, ...historyData.map(d => d.ms || 0));
            svg.innerHTML += `<text x="${{pad - 5}}" y="${{pad + 4}}" text-anchor="end">${{Math.round(maxMs)}}ms</text>`;
            svg.innerHTML += `<text x="${{pad - 5}}" y="${{h - pad}}" text-anchor="end">0ms</text>`;

            const pts = historyData.map((d, i) => {{
                const x = pad + (i / Math.max(1, historyData.length - 1)) * innerW;
                const normY = d.status === 'ONLINE' ? (d.ms / maxMs) : 0;
                const y = (h - pad) - normY * innerH;
                return {{ x, y, ...d }};
            }});

            pts.forEach(p => {{
                const col = p.status === 'ONLINE' ? '#00ff66' : '#ff3333';
                svg.innerHTML += `<line x1="${{p.x}}" y1="${{h-pad}}" x2="${{p.x}}" y2="${{p.y}}" stroke="${{col}}" stroke-width="3" opacity="0.6"/>`;
                svg.innerHTML += `<circle cx="${{p.x}}" cy="${{p.y}}" r="4" fill="${{col}}" stroke="#030804" stroke-width="2"/>`;
                svg.innerHTML += `<text x="${{p.x}}" y="${{h - pad + 15}}" text-anchor="middle" font-size="9">${{p.time}}</text>`;
            }});
        }}
        drawChart();
    </script>
</body>
</html>
"""
        self.wfile.write(html.encode("utf-8"))

    def do_POST(self):
        if self.path == "/check":
            threading.Thread(target=run_watchdog, args=(True,)).start()
        elif self.path == "/reboot":
            threading.Thread(target=reboot_router).start()
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

def run_server():
    server = HTTPServer(("0.0.0.0", CONFIG.get("web_port", 8080)), RequestHandler)
    server.serve_forever()

if __name__ == "__main__":
    t_watchdog = threading.Thread(target=watchdog_loop, daemon=True)
    t_watchdog.start()
    run_server()
EOF

pct push "$CT_ID" "$TMP_APP" /opt/omada-watchdog/app.py
rm -f "$TMP_APP"

pct exec "$CT_ID" -- bash -c "cat << 'EOF' > /etc/systemd/system/omada-watchdog.service
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
EOF
"

echo ">> Aktywacja usługi..."
pct exec "$CT_ID" -- systemctl daemon-reload
pct exec "$CT_ID" -- systemctl enable --now omada-watchdog

FINAL_IP=$(pct exec "$CT_ID" -- ip -4 addr show eth0 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' || echo "$CT_IP")

echo "================================================================"
echo "  INSTALACJA ZAKOŃCZONA SUKCESEM!"
echo "  Kontener ID:  $CT_ID"
echo "  Adres IP:     $FINAL_IP"
echo "  Panel Web UI: http://${FINAL_IP}:${WEB_PORT}"
echo "================================================================"
