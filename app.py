import os
import json
import time
import subprocess
import urllib.request
import urllib.error
import ssl
import threading
import ipaddress
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit

CONFIG_FILE = "/opt/omada-watchdog/config.json"
STATE_FILE = "/opt/omada-watchdog/state.json"


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    defaults = {
        "omada_url": "https://192.168.0.4",
        "omadac_id": "",
        "client_id": "",
        "client_secret": "",
        "target_mac": "",
        "site_id": "",
        "site_name": "",
        "web_port": 8080,
        "cooldown_hours": 3,
        "check_interval_minutes": 30,
        "retry_interval_minutes": 1,
        "max_retries": 3,
        "ping_targets": ["1.1.1.1", "8.8.8.8"]
    }
    for key, value in defaults.items():
        cfg.setdefault(key, value)
    return cfg


CONFIG = load_config()
SSL_CONTEXT = ssl._create_unverified_context()
STATE_LOCK = threading.RLock()
API_LOCK = threading.Lock()

state = {
    "last_check": None,
    "last_status": "Oczekiwanie...",
    "last_reboot": None,
    "last_latency_ms": None,
    "consecutive_failures": 0,
    "current_interval_minutes": CONFIG["check_interval_minutes"],
    "history_24h": [],
    "reboot_history": [],
    "diagnostic_history": [],
    "logs": []
}


def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            with STATE_LOCK:
                state.update(saved)
    except Exception:
        pass


def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp_path = STATE_FILE + ".tmp"
        with STATE_LOCK:
            snapshot = dict(state)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2, ensure_ascii=False)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, STATE_FILE)
    except Exception as exc:
        print(f"Błąd zapisu stanu: {exc}", flush=True)


def add_log(message):
    entry = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(entry, flush=True)
    with STATE_LOCK:
        state.setdefault("logs", []).insert(0, entry)
        state["logs"] = state["logs"][:200]
    save_state()


load_state()

_TOKEN_CACHE = {"token": None, "expires_at": 0}


def get_omada_token(force_refresh=False):
    with API_LOCK:
        now = time.time()
        if (not force_refresh and _TOKEN_CACHE["token"]
                and now < _TOKEN_CACHE["expires_at"]):
            return _TOKEN_CACHE["token"]

        url = CONFIG["omada_url"].rstrip("/")
        url += "/openapi/authorize/token?grant_type=client_credentials"
        payload = json.dumps({
            "omadacId": CONFIG["omadac_id"],
            "client_id": CONFIG["client_id"],
            "client_secret": CONFIG["client_secret"]
        }).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(request, context=SSL_CONTEXT, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))

        if result.get("errorCode", 0) != 0 or "result" not in result:
            raise RuntimeError(f"Omada token error: {result}")

        token = result["result"]["accessToken"]
        expires_in = int(result["result"].get("expiresIn", 7200))
        _TOKEN_CACHE["token"] = token
        _TOKEN_CACHE["expires_at"] = now + max(60, expires_in - 120)
        return token


def omada_api_request(method, endpoint, body=None):
    """Wysyła żądanie do Open API. Endpoint może być relatywny lub pełny /openapi/."""
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        raise ValueError("Endpoint API jest pusty")
    if "\r" in endpoint or "\n" in endpoint:
        raise ValueError("Nieprawidłowy endpoint")

    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        raise ValueError("Dozwolone są tylko ścieżki API, nie pełne URL-e")

    base_url = CONFIG["omada_url"].rstrip("/")
    if endpoint.startswith("/openapi/"):
        path = endpoint
    elif endpoint.startswith("openapi/"):
        path = "/" + endpoint
    else:
        path = (
            f"/openapi/v1/{CONFIG['omadac_id']}/sites/"
            f"{CONFIG['site_id']}/{endpoint.lstrip('/')}"
        )

    token = get_omada_token()
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"AccessToken={token}",
        "Content-Type": "application/json"
    }
    request = urllib.request.Request(
        base_url + path, data=data, headers=headers, method=method.upper()
    )

    try:
        with urllib.request.urlopen(request, context=SSL_CONTEXT, timeout=12) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"raw": raw[:4000]}
            return parsed, response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw[:4000]}
        return parsed, exc.code


