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
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        raise ValueError("Endpoint API jest pusty")
    if "\r" in endpoint or "\n" in endpoint:
        raise ValueError("Nieprawidłowy endpoint")

    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        raise ValueError("Dozwolone są tylko ścieżki API, nie pełne adresy URL")

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
    0: "Operacja powiodła się (SUCCESS)",
    -1000: "Nieprawidłowe parametry zapytania (Invalid Parameters)",
    -1001: "Brak wymaganego parametru (np. brak page / pageSize)",
    -1002: "Brak autoryzacji lub wygasły token sesji",
    -1003: "Żądany zasób nie istnieje",
    -1004: "Brak uprawnień do tego zasobu",
    -1600: "Nieobsługiwana ścieżka (zły endpoint lub brak na tym poziomie)",
    -3000: "Urządzenie offline lub nie odpowiada kontrolerowi",
    -3001: "Operacja nieobsługiwana przez ten model urządzenia",
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
    add_log(f"Przygotowanie restartu routera ER605 (wyzwalacz: {trigger})")
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
            add_log("Komenda restartu została wysłana pomyślnie do kontrolera Omada!")
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
        add_log(f"ALERT: Brak WAN! Próba {fail_index}/{max_retries}; ponowienie za 1 minutę.")
        save_state()

        try:
            diag = diagnose_wan_state()
            lxc = diag.get("lxc", {})
            add_log(
                "Diagnostyka awarii: "
                f"Omada HTTP={diag.get('omada_http_code')}, "
                f"Brama LXC={lxc.get('lxc_gateway', 'brak')}, "
                f"Ping bramy={((lxc.get('gateway_ping') or {}).get('ok'))}, "
                f"DNS={((lxc.get('dns') or {}).get('ok'))}"
            )
        except Exception as exc:
            add_log(f"Błąd diagnostyki awarii: {exc}")

        if fail_index >= max_retries:
            if is_cooldown_active():
                add_log(f"Restart pominięty: aktywny cooldown {CONFIG['cooldown_hours']}h.")
            else:
                perform_reboot(trigger="AUTO_WATCHDOG_FAIL")

        retry_seconds = max(1, int(CONFIG.get("retry_interval_minutes", 1))) * 60
        time.sleep(retry_seconds)


