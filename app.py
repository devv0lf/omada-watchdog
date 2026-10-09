import os
import sys
import json
import time
import subprocess
import urllib.request
import ssl
import threading
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
    "reboot_history": [],
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

def add_reboot_log(trigger, status, code, details):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    item = {
        "timestamp": ts,
        "trigger": trigger,
        "status": status,
        "code": code,
        "details": details
    }
    if "reboot_history" not in state:
        state["reboot_history"] = []
    state["reboot_history"].insert(0, item)
    if len(state["reboot_history"]) > 50:
        state["reboot_history"].pop()
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

def reboot_router(trigger="MANUAL_WEB_UI"):
    mac = CONFIG['target_mac']
    log(f"Inicjalizacja restartu routera {mac} (trigger: {trigger})...")
    try:
        token = get_token()
        headers = {"Authorization": f"AccessToken={token}", "Content-Type": "application/json"}
        site_id = CONFIG.get("site_id")
        if not site_id:
            sites_url = f"{CONFIG['omada_url']}/openapi/v1/{CONFIG['omadac_id']}/sites?page=1&pageSize=100"
            req_sites = urllib.request.Request(sites_url, headers=headers)
            with urllib.request.urlopen(req_sites, context=ctx, timeout=10) as r:
                sites_res = json.loads(r.read().decode())
            sites = sites_res.get("result", {}).get("data", [])
            if sites:
                site_id = sites[0].get("siteId")

        reboot_url = f"{CONFIG['omada_url']}/openapi/v1/{CONFIG['omadac_id']}/sites/{site_id}/devices/{mac}/reboot"
        req_reboot = urllib.request.Request(reboot_url, data=b"{}", headers=headers)
        with urllib.request.urlopen(req_reboot, context=ctx, timeout=10) as r:
            res = json.loads(r.read().decode())
        
        err_code = res.get("errorCode")
        msg = res.get("msg", "OK")
        if err_code == 0:
            state["last_reboot"] = datetime.now().isoformat()
            log("Komenda restartu wysłana pomyślnie do kontrolera Omada!")
            add_reboot_log(trigger, "SUCCESS", err_code, msg)
        else:
            log(f"Błąd kontrolera Omada: {err_code} - {msg}")
            add_reboot_log(trigger, "FAILED", err_code, msg)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()[:150]
        log(f"HTTP Error {e.code}: {err_body}")
        add_reboot_log(trigger, "FAILED", f"HTTP_{e.code}", err_body)
    except Exception as e:
        log(f"Wyjątek restartu: {e}")
        add_reboot_log(trigger, "FAILED", "EXCEPTION", str(e))
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
                reboot_router(trigger="AUTO_WATCHDOG_FAIL")
                state["consecutive_failures"] = 0
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

        reboot_rows = ""
        reboots = state.get("reboot_history", [])
        if not reboots:
            reboot_rows = "<tr><td colspan='5' style='text-align:center;color:#559966;padding:12px;'>// BRAK ZAREJESTROWANYCH PRÓB RESTARTU</td></tr>"
        else:
            for r in reboots:
                status_color = "#00ff66" if r.get("status") == "SUCCESS" else "#ff3333"
                reboot_rows += f"""<tr>
                    <td>{r.get('timestamp')}</td>
                    <td>{r.get('trigger')}</td>
                    <td style='color:{status_color};font-weight:bold;'>[{r.get('status')}]</td>
                    <td>{r.get('code')}</td>
                    <td style='color:#cbd5e1;word-break:break-all;'>{r.get('details')}</td>
                </tr>"""

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
        .container {{ max-width: 950px; margin: 0 auto; }}
        .panel {{
            background: rgba(3, 14, 6, 0.85);
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
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
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
            font-size: 15px;
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
        table.matrix-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
            margin-top: 10px;
            background: rgba(0, 5, 2, 0.9);
        }}
        table.matrix-table th, table.matrix-table td {{
            border: 1px solid var(--matrix-dim);
            padding: 8px 10px;
            text-align: left;
        }}
        table.matrix-table th {{
            background: rgba(0, 255, 102, 0.08);
            color: #00ff66;
            letter-spacing: 1px;
        }}
        pre {{
            margin: 0;
            padding: 12px;
            background: rgba(0, 5, 2, 0.95);
            border: 1px solid var(--matrix-dim);
            color: #88cc99;
            font-size: 12px;
            line-height: 1.5;
            max-height: 220px;
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
            <div class="stat-label" style="margin-bottom:8px;">// REBOOT_HISTORY & AUDIT_LOG</div>
            <div style="overflow-x:auto;">
                <table class="matrix-table">
                    <thead>
                        <tr>
                            <th>TIMESTAMP</th>
                            <th>TRIGGER</th>
                            <th>STATUS</th>
                            <th>CODE</th>
                            <th>DETAILS / OMADA_RESPONSE</th>
                        </tr>
                    </thead>
                    <tbody>
                        {reboot_rows}
                    </tbody>
                </table>
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
            threading.Thread(target=reboot_router, args=("MANUAL_WEB_UI",)).start()
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