OMADA_ERROR_CODES = {
    0: "Operacja zakończona powodzeniem",
    -1000: "Nieprawidłowe parametry żądania",
    -1001: "Brak wymaganego parametru",
    -1002: "Brak autoryzacji lub wygasły token",
    -1003: "Nie znaleziono zasobu",
    -1004: "Brak uprawnień",
    -3000: "Urządzenie offline lub niedostępne dla kontrolera",
    -3001: "Operacja nieobsługiwana przez urządzenie",
    -3900: "Błąd komunikacji z urządzeniem"
}


def safe_ping(target, count=1, timeout=2):
    try:
        ipaddress.ip_address(str(target))
    except ValueError:
        return {"target": str(target), "ok": False, "error": "Nieprawidłowy adres IP"}
    try:
        result = subprocess.run(
            ["ping", "-n", "-c", str(count), "-W", str(timeout), str(target)],
            capture_output=True, text=True, timeout=count * timeout + 2
        )
        output = (result.stdout or result.stderr or "")[-1200:]
        return {"target": target, "ok": result.returncode == 0, "output": output}
    except Exception as exc:
        return {"target": target, "ok": False, "error": str(exc)}


def local_diagnostics():
    result = {"default_route": None, "gateway_ping": None, "public_ip_pings": [], "dns": None}
    try:
        route = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=3
        )
        result["default_route"] = route.stdout.strip() or route.stderr.strip()
        fields = route.stdout.split()
        if "via" in fields:
            gateway = fields[fields.index("via") + 1]
            result["lxc_gateway"] = gateway
            result["gateway_ping"] = safe_ping(gateway, count=1, timeout=2)
    except Exception as exc:
        result["route_error"] = str(exc)

    targets = CONFIG.get("ping_targets", ["1.1.1.1", "8.8.8.8"])
    result["public_ip_pings"] = [safe_ping(ip, count=1, timeout=2) for ip in targets[:4]]

    try:
        dns = subprocess.run(
            ["getent", "ahostsv4", "example.com"],
            capture_output=True, text=True, timeout=4
        )
        result["dns"] = {
            "ok": dns.returncode == 0,
            "output": (dns.stdout or dns.stderr or "")[:500]
        }
    except Exception as exc:
        result["dns"] = {"ok": False, "error": str(exc)}
    return result


def diagnose_wan_state():
    """Zapisuje surową odpowiedź Omady i testy sieciowe z LXC.

    Nie wyliczamy WAN1 IP/bramy z niepotwierdzonych nazw pól API. Surowa
    odpowiedź zachowuje dane do sprawdzenia zgodnie z wersją kontrolera.
    """
    diag = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "device_mac": CONFIG.get("target_mac"),
        "omada_http_code": None,
        "omada_error": None,
        "omada_device": None,
        "lxc": local_diagnostics()
    }
    mac = str(CONFIG.get("target_mac", "")).replace(":", "-").upper()
    try:
        response, http_code = omada_api_request("GET", f"devices/{mac}")
        diag["omada_http_code"] = http_code
        diag["omada_device"] = response
        if isinstance(response, dict):
            error_code = response.get("errorCode")
            if error_code not in (None, 0):
                diag["omada_error"] = {
                    "code": error_code,
                    "description": OMADA_ERROR_CODES.get(error_code, "Nieznany kod Omada")
                }
    except Exception as exc:
        diag["omada_error"] = str(exc)

    with STATE_LOCK:
        state.setdefault("diagnostic_history", []).insert(0, diag)
        state["diagnostic_history"] = state["diagnostic_history"][:100]
    save_state()
    return diag