HTML_PAGE = r"""<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Omada ER605 Watchdog // Cyber Matrix</title>
  <style>
    :root {
      --bg: #050808;
      --card: rgba(10,20,15,.92);
      --green: #00ff66;
      --dim: #007a39;
      --red: #ff4355;
      --line: #124b2b;
      --text-muted: #83bd98;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 18px;
      background: var(--bg); color: var(--green);
      font: 14px Consolas, "Courier New", monospace;
    }
    #glow {
      position: fixed; inset: 0; pointer-events: none; z-index: -1;
      background: radial-gradient(circle at 50% 30%, rgba(0,255,102,.08), transparent 55%);
    }
    main { max-width: 1250px; margin: auto; }
    .header, .card {
      border: 1px solid var(--line); background: var(--card);
      padding: 15px; margin-bottom: 14px; box-shadow: 0 0 12px #002511;
    }
    .header {
      display: flex; justify-content: space-between; gap: 12px;
      align-items: center; flex-wrap: wrap;
    }
    h1 { font-size: 20px; margin: 0 0 5px; }
    h2 { font-size: 14px; color: #8affb5; margin: 0 0 12px; }
    .grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 12px;
    }
    .metric { font-size: 24px; font-weight: bold; }
    .muted { color: var(--text-muted); }
    .online { color: var(--green); text-shadow: 0 0 8px var(--green); }
    .offline { color: var(--red); text-shadow: 0 0 8px var(--red); }
    button, select, input, textarea {
      font: inherit; color: var(--green); background: #00170a;
      border: 1px solid var(--dim); padding: 8px 12px;
    }
    button { cursor: pointer; transition: all 0.2s; font-weight: bold; }
    button:hover { background: #06451e; box-shadow: 0 0 8px var(--green); }
    .btn-diag { border-color: #00ddff; color: #00ddff; }
    .btn-diag:hover { background: #003344; box-shadow: 0 0 8px #00ddff; }
    .danger { border-color: var(--red); color: var(--red); }
    .danger:hover { background: #550011; box-shadow: 0 0 8px var(--red); }
    textarea { width: 100%; min-height: 70px; margin-top: 8px; }
    input { min-width: 280px; flex: 1; }
    select { min-width: 280px; }
    .row { display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0; align-items: center; }
    pre {
      white-space: pre-wrap; word-break: break-word; background: #020803;
      padding: 10px; max-height: 350px; overflow: auto; border: 1px solid var(--line);
    }
    table { width: 100%; border-collapse: collapse; }
    th, td { border: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: top; }
    th { background: rgba(0, 255, 102, 0.08); }
    small { color: #9bdab0; }
    .scroll { overflow: auto; max-height: 400px; }
    .logs { max-height: 220px; overflow: auto; line-height: 1.55; }
    .diag { max-width: 500px; word-break: break-word; font-size: 12px; }
    .preset-desc {
      background: #001407; border: 1px dashed var(--dim);
      padding: 8px; margin: 6px 0; font-size: 12px; color: #afffd0;
    }
  </style>
</head>
<body>
<div id="glow"></div>
<main>
  <div class="header">
    <div>
      <h1>OMADA ER605 WATCHDOG // SYSTEM CYBERNETYCZNY</h1>
      <small>Węzeł: LXC 107 | Cel: ER605 BRAMA (__MAC__) | Kontroler: Omada SDN</small>
    </div>
    <div class="row">
      <button onclick="manualCheck()">TEST PING WAN</button>
      <button class="btn-diag" onclick="manualDiag()">PEŁNA DIAGNOSTYKA WAN</button>
      <button class="danger" onclick="manualReboot()">WYMUŚ RESTART ER605</button>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>STATUS POŁĄCZENIA WAN</h2>
      <div id="status" class="metric">Wczytywanie...</div>
      <div id="latency" class="muted">Latencja: --</div>
    </div>
    <div class="card">
      <h2>BŁĘDY / KOLEJNY CYKL</h2>
      <div id="failures" class="metric">--</div>
      <div id="interval" class="muted">Kolejne sprawdzenie za: --</div>
    </div>
    <div class="card">
      <h2>OSTATNI RESTART / COOLDOWN</h2>
      <div id="reboot" style="font-size: 16px; font-weight: bold;">--</div>
      <div id="cooldown" class="muted">Cooldown: --</div>
    </div>
  </div>

  <section class="card">
    <h2>HISTORIA DIAGNOSTYKI (KAŻDA PRÓBA OFFLINE & TEST MANUALNY)</h2>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>Czas</th>
            <th>API Omada</th>
            <th>Trasa LXC / Brama domyślna</th>
            <th>Publiczne IP / Test DNS</th>
            <th>Surowa odpowiedź Omady</th>
          </tr>
        </thead>
        <tbody id="diag"></tbody>
      </table>
    </div>
  </section>

  <section class="card">
    <h2>AUDYT RESTARTÓW ROUTERA</h2>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>Czas</th>
            <th>Wyzwalacz</th>
            <th>Wynik operacji</th>
            <th>Zrzut stanu przed restartem</th>
          </tr>
        </thead>
        <tbody id="audit"></tbody>
      </table>
    </div>
  </section>

  <section class="card">
    <h2>KONSOLA OMADA OPEN API & DIAGNOSTYKA BŁĘDÓW</h2>
    <small>Wybierz gotową operację lub wprowadź własny endpoint. Używaj wyłącznie w zaufanej sieci lokalnej LAN.</small>
    
    <div class="row" style="margin-top: 10px;">
      <label for="preset-select"><strong>Gotowe komendy:</strong></label>
      <select id="preset-select" onchange="applyPreset()">
        <option value="">-- Wybierz gotową komendę do Omady --</option>
        <option value="er605_state">🔍 Stan i porty routera ER605 (uptime, link, konfiguracja)</option>
        <option value="devices_list">📋 Lista wszystkich urządzeń w witrynie (switche, routery, AP)</option>
        <option value="sites_list">🌐 Lista witryn w kontrolerze (identyfikatory i nazwy)</option>
        <option value="events_global">⚠️ Zdarzenia systemowe kontrolera (Events)</option>
        <option value="logs_global">📑 Dziennik operacji kontrolera (Logs)</option>
        <option value="alerts_site">🚨 Alerty w bieżącej witrynie (Alerts)</option>
        <option value="reboot_er605">🔄 Wyślij polecenie restartu routera ER605</option>
      </select>
    </div>

    <div id="preset-desc" class="preset-desc" style="display:none;"></div>

    <div class="row">
      <select id="method" style="min-width: 100px;">
        <option>GET</option>
        <option>POST</option>
      </select>
      <input id="endpoint" placeholder="np. devices?page=1&pageSize=10 lub /openapi/v1/..." />
      <button onclick="runApi()">WYŚLIJ DO KONTROLERA</button>
    </div>
    <textarea id="body" placeholder='Opcjonalne Body w formacie JSON dla zapytań POST (np. {"key": "value"})'></textarea>
    <pre id="apiout">// Odpowiedź z kontrolera Omada pojawi się tutaj...</pre>
  </section>

  <section class="card">
    <h2>LOGI SYSTEMOWE WATCHDOGA</h2>
    <div id="logs" class="logs">Ładowanie logów...</div>
  </section>
</main>

<script>
const MAC = "__MAC__";
const OMADAC_ID = "__OMADAC_ID__";

const PRESETS = {
  "er605_state": {
    method: "GET",
    endpoint: "devices/" + MAC,
    body: "",
    desc: "Pobiera szczegółowe dane o routerze ER605: czas pracy (uptime), stan portów LAN/WAN, adresy IP i status linku."
  },
  "devices_list": {
    method: "GET",
    endpoint: "devices?page=1&pageSize=10",
    body: "",
    desc: "Zwraca listę wszystkich zaadoptowanych urządzeń w bieżącej witrynie (router, przełączniki, access pointy)."
  },
  "sites_list": {
    method: "GET",
    endpoint: "/openapi/v1/" + OMADAC_ID + "/sites?page=1&pageSize=10",
    body: "",
    desc: "Pobiera listę witryn skonfigurowanych w kontrolerze Omada (np. witryna 'dom' wraz z jej Site ID)."
  },
  "events_global": {
    method: "GET",
    endpoint: "/openapi/v1/" + OMADAC_ID + "/events?page=1&pageSize=20",
    body: "",
    desc: "Wyświetla ostatnie 20 zdarzeń sieciowych zarejestrowanych przez kontroler (rozłączenia linku, zmiany IP)."
  },
  "logs_global": {
    method: "GET",
    endpoint: "/openapi/v1/" + OMADAC_ID + "/logs?page=1&pageSize=20",
    body: "",
    desc: "Dziennik operacji i logów administracyjnych kontrolera Omada."
  },
  "alerts_site": {
    method: "GET",
    endpoint: "alerts?page=1&pageSize=20",
    body: "",
    desc: "Pobiera aktywne alerty i ostrzeżenia o awariach w bieżącej witrynie."
  },
  "reboot_er605": {
    method: "POST",
    endpoint: "devices/" + MAC + "/reboot",
    body: "",
    desc: "Wysyła oficjalną komendę ponownego uruchomienia (reboot) do routera ER605 przez Open API."
  }
};

function applyPreset() {
  const sel = document.getElementById("preset-select").value;
  const descBox = document.getElementById("preset-desc");
  if (!sel || !PRESETS[sel]) {
    descBox.style.display = "none";
    return;
  }
  const p = PRESETS[sel];
  document.getElementById("method").value = p.method;
  document.getElementById("endpoint").value = p.endpoint;
  document.getElementById("body").value = p.body;
  descBox.textContent = p.desc;
  descBox.style.display = "block";
}

document.addEventListener('mousemove', e => {
  document.getElementById('glow').style.background = `radial-gradient(circle at ${e.clientX}px ${e.clientY}px, rgba(0,255,102,.12), transparent 52%)`;
});

function esc(x) {
  return String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function refresh() {
  try {
    let d = await (await fetch('/api/status')).json();
    let s = document.getElementById('status');
    s.textContent = d.status;
    s.className = 'metric ' + (d.status === 'ONLINE' ? 'online' : 'offline');
    document.getElementById('latency').textContent = 'Latencja: ' + (d.latency_ms == null ? '--' : d.latency_ms.toFixed(1) + ' ms');
    document.getElementById('failures').textContent = d.consecutive_failures + ' / ' + d.max_retries;
    document.getElementById('interval').textContent = 'Kolejne sprawdzenie za: ' + d.current_interval_minutes + ' min';
    document.getElementById('reboot').textContent = d.last_reboot || 'Brak zarejestrowanych restartów';
    document.getElementById('cooldown').textContent = 'Cooldown ' + d.cooldown_hours + 'h: ' + (d.cooldown_active ? 'AKTYWNY' : 'Nieaktywny');

    // Historia diagnostyki
    document.getElementById('diag').innerHTML = (d.diagnostic_history || []).slice(0, 30).map(x => {
      let l = x.lxc || {};
      let errText = x.omada_error ? JSON.stringify(x.omada_error) : 'OK';
      return `<tr>
        <td>${esc(x.timestamp)}</td>
        <td>HTTP ${esc(x.omada_http_code)}<br><small>${esc(errText)}</small></td>
        <td class="diag"><pre>${esc(JSON.stringify({brama_lxc: l.lxc_gateway, ping_bramy: l.gateway_ping}, null, 2))}</pre></td>
        <td class="diag"><pre>${esc(JSON.stringify({publiczne_pingi: l.public_ip_pings, test_dns: l.dns}, null, 2))}</pre></td>
        <td><details><summary>Pokaż dane ER605</summary><pre>${esc(JSON.stringify(x.omada_device, null, 2))}</pre></details></td>
      </tr>`;
    }).join('') || '<tr><td colspan="5">Brak historii diagnostyki.</td></tr>';

    // Audyt restartów
    document.getElementById('audit').innerHTML = (d.reboot_history || []).map(x => `<tr>
      <td>${esc(x.timestamp)}</td>
      <td>${esc(x.trigger)}</td>
      <td style="color:${x.status === 'SUCCESS' ? 'var(--green)' : 'var(--red)'}; font-weight:bold;">${esc(x.status)} (HTTP ${esc(x.http_code)})</td>
      <td><details><summary>Pokaż zrzut WAN</summary><pre>${esc(JSON.stringify(x.wan_diag, null, 2))}</pre></details></td>
    </tr>`).join('') || '<tr><td colspan="4">Brak zarejestrowanych restartów.</td></tr>';

    // Logi
    document.getElementById('logs').innerHTML = (d.logs || []).map(x => esc(x)).join('<br>');
  } catch (e) {
    console.error(e);
  }
}

async function manualCheck() {
  await fetch('/api/manual-check', { method: 'POST' });
  setTimeout(refresh, 1000);
}

async function manualDiag() {
  await fetch('/api/manual-diag', { method: 'POST' });
  setTimeout(refresh, 1000);
}

async function manualReboot() {
  if (confirm('Czy na pewno chcesz wymusić natychmiastowy restart routera ER605 przez Open API?')) {
    await fetch('/api/manual-reboot', { method: 'POST' });
    setTimeout(refresh, 1500);
  }
}

async function runApi() {
  let out = document.getElementById('apiout');
  out.textContent = '// Wysyłanie zapytania do Omada Controller...';
  let bodyText = document.getElementById('body').value.trim();
  let body = null;
  try {
    if (bodyText) body = JSON.parse(bodyText);
  } catch (e) {
    out.textContent = '// BŁĄD: Niepoprawny format JSON w polu Body!';
    return;
  }
  try {
    let r = await fetch('/api/console', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        method: document.getElementById('method').value,
        endpoint: document.getElementById('endpoint').value,
        body
      })
    });
    let data = await r.json();
    let text = `HTTP STATUS: ${data.http_code}\n`;
    if (data.error_code !== undefined && data.error_code !== null) {
      text += `KOD OMADA: ${data.error_code} -> ${data.error_description}\n`;
    }
    text += "\n" + JSON.stringify(data.response || data, null, 2);
    out.textContent = text;
  } catch (e) {
    out.textContent = '// Błąd zapytania: ' + e;
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


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

        # Wstrzyknięcie aktualnego adresu MAC i Omada ID do HTML-a
        mac_formatted = str(CONFIG.get("target_mac", "")).replace(":", "-").upper()
        page = HTML_PAGE.replace("__MAC__", mac_formatted).replace("__OMADAC_ID__", str(CONFIG.get("omadac_id", "")))
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65536:
                return self.send_json({"error": "Zbyt duże ciało zapytania"}, 413)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            return self.send_json({"error": f"Błędne zapytanie: {exc}"}, 400)

        if self.path == "/api/manual-check":
            threading.Thread(target=self.manual_check, daemon=True).start()
            return self.send_json({"status": "Test pingu rozpoczęty"}, 202)

        if self.path == "/api/manual-diag":
            threading.Thread(target=self.manual_diag, daemon=True).start()
            return self.send_json({"status": "Pełna diagnostyka rozpoczęta"}, 202)

        if self.path == "/api/manual-reboot":
            threading.Thread(target=perform_reboot, args=("MANUAL_WEB_UI",), daemon=True).start()
            return self.send_json({"status": "Restart zainicjalizowany"}, 202)

        if self.path == "/api/console":
            method = str(payload.get("method", "GET")).upper()
            endpoint = str(payload.get("endpoint", ""))
            body = payload.get("body")
            if method not in ("GET", "POST"):
                return self.send_json({"error": "Dozwolone są wyłącznie metody GET oraz POST"}, 400)
            try:
                response, http_code = omada_api_request(method, endpoint, body)
                err_code = response.get("errorCode") if isinstance(response, dict) else None
                return self.send_json({
                    "http_code": http_code,
                    "error_code": err_code,
                    "error_description": OMADA_ERROR_CODES.get(err_code, "Nieznany kod błędu Omada"),
                    "response": response
                })
            except Exception as exc:
                return self.send_json({"error": str(exc)}, 502)

        return self.send_json({"error": "Nie znaleziono endpointu"}, 404)

    @staticmethod
    def manual_check():
        add_log("Ręczny test pingu WAN z panelu WWW...")
        online, latency = check_internet()
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with STATE_LOCK:
            state["last_check"] = now_text
            state["last_status"] = "ONLINE" if online else "OFFLINE"
            state["last_latency_ms"] = latency
            if online:
                state["consecutive_failures"] = 0
        if online:
            add_log(f"Test ręczny: ONLINE ({latency:.1f} ms)" if latency is not None else "Test ręczny: ONLINE")
        else:
            add_log("Test ręczny: OFFLINE!")
        save_state()

    @staticmethod
    def manual_diag():
        add_log("Ręczne uruchomienie pełnej diagnostyki sieci i Omada API...")
        try:
            diag = diagnose_wan_state()
            lxc = diag.get("lxc", {})
            add_log(
                "Diagnostyka ręczna zakończona: "
                f"Omada HTTP={diag.get('omada_http_code')}, "
                f"Brama={lxc.get('lxc_gateway')}, "
                f"Ping={((lxc.get('gateway_ping') or {}).get('ok'))}, "
                f"DNS={((lxc.get('dns') or {}).get('ok'))}"
            )
        except Exception as exc:
            add_log(f"Błąd diagnostyki ręcznej: {exc}")
        save_state()


def run_server():
    port = int(CONFIG.get("web_port", 8080))
    server = ThreadingHTTPServer(("0.0.0.0", port), WebHandler)
    add_log(f"Panel WWW watchdoga wystartował na porcie {port}.")
    server.serve_forever()


if __name__ == "__main__":
    watchdog = threading.Thread(target=watchdog_loop, daemon=True)
    watchdog.start()
    run_server()