def is_cooldown_active():
    last_reboot = state.get("last_reboot")
    if not last_reboot:
        return False
    try:
        last = datetime.strptime(last_reboot, "%Y-%m-%d %H:%M:%S")
        return datetime.now() - last < timedelta(hours=float(CONFIG["cooldown_hours"]))
    except (ValueError, TypeError):
        return False


def perform_reboot(trigger="MANUAL"):
    add_log(f"Przygotowanie restartu ER605 (trigger: {trigger})")
    # Każdy restart ma własny zrzut WAN bezpośrednio przed żądaniem.
    diag = diagnose_wan_state()
    mac = str(CONFIG.get("target_mac", "")).replace(":", "-").upper()
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trigger": trigger,
        "status": "FAIL",
        "http_code": None,
        "wan_diag": diag,
        "details": None
    }
    try:
        response, http_code = omada_api_request("POST", f"devices/{mac}/reboot")
        entry["http_code"] = http_code
        entry["details"] = response
        success = http_code == 200 and isinstance(response, dict) and response.get("errorCode", 0) == 0
        if success:
            entry["status"] = "SUCCESS"
            with STATE_LOCK:
                state["last_reboot"] = entry["timestamp"]
            add_log("Komenda restartu wysłana do kontrolera Omada.")
        else:
            add_log(f"Błąd restartu: HTTP {http_code}: {json.dumps(response, ensure_ascii=False)[:1000]}")
    except Exception as exc:
        entry["details"] = str(exc)
        add_log(f"Wyjątek podczas restartu: {exc}")

    with STATE_LOCK:
        state.setdefault("reboot_history", []).insert(0, entry)
        state["reboot_history"] = state["reboot_history"][:50]
    save_state()
    return entry["status"] == "SUCCESS"


def check_internet():
    for target in CONFIG.get("ping_targets", ["1.1.1.1", "8.8.8.8"]):
        result = safe_ping(target, count=1, timeout=2)
        if result["ok"]:
            latency = None
            output = result.get("output", "")
            for line in output.splitlines():
                if "min/avg/max" in line or "rtt min/avg/max" in line:
                    try:
                        latency = float(line.split("=")[1].strip().split("/")[1])
                    except (IndexError, ValueError):
                        pass
            return True, latency
    return False, None


def watchdog_loop():
    time.sleep(3)
    while True:
        add_log("Sprawdzanie łączności WAN...")
        online, latency = check_internet()
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with STATE_LOCK:
            state["last_check"] = now_text
            state.setdefault("history_24h", [])

        if online:
            with STATE_LOCK:
                state["last_status"] = "ONLINE"
                state["last_latency_ms"] = latency
                state["consecutive_failures"] = 0
                state["current_interval_minutes"] = CONFIG["check_interval_minutes"]
                state["history_24h"].append({"time": now_text, "online": True, "latency": latency})
                state["history_24h"] = state["history_24h"][-500:]
            add_log(f"Status: ONLINE ({latency:.1f} ms)" if latency is not None else "Status: ONLINE")
            save_state()
            time.sleep(max(1, int(CONFIG["check_interval_minutes"])) * 60)
            continue

        with STATE_LOCK:
            state["last_status"] = "OFFLINE"
            state["last_latency_ms"] = None
            state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
            fail_index = state["consecutive_failures"]
            max_retries = max(1, int(CONFIG["max_retries"]))
            state["current_interval_minutes"] = 1
            state["history_24h"].append({"time": now_text, "online": False, "latency": None})
            state["history_24h"] = state["history_24h"][-500:]
        add_log(f"ALERT: Brak WAN! Próba {fail_index}/{max_retries}; następny test za 1 minutę.")
        save_state()

        try:
            diag = diagnose_wan_state()
            # Nie zapisujemy ogromnych odpowiedzi do zwykłego logu; są w historii API.
            lxc = diag.get("lxc", {})
            add_log(
                "Diagnostyka OFFLINE: "
                f"Omada HTTP={diag.get('omada_http_code')}, "
                f"brama LXC={lxc.get('lxc_gateway', 'brak')}, "
                f"ping bramy={((lxc.get('gateway_ping') or {}).get('ok'))}, "
                f"DNS={((lxc.get('dns') or {}).get('ok'))}"
            )
        except Exception as exc:
            add_log(f"Błąd diagnostyki OFFLINE: {exc}")

        if fail_index >= max_retries:
            if is_cooldown_active():
                add_log(f"Restart pominięty: aktywny cooldown {CONFIG['cooldown_hours']}h.")
            else:
                perform_reboot(trigger="AUTO_WATCHDOG_FAIL")

        retry_seconds = max(1, int(CONFIG.get("retry_interval_minutes", 1))) * 60
        time.sleep(retry_seconds)


HTML_PAGE = r"""<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Omada ER605 Watchdog</title>
<style>
:root{--bg:#050808;--card:rgba(10,20,15,.92);--green:#00ff66;--dim:#007a39;--red:#ff4355;--line:#124b2b}
*{box-sizing:border-box}body{margin:0;padding:18px;background:var(--bg);color:var(--green);font:14px Consolas,"Courier New",monospace}
#glow{position:fixed;inset:0;pointer-events:none;z-index:-1;background:radial-gradient(circle at 50% 30%,rgba(0,255,102,.08),transparent 55%)}
main{max-width:1250px;margin:auto}.header,.card{border:1px solid var(--line);background:var(--card);padding:15px;margin-bottom:14px;box-shadow:0 0 12px #002511}
.header{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}h1{font-size:20px;margin:0 0 5px}h2{font-size:14px;color:#8affb5;margin:0 0 12px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.metric{font-size:24px}.muted{color:#83bd98}.online{color:var(--green)}.offline{color:var(--red)}button,select,input,textarea{font:inherit;color:var(--green);background:#00170a;border:1px solid var(--dim);padding:8px}button{cursor:pointer}button:hover{background:#06451e}.danger{border-color:var(--red);color:var(--red)}textarea{width:100%;min-height:70px}input{min-width:250px;flex:1}.row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}pre{white-space:pre-wrap;word-break:break-word;background:#020803;padding:10px;max-height:350px;overflow:auto;border:1px solid var(--line)}table{width:100%;border-collapse:collapse}th,td{border:1px solid var(--line);padding:7px;text-align:left;vertical-align:top}small{color:#9bdab0}.scroll{overflow:auto}.logs{max-height:220px;overflow:auto;line-height:1.55}.chip{font-size:12px}.diag{max-width:600px;word-break:break-word}
</style></head><body><div id="glow"></div><main>
<div class="header"><div><h1>OMADA ER605 WATCHDOG // DIAGNOSTICS</h1><small>WAN checks, API snapshots, and audit history</small></div><div class="row"><button onclick="manualCheck()">TEST WAN NOW</button><button class="danger" onclick="manualReboot()">FORCE REBOOT</button></div></div>
<div class="grid">
<div class="card"><h2>WAN STATUS</h2><div id="status" class="metric">Loading...</div><div id="latency" class="muted"></div></div>
<div class="card"><h2>FAILURES / NEXT INTERVAL</h2><div id="failures" class="metric">--</div><div id="interval" class="muted"></div></div>
<div class="card"><h2>LAST REBOOT / COOLDOWN</h2><div id="reboot">--</div><div id="cooldown" class="muted"></div></div>
</div>
<section class="card"><h2>DIAGNOSTIC HISTORY (each OFFLINE attempt)</h2><div class="scroll"><table><thead><tr><th>Time</th><th>Omada API</th><th>LXC route / gateway</th><th>Public IP / DNS</th><th>Raw device response</th></tr></thead><tbody id="diag"></tbody></table></div></section>
<section class="card"><h2>REBOOT AUDIT</h2><div class="scroll"><table><thead><tr><th>Time</th><th>Trigger</th><th>Result</th><th>WAN snapshot</th></tr></thead><tbody id="audit"></tbody></table></div></section>
<section class="card"><h2>OMADA OPEN API CONSOLE</h2><small>Only use trusted LAN access. POST requests can change device state.</small>
<div class="row"><button class="chip" onclick="preset('GET','devices')">GET devices</button><button class="chip" onclick="preset('GET','devices/'+MAC)">GET ER605</button><button class="chip" onclick="preset('GET','sites')">GET sites</button></div>
<div class="row"><select id="method"><option>GET</option><option>POST</option></select><input id="endpoint" placeholder="e.g. devices or devices/{mac}"><button onclick="runApi()">SEND</button></div>
<textarea id="body" placeholder="Optional JSON body for POST"></textarea><pre id="apiout">Response will appear here.</pre></section>
<section class="card"><h2>SYSTEM LOGS</h2><div id="logs" class="logs"></div></section>
</main><script>
const MAC="";
document.addEventListener('mousemove',e=>{document.getElementById('glow').style.background=`radial-gradient(circle at ${e.clientX}px ${e.clientY}px,rgba(0,255,102,.12),transparent 52%)`});
function esc(x){return String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function preset(m,e){document.getElementById('method').value=m;document.getElementById('endpoint').value=e}
async function refresh(){try{let d=await(await fetch('/api/status')).json();let s=document.getElementById('status');s.textContent=d.status;s.className='metric '+(d.status==='ONLINE'?'online':'offline');document.getElementById('latency').textContent='Latency: '+(d.latency_ms==null?'--':d.latency_ms+' ms');document.getElementById('failures').textContent=d.consecutive_failures+' / '+d.max_retries;document.getElementById('interval').textContent='Next interval: '+d.current_interval_minutes+' min';document.getElementById('reboot').textContent=d.last_reboot||'No reboot recorded';document.getElementById('cooldown').textContent='Cooldown '+d.cooldown_hours+'h: '+(d.cooldown_active?'ACTIVE':'inactive');
document.getElementById('diag').innerHTML=(d.diagnostic_history||[]).slice(0,30).map(x=>{let l=x.lxc||{};return `<tr><td>${esc(x.timestamp)}</td><td>HTTP ${esc(x.omada_http_code)}<br>${esc(JSON.stringify(x.omada_error||''))}</td><td class="diag"><pre>${esc(JSON.stringify({default_route:l.default_route,lxc_gateway:l.lxc_gateway,gateway_ping:l.gateway_ping},null,2))}</pre></td><td class="diag"><pre>${esc(JSON.stringify({public_ip_pings:l.public_ip_pings,dns:l.dns},null,2))}</pre></td><td><details><summary>Show raw</summary><pre>${esc(JSON.stringify(x.omada_device,null,2))}</pre></details></td></tr>`}).join('')||'<tr><td colspan="5">No diagnostics yet.</td></tr>';
document.getElementById('audit').innerHTML=(d.reboot_history||[]).map(x=>`<tr><td>${esc(x.timestamp)}</td><td>${esc(x.trigger)}</td><td>${esc(x.status)} / HTTP ${esc(x.http_code)}</td><td><details><summary>Show snapshot</summary><pre>${esc(JSON.stringify(x.wan_diag,null,2))}</pre></details></td></tr>`).join('')||'<tr><td colspan="4">No reboot records.</td></tr>';
document.getElementById('logs').innerHTML=(d.logs||[]).map(x=>esc(x)).join('<br>');}catch(e){console.error(e)}}
async function manualCheck(){await fetch('/api/manual-check',{method:'POST'});setTimeout(refresh,1000)}
async function manualReboot(){if(confirm('Restart ER605 now?')){await fetch('/api/manual-reboot',{method:'POST'});setTimeout(refresh,1500)}}
async function runApi(){let out=document.getElementById('apiout');out.textContent='Sending...';let bodyText=document.getElementById('body').value.trim();let body=null;try{if(bodyText)body=JSON.parse(bodyText)}catch(e){out.textContent='Invalid JSON body';return}try{let r=await fetch('/api/console',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({method:document.getElementById('method').value,endpoint:document.getElementById('endpoint').value,body})});out.textContent=JSON.stringify(await r.json(),null,2)}catch(e){out.textContent='Request failed: '+e}}
refresh();setInterval(refresh,5000);
</script></body></html>"""


class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[HTTP] " + fmt % args, flush=True)

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/api/status", "/api/state"):
            with STATE_LOCK:
                result = {
                    "status": state.get("last_status"),
                    "last_check": state.get("last_check"),
                    "last_reboot": state.get("last_reboot"),
                    "latency_ms": state.get("last_latency_ms"),
                    "consecutive_failures": state.get("consecutive_failures", 0),
                    "max_retries": CONFIG["max_retries"],
                    "current_interval_minutes": state.get("current_interval_minutes"),
                    "cooldown_active": is_cooldown_active(),
                    "cooldown_hours": CONFIG["cooldown_hours"],
                    "history_24h": state.get("history_24h", [])[-100:],
                    "diagnostic_history": state.get("diagnostic_history", [])[:30],
                    "reboot_history": state.get("reboot_history", [])[:20],
                    "logs": state.get("logs", [])[:100]
                }
            return self.send_json(result)

        body = HTML_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65536:
                return self.send_json({"error": "Request body too large"}, 413)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            return self.send_json({"error": f"Invalid request: {exc}"}, 400)

        if self.path == "/api/manual-check":
            threading.Thread(target=self.manual_check, daemon=True).start()
            return self.send_json({"status": "Manual check started"}, 202)

        if self.path == "/api/manual-reboot":
            # Manual restart intentionally requires an explicit confirmation in UI.
            threading.Thread(target=perform_reboot, args=("MANUAL_WEB_UI",), daemon=True).start()
            return self.send_json({"status": "Reboot request started"}, 202)

        if self.path == "/api/console":
            method = str(payload.get("method", "GET")).upper()
            endpoint = str(payload.get("endpoint", ""))
            body = payload.get("body")
            if method not in ("GET", "POST"):
                return self.send_json({"error": "Only GET and POST are allowed"}, 400)
            try:
                response, http_code = omada_api_request(method, endpoint, body)
                err_code = response.get("errorCode") if isinstance(response, dict) else None
                return self.send_json({
                    "http_code": http_code,
                    "error_code": err_code,
                    "error_description": OMADA_ERROR_CODES.get(err_code, "Unknown Omada API code"),
                    "response": response
                })
            except Exception as exc:
                return self.send_json({"error": str(exc)}, 502)

        return self.send_json({"error": "Not found"}, 404)

    @staticmethod
    def manual_check():
        add_log("Manual WAN check started from UI.")
        online, latency = check_internet()
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with STATE_LOCK:
            state["last_check"] = now_text
            state["last_status"] = "ONLINE" if online else "OFFLINE"
            state["last_latency_ms"] = latency
            if online:
                state["consecutive_failures"] = 0
        if online:
            add_log(f"Manual test: ONLINE ({latency:.1f} ms)" if latency is not None else "Manual test: ONLINE")
        else:
            add_log("Manual test: OFFLINE; collecting diagnostics.")
            try:
                diagnose_wan_state()
            except Exception as exc:
                add_log(f"Manual diagnostics failed: {exc}")
        save_state()


def run_server():
    port = int(CONFIG.get("web_port", 8080))
    server = ThreadingHTTPServer(("0.0.0.0", port), WebHandler)
    add_log(f"Web UI listening on port {port}.")
    server.serve_forever()


if __name__ == "__main__":
    watchdog = threading.Thread(target=watchdog_loop, daemon=True)
    watchdog.start()
    run_server()
