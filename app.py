#!/usr/bin/env python3
"""Omada ER605 Watchdog and local dashboard."""
import copy
import html
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
DEFAULT_CONFIG = {
    "controller_url": "https://192.168.0.4/", "omada_id": "", "site_id": "",
    "client_id": "", "client_secret": "", "er605_mac": "",
    "check_interval_minutes": 30, "retry_interval_minutes": 1,
    "max_retries": 3, "cooldown_hours": 3, "web_port": 8080,
    "post_reboot_wait_minutes": 10,
    "ping_hosts": ["1.1.1.1", "8.8.8.8", "1.0.0.1"],
}
OMADA_ERRORS = {
    0: "Operacja powiodła się", -1000: "Ogólny błąd kontrolera",
    -1001: "Brak wymaganego parametru", -1002: "Nieprawidłowy parametr",
    -1003: "Brak autoryzacji lub wygasły token", -1004: "Brak uprawnień",
    -1005: "Zasób nie istnieje", -1006: "Zasób jest zajęty",
    -1600: "Nieobsługiwana ścieżka", -30001: "Błędny Client ID lub Client Secret",
    -30002: "Nieprawidłowy token autoryzacji", -39000: "Limit zapytań przekroczony",
    -44116: "Omada odrzuciła autoryzację Open API; sprawdź Omada ID, Client ID, Client Secret i aplikację Open API",
}
lock = threading.RLock()
state = {
    "wan_online": True, "latency_ms": 0.0, "consecutive_fails": 0,
    "last_check": None, "next_check": None, "last_reboot": None,
    "cooldown_active": False, "history": [], "reboot_history": [],
    "diagnostics_history": [], "logs": [],
}


def log_message(message):
    entry = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(entry, flush=True)
    with lock:
        state["logs"].append(entry)
        state["logs"] = state["logs"][-100:]


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("config.json musi zawierać obiekt JSON")
        cfg = DEFAULT_CONFIG.copy()
        cfg.update(raw)
        # Wsteczna zgodność z nazwami kluczy używanymi przez instalator.
        for current, legacy in (("controller_url", "omada_url"),
                                ("omada_id", "omadac_id"),
                                ("er605_mac", "target_mac")):
            if not raw.get(current) and raw.get(legacy):
                cfg[current] = raw[legacy]
        # Instalator może zapisywać retry_delay_seconds zamiast minut.
        if "retry_interval_minutes" not in raw and "retry_delay_seconds" in raw:
            seconds = max(1, int(raw["retry_delay_seconds"]))
            cfg["retry_interval_minutes"] = max(1, (seconds + 59) // 60)
        return cfg
    except FileNotFoundError:
        log_message(f"BŁĄD: Brak pliku konfiguracyjnego {CONFIG_PATH}")
    except Exception as exc:
        log_message(f"BŁĄD wczytywania config.json: {exc}")
    return DEFAULT_CONFIG.copy()


def configured_ping_hosts(cfg):
    hosts = cfg.get("ping_hosts", DEFAULT_CONFIG["ping_hosts"])
    if isinstance(hosts, str):
        hosts = [item.strip() for item in hosts.split(",") if item.strip()]
    if not isinstance(hosts, list) or not hosts:
        return DEFAULT_CONFIG["ping_hosts"]
    return [str(host).strip() for host in hosts if str(host).strip()] or DEFAULT_CONFIG["ping_hosts"]


def load_state():
    if not os.path.exists(STATE_PATH):
        return
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        with lock:
            for key in ("reboot_history", "history", "diagnostics_history", "last_reboot"):
                if key in saved:
                    state[key] = saved[key]
    except Exception as exc:
        log_message(f"Błąd odczytu state.json: {exc}")


def save_state():
    with lock:
        data = {"reboot_history": state["reboot_history"][-50:],
                "history": state["history"][-288:],
                "diagnostics_history": state["diagnostics_history"][:30],
                "last_reboot": state["last_reboot"]}
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="state.", suffix=".tmp", dir=BASE_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STATE_PATH)
    except Exception as exc:
        log_message(f"Błąd zapisu state.json: {exc}")
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def format_mac_dashes(mac):
    clean = str(mac).replace(":", "").replace("-", "").strip().upper()
    if len(clean) == 12 and all(c in "0123456789ABCDEF" for c in clean):
        return "-".join(clean[i:i + 2] for i in range(0, 12, 2))
    return str(mac).strip()


def check_cooldown(cfg):
    with lock:
        last = state["last_reboot"]
    if not last:
        return False
    try:
        hours = max(0.0, float(cfg.get("cooldown_hours", 3)))
        return datetime.now() - datetime.fromisoformat(last) < timedelta(hours=hours)
    except (TypeError, ValueError):
        return False


class OmadaClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base_url = str(cfg.get("controller_url", "")).rstrip("/")
        self.omada_id = str(cfg.get("omada_id", ""))
        self.site_id = str(cfg.get("site_id", ""))
        self.client_id = str(cfg.get("client_id", ""))
        self.client_secret = str(cfg.get("client_secret", ""))
        self.token = None
        self.token_expire = 0
        # Umożliwia lokalny kontroler z certyfikatem self-signed.
        self.ssl_ctx = ssl.create_default_context()
        self.ssl_ctx.check_hostname = False
        self.ssl_ctx.verify_mode = ssl.CERT_NONE

    def get_token(self):
        now = time.time()
        if self.token and now < self.token_expire:
            return self.token
        if not all((self.base_url, self.omada_id, self.client_id, self.client_secret)):
            log_message("Błąd Omada: uzupełnij URL, Omada ID, Client ID i Client Secret.")
            return None
        url = f"{self.base_url}/openapi/authorize/token"
        payload = {"omadacId": self.omada_id, "appId": self.client_id,
                   "secret": self.client_secret}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("errorCode", -1) != 0:
                code = data.get("errorCode")
                log_message(f"Błąd tokenu Omada: {data.get('msg')} (kod {code})")
                return None
            result = data.get("result") or {}
            self.token = result.get("accessToken")
            expires_in = int(result.get("expiresIn", 7200))
            self.token_expire = now + max(60, expires_in - 120)
            return self.token
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            log_message(f"HTTP {exc.code} podczas pobierania tokenu Omada: {raw[:500]}")
            return None
        except Exception as exc:
            log_message(f"Wyjątek podczas pobierania tokenu Omada: {exc}")
            return None

    def request(self, method, path, body=None):
        method = str(method).upper()
        if method not in ("GET", "POST"):
            return 400, -1002, {"error": "Dozwolone metody: GET i POST"}
        path = str(path).strip()
        if not path or "\r" in path or "\n" in path:
            return 400, -1002, {"error": "Nieprawidłowa ścieżka API"}
        if not path.startswith("/"):
            path = "/" + path
        parsed = urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc:
            return 400, -1002, {"error": "Podaj ścieżkę API, nie pełny URL"}
        if not path.startswith("/openapi/"):
            full_url = f"{self.base_url}/openapi/v1/{urllib.parse.quote(self.omada_id, safe='')}{path}"
        else:
            full_url = f"{self.base_url}{path}"
        token = self.get_token()
        if not token:
            return 401, -1003, {"error": "Nie udało się pobrać tokenu autoryzacji"}
        headers = {"Content-Type": "application/json", "Authorization": f"AccessToken={token}"}
        data_bytes = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(full_url, data=data_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=12) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    parsed_body = json.loads(raw)
                except json.JSONDecodeError:
                    parsed_body = {"raw": raw}
                return resp.status, parsed_body.get("errorCode", 0), parsed_body
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed_body = json.loads(raw)
            except json.JSONDecodeError:
                parsed_body = {"raw": raw or str(exc)}
            return exc.code, parsed_body.get("errorCode", -1000), parsed_body
        except Exception as exc:
            return 500, -1000, {"error": str(exc)}

    def reboot_device(self, mac):
        mac_dash = urllib.parse.quote(format_mac_dashes(mac), safe="-")
        path = f"/sites/{urllib.parse.quote(self.site_id, safe='')}/devices/{mac_dash}/reboot"
        return self.request("POST", path, body={})

    def get_er605_status(self, mac):
        target_dash = format_mac_dashes(mac).upper()
        target_colon = target_dash.replace("-", ":")
        path = f"/sites/{urllib.parse.quote(self.site_id, safe='')}/devices?page=1&pageSize=50"
        http_code, code, response = self.request("GET", path)
        if http_code != 200 or code != 0:
            return {"http_code": http_code, "error_code": code,
                    "error_description": OMADA_ERRORS.get(code, "Błąd kontrolera"),
                    "raw_response": response}
        result = response.get("result", {})
        devices = result.get("data", []) if isinstance(result, dict) else result
        if not isinstance(devices, list):
            devices = []
        found = next((dev for dev in devices if str(dev.get("mac", "")).upper()
                      in (target_dash, target_colon)), None)
        if not found:
            return {"http_code": http_code, "error_code": 404,
                    "error_description": f"Nie znaleziono {target_dash} na pierwszej stronie listy",
                    "devices_count": len(devices),
                    "available_macs": [d.get("mac") for d in devices if isinstance(d, dict)]}
        return {"http_code": http_code, "error_code": 0,
                "error_description": "Router zidentyfikowany", "device": found,
                "summary": {"name": found.get("name"),
                            "model": found.get("showModel") or found.get("model"),
                            "ip": found.get("ip"), "status": found.get("status"),
                            "status_category": found.get("statusCategory"),
                            "uptime": found.get("uptime") or found.get("uptimeStr"),
                            "wan_ports": found.get("wanPorts") or found.get("portList") or []}}


def ping_target(host, timeout=2):
    try:
        proc = subprocess.run(["ping", "-c", "1", "-W", str(timeout), str(host)],
                              capture_output=True, text=True, timeout=timeout + 1)
        latency = None
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if "time=" in line:
                    try:
                        latency = float(line.split("time=", 1)[1].split()[0].replace("ms", ""))
                    except (ValueError, IndexError):
                        pass
        return proc.returncode == 0, latency, proc.stdout or proc.stderr
    except Exception as exc:
        return False, None, str(exc)


def run_full_network_diagnostics(cfg, trigger="MANUAL"):
    log_message(f"Rozpoczęto diagnostykę WAN (wyzwalacz: {trigger})...")
    gateway = "Nieznana"
    gateway_ping = {"ok": False, "output": "Brak"}
    try:
        proc = subprocess.run(["ip", "route", "show", "default"], capture_output=True,
                              text=True, timeout=3)
        route = proc.stdout.strip()
        parts = route.split()
        if "via" in parts:
            gateway = parts[parts.index("via") + 1]
            ok, latency, output = ping_target(gateway, 1)
            gateway_ping = {"target": gateway, "ok": ok, "latency_ms": latency, "output": output}
    except Exception as exc:
        gateway = f"Błąd: {exc}"
    public_pings = []
    for host in configured_ping_hosts(cfg):
        ok, latency, output = ping_target(host, 2)
        public_pings.append({"target": host, "ok": ok, "latency_ms": latency, "output": output})
    try:
        answers = socket.getaddrinfo("example.com", 80)
        dns = {"ok": True, "output": "\n".join(sorted({a[4][0] for a in answers}))}
    except Exception as exc:
        dns = {"ok": False, "output": f"DNS resolution failed: {exc}"}
    omada_status = OmadaClient(cfg).get_er605_status(cfg.get("er605_mac", ""))
    entry = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "trigger": trigger,
             "api_omada": {"http_code": omada_status.get("http_code"),
                           "code": omada_status.get("error_code"),
                           "description": omada_status.get("error_description")},
             "lxc_route": {"brama_lxc": gateway, "ping_bramy": gateway_ping},
             "public_pings": public_pings, "test_dns": dns, "omada_raw": omada_status}
    with lock:
        state["diagnostics_history"].insert(0, entry)
        state["diagnostics_history"] = state["diagnostics_history"][:30]
    save_state()
    first_ping_ok = public_pings[0]["ok"] if public_pings else False
    log_message(f"Diagnostyka: Omada HTTP={omada_status.get('http_code')}, brama={gateway}, "
                f"ping publiczny={first_ping_ok}, DNS={dns['ok']}")
    return entry


def check_wan_connectivity(cfg):
    successes, latencies = 0, []
    for target in configured_ping_hosts(cfg):
        ok, latency, _ = ping_target(target, 2)
        if ok:
            successes += 1
            if latency is not None:
                latencies.append(latency)
    return successes >= 1, round(sum(latencies) / len(latencies), 1) if latencies else 0.0


def perform_reboot(cfg, trigger="AUTO_WATCHDOG_FAIL"):
    mac = cfg.get("er605_mac", "")
    log_message(f"Inicjalizacja restartu ER605 (MAC: {mac}, trigger: {trigger})...")
    snapshot = run_full_network_diagnostics(cfg, trigger=f"PRE_REBOOT_{trigger}")
    http_code, err_code, response = OmadaClient(cfg).reboot_device(mac)
    success = http_code == 200 and err_code == 0
    now = datetime.now()
    record = {"timestamp": now.strftime("%Y-%m-%d %H:%M:%S"), "trigger": trigger,
              "status": "SUCCESS" if success else "FAILED", "http_code": http_code,
              "omada_code": err_code,
              "omada_msg": response.get("msg", OMADA_ERRORS.get(err_code, "Nieznana odpowiedź")),
              "pre_reboot_snapshot": snapshot}
    with lock:
        # Cooldown uruchamia się po każdej próbie, także nieudanej.
        state["last_reboot"] = now.isoformat()
        state["reboot_history"].insert(0, record)
        state["reboot_history"] = state["reboot_history"][:50]
    save_state()
    log_message("SUKCES: kontroler przyjął polecenie restartu." if success else
                f"BŁĄD restartu: HTTP {http_code}, kod {err_code}, odpowiedź: {response}")
    return success, record


def watchdog_loop():
    log_message("Watchdog WAN wystartował. Pętla monitorowania aktywna.")
    while True:
        cfg = load_config()
        online, latency = check_wan_connectivity(cfg)
        now = datetime.now()
        cooldown = check_cooldown(cfg)
        with lock:
            state["wan_online"] = online
            state["latency_ms"] = latency
            state["last_check"] = now.strftime("%Y-%m-%d %H:%M:%S")
            state["cooldown_active"] = cooldown
            state["history"].append({"t": now.strftime("%H:%M"), "online": online, "lat": latency})
            state["history"] = state["history"][-288:]
            if online:
                state["consecutive_fails"] = 0
            else:
                state["consecutive_fails"] += 1
            fails = state["consecutive_fails"]
        save_state()
        if online:
            delay = max(1, int(cfg.get("check_interval_minutes", 30)))
            log_message(f"Status: ONLINE ({latency} ms). Kolejny test za {delay} min.")
        else:
            max_fails = max(1, int(cfg.get("max_retries", 3)))
            log_message(f"ALERT: Brak WAN! Próba {fails}/{max_fails}.")
            run_full_network_diagnostics(cfg, trigger=f"OFFLINE_ATTEMPT_{fails}")
            if fails >= max_fails:
                if check_cooldown(cfg):
                    delay = max(1, int(cfg.get("retry_interval_minutes", 1)))
                    log_message(f"Cooldown aktywny — pomijam restart. Kolejna próba za {delay} min.")
                else:
                    log_message(f"Osiągnięto limit {max_fails} prób offline. Wysyłam restart ER605.")
                    perform_reboot(cfg, trigger="AUTO_WATCHDOG_FAIL")
                    delay = max(1, int(cfg.get("post_reboot_wait_minutes", 10)))
                    log_message(f"Test po restarcie za {delay} min.")
            else:
                delay = max(1, int(cfg.get("retry_interval_minutes", 1)))
                log_message(f"Kolejna próba {fails + 1}/{max_fails} za {delay} min.")
        next_check = datetime.now() + timedelta(minutes=delay)
        with lock:
            state["next_check"] = next_check.strftime("%Y-%m-%d %H:%M:%S")
        time.sleep(delay * 60)


def esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def generate_svg_chart(history):
    points = history[-288:]
    if len(points) < 2:
        return '<div class="empty">Gromadzenie punktów pomiarowych...</div>'
    width, height, pad = 900, 150, 30
    max_lat = max([float(p.get("lat") or 0) for p in points] + [60.0])
    step = (width - 2 * pad) / (len(points) - 1)
    marks, path = [], []
    for i, point in enumerate(points):
        x = pad + i * step
        try:
            latency = max(0.0, float(point.get("lat") or 0))
        except (TypeError, ValueError):
            latency = 0.0
        online = bool(point.get("online"))
        y = height - pad if not online else height - pad - latency / max_lat * (height - 2 * pad)
        if not online:
            marks.append(f'<line x1="{x:.1f}" y1="{pad}" x2="{x:.1f}" y2="{height-pad}" stroke="#ff3333" stroke-width="2"><title>{esc(point.get("t"))}: OFFLINE</title></line>')
        else:
            marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2" fill="#00ff66"><title>{esc(point.get("t"))}: {latency:.1f} ms</title></circle>')
        path.append(("M" if i == 0 else "L") + f" {x:.1f} {y:.1f}")
    return f'''<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Wykres WAN z 24 godzin">
      <line x1="{pad}" y1="{height-pad}" x2="{width-pad}" y2="{height-pad}" stroke="#1a4d26"/>
      <text x="{pad}" y="18" fill="#00aa44" font-size="10">Maks. opóźnienie: {max_lat:.0f} ms</text>
      <text x="{width-pad}" y="18" fill="#00aa44" font-size="10" text-anchor="end">Ostatnie 24 h</text>
      {''.join(marks)}<path d="{' '.join(path)}" fill="none" stroke="#00ff66" stroke-width="1.5" opacity=".7"/>
      <text x="{pad}" y="{height-6}" fill="#00aa44" font-size="10">{esc(points[0].get('t'))}</text>
      <text x="{width-pad}" y="{height-6}" fill="#00aa44" font-size="10" text-anchor="end">{esc(points[-1].get('t'))}</text>
    </svg>'''


class WebHandler(BaseHTTPRequestHandler):
    server_version = "OmadaWatchdog/1.2"

    def log_message(self, fmt, *args):
        return

    def send_json_response(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_HEAD(self):
        if urllib.parse.urlsplit(self.path).path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Connection", "close")
            self.end_headers()
        else:
            self.send_error(404)
        self.close_connection = True

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/status":
            with lock:
                result = {key: state[key] for key in (
                    "wan_online", "latency_ms", "consecutive_fails", "last_check",
                    "next_check", "last_reboot", "cooldown_active")}
            self.send_json_response(200, result)
        elif path == "/api/state":
            with lock:
                result = copy.deepcopy(state)
            self.send_json_response(200, result)
        elif path in ("/", "/index.html"):
            self.render_dashboard()
        else:
            self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        cfg = load_config()
        if path == "/api/check":
            online, latency = check_wan_connectivity(cfg)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with lock:
                state["wan_online"], state["latency_ms"], state["last_check"] = online, latency, now
            log_message(f"Ręczny test WAN: {'ONLINE' if online else 'OFFLINE'} ({latency} ms)")
            self.send_json_response(200, {"online": online, "latency_ms": latency})
        elif path == "/api/diagnostics":
            self.send_json_response(200, run_full_network_diagnostics(cfg, "MANUAL_WEB_UI"))
        elif path == "/api/reboot":
            success, record = perform_reboot(cfg, "MANUAL_WEB_UI")
            self.send_json_response(200 if success else 502, record)
        elif path == "/api/omada/console":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 65536:
                    raise ValueError("Nieprawidłowy rozmiar żądania")
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                method = str(payload.get("method", "GET")).upper()
                if method not in ("GET", "POST"):
                    raise ValueError("Dozwolone metody to GET i POST")
                http_code, err_code, response = OmadaClient(cfg).request(
                    method, payload.get("path", ""), payload.get("body"))
                self.send_json_response(200, {"http_code": http_code, "error_code": err_code,
                    "error_description": OMADA_ERRORS.get(err_code, "Nieznany kod błędu Omada API"),
                    "response": response})
            except Exception as exc:
                self.send_json_response(400, {"error": str(exc)})
        else:
            self.send_error(404)

    def render_dashboard(self):
        cfg = load_config()
        with lock:
            st = copy.deepcopy(state)
        online = bool(st.get("wan_online"))
        status_text = "ONLINE" if online else "OFFLINE"
        status_color = "#00ff66" if online else "#ff3333"
        cooldown = "Aktywny" if st.get("cooldown_active") else "Nieaktywny"
        diag_cards = []
        for item in st["diagnostics_history"][:10]:
            api = item.get("api_omada", {})
            code = api.get("code", "?")
            api_color = "#00ff66" if code == 0 else "#ffaa00"
            route = item.get("lxc_route", {})
            gw_ok = route.get("ping_bramy", {}).get("ok", False)
            dns_ok = item.get("test_dns", {}).get("ok", False)
            raw = esc(json.dumps(item.get("omada_raw", {}), indent=2, ensure_ascii=False))
            diag_cards.append(f'''<article class="diag-card"><div class="diag-header"><div><b class="green">{esc(item.get('timestamp'))}</b> <span class="badge">{esc(item.get('trigger'))}</span></div>
              <div><span class="badge" style="color:{api_color}">Omada: {esc(code)}</span><span class="badge {'badge-ok' if gw_ok else 'badge-err'}">Brama {'OK' if gw_ok else 'FAIL'}</span><span class="badge {'badge-ok' if dns_ok else 'badge-err'}">DNS {'OK' if dns_ok else 'FAIL'}</span></div></div>
              <div class="diag-summary"><div><b>Brama LAN LXC:</b> {esc(route.get('brama_lxc','brak'))}</div><div><b>Omada API:</b> {esc(api.get('description',''))}</div></div>
              <details><summary>Pokaż surowe dane ER605 / odpowiedź API</summary><pre class="json-box">{raw}</pre></details></article>''')
        if not diag_cards:
            diag_cards.append('<div class="empty">Brak wpisów diagnostycznych.</div>')
        audit_rows = []
        for record in st["reboot_history"][:10]:
            color = "#00ff66" if record.get("status") == "SUCCESS" else "#ff3333"
            snapshot = esc(json.dumps(record.get("pre_reboot_snapshot", {}), indent=2, ensure_ascii=False))
            audit_rows.append(f'''<tr><td>{esc(record.get('timestamp'))}</td><td><code>{esc(record.get('trigger'))}</code></td>
              <td style="color:{color};font-weight:bold">{esc(record.get('status'))} (HTTP {esc(record.get('http_code'))})</td>
              <td><details><summary>Zrzut WAN</summary><pre class="json-box">{snapshot}</pre></details></td></tr>''')
        if not audit_rows:
            audit_rows.append('<tr><td colspan="4" class="empty">Brak zarejestrowanych restartów.</td></tr>')
        logs = esc("\n".join(st["logs"][-35:]))
        chart = generate_svg_chart(st["history"])
        mac = esc(cfg.get("er605_mac", ""))
        mac_dash = esc(format_mac_dashes(cfg.get("er605_mac", "")))
        controller = esc(cfg.get("controller_url", ""))
        fail_count = esc(st.get("consecutive_fails", 0))
        max_retries = esc(cfg.get("max_retries", 3))
        next_check = esc(st.get("next_check") or "nieznany")
        last_reboot = esc(st.get("last_reboot") or "Brak")
        latency = esc(st.get("latency_ms", 0))
        cooldown_hours = esc(cfg.get("cooldown_hours", 3))
        site = json.dumps(str(cfg.get("site_id", "")))
        mac_js = json.dumps(format_mac_dashes(cfg.get("er605_mac", "")))
        html_doc = f'''<!doctype html><html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Omada ER605 Watchdog</title>
<style>
:root{{--bg:#050807;--card:rgba(7,18,11,.88);--border:#13381e;--green:#00ff66;--dim:#00aa44;--red:#ff4444}}*{{box-sizing:border-box}}body{{margin:0;padding:16px;background:#050807;background-image:radial-gradient(circle at var(--mx,50%) var(--my,50%),rgba(0,255,102,.08),transparent 60%);color:#b8e6c4;font:14px Consolas,'Courier New',monospace;min-height:100vh}}.container{{max-width:1100px;margin:auto}}header{{border-bottom:2px solid var(--border);padding-bottom:12px;margin-bottom:20px}}h1{{font-size:1.3rem;color:var(--green);margin:0 0 6px;text-shadow:0 0 8px #00ff6655}}.node-info,.card-title,.card-sub{{color:#77a885;font-size:.85rem}}.actions,.row{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px}}button,select,input{{font:inherit;background:#091a10;color:var(--green);border:1px solid var(--border);padding:9px 12px;border-radius:4px}}button{{cursor:pointer}}button:hover{{background:#11381e;border-color:var(--green)}}.danger{{color:#ff7777;border-color:#551111}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin-bottom:22px}}.card,.diag-card,.console{{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:14px}}.card-title{{text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}}.value{{font-size:1.5rem;font-weight:bold;color:var(--green)}}.section{{font-size:1rem;color:var(--green);border-left:3px solid var(--green);padding-left:8px;margin:26px 0 12px}}.chart{{width:100%;height:auto;display:block;background:#000a05;border:1px solid var(--border);border-radius:4px}}.diag-card{{margin-bottom:10px}}.diag-header{{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;border-bottom:1px solid #142e1b;padding-bottom:7px;margin-bottom:8px}}.diag-summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px;font-size:.85rem;margin:10px 0}}.badge{{display:inline-block;border:1px solid #16803a;border-radius:3px;padding:2px 6px;margin:2px;font-size:.75rem}}.badge-ok{{color:var(--green);border-color:#00aa44}}.badge-err{{color:#ff5555;border-color:#aa2222}}.green{{color:var(--green)}}details{{margin-top:8px}}summary{{cursor:pointer;color:#00cc55}}.json-box{{background:#030805;border:1px solid #0f2b17;padding:10px;border-radius:4px;color:#9fe0b0;font-size:.8rem;max-height:280px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}}table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--border)}}th,td{{padding:9px;text-align:left;border-bottom:1px solid var(--border);overflow-wrap:anywhere}}th{{color:var(--green);background:#091c10}}.table-wrap{{overflow-x:auto}}input[type=text]{{flex:1;min-width:220px}}.console{{margin-bottom:20px}}.empty{{color:#008833;padding:14px;text-align:center}}#logOutput{{width:100%;height:220px}}@media(max-width:650px){{body{{padding:8px}}h1{{font-size:1.05rem}}.actions button{{width:100%}}.row{{flex-direction:column}}input[type=text]{{width:100%;min-width:0}}th,td{{font-size:.75rem;padding:6px}}}}
</style></head><body><main class="container"><header><h1>OMADA ER605 WATCHDOG // SYSTEM CYBERNETYCZNY</h1><div class="node-info">Węzeł: LXC | Cel: ER605 ({mac}) | Kontroler: {controller}</div></header>
<div class="actions"><button onclick="triggerPing()">TEST PING WAN</button><button onclick="triggerDiag()">PEŁNA DIAGNOSTYKA WAN</button><button class="danger" onclick="triggerReboot()">WYMUŚ RESTART ER605</button></div>
<div class="grid"><section class="card"><div class="card-title">Status WAN</div><div class="value" style="color:{status_color}">{status_text}</div><div class="card-sub">Latencja: {latency} ms</div></section>
<section class="card"><div class="card-title">Błędy / kolejny cykl</div><div class="value">{fail_count} / {max_retries}</div><div class="card-sub">Następny test: {next_check}</div></section>
<section class="card"><div class="card-title">Ostatni restart / cooldown</div><div class="value" style="font-size:1rem">{last_reboot}</div><div class="card-sub">Cooldown {cooldown_hours} h: {cooldown}</div></section></div>
<h2 class="section">Historia łączności i latencji (24 godziny)</h2>{chart}
<h2 class="section">Historia diagnostyki (offline i testy ręczne)</h2>{''.join(diag_cards)}
<h2 class="section">Audyt restartów routera</h2><div class="table-wrap"><table><thead><tr><th>Czas</th><th>Wyzwalacz</th><th>Wynik</th><th>Zrzut przed restartem</th></tr></thead><tbody>{''.join(audit_rows)}</tbody></table></div>
<h2 class="section">Konsola Omada Open API</h2><div class="console"><label for="presetCmd">Gotowe komendy:</label><select id="presetCmd" onchange="applyPreset()" style="width:100%;margin:6px 0 12px"><option value="">-- Wybierz komendę --</option><option value="GET|/sites/{{site}}/devices?page=1&amp;pageSize=50">Lista urządzeń witryny</option><option value="GET|/sites/{{site}}/gateways">Bramy witryny (endpoint zależy od wersji API)</option><option value="GET|/sites?page=1&amp;pageSize=50">Lista witryn</option><option value="GET|/sites/{{site}}/events?page=1&amp;pageSize=50">Zdarzenia (jeśli obsługiwane)</option><option value="GET|/sites/{{site}}/alerts?page=1&amp;pageSize=50">Alerty (jeśli obsługiwane)</option><option value="POST|/sites/{{site}}/devices/{{mac}}/reboot">Restart ER605</option></select>
<div class="row"><select id="apiMethod"><option>GET</option><option>POST</option></select><input type="text" id="apiPath" placeholder="/sites/.../devices?page=1&amp;pageSize=50"><button onclick="sendApiRequest()">WYŚLIJ</button></div><label>Odpowiedź kontrolera:</label><pre id="apiConsoleOutput" class="json-box">Oczekiwanie na zapytanie...</pre></div>
<h2 class="section">Logi systemowe</h2><textarea id="logOutput" class="json-box" readonly>{logs}</textarea></main>
<script>
document.addEventListener('mousemove',e=>{{document.body.style.setProperty('--mx',(e.clientX/innerWidth*100)+'%');document.body.style.setProperty('--my',(e.clientY/innerHeight*100)+'%')}});
async function post(path,body){{const r=await fetch(path,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:body?JSON.stringify(body):'{{}}'}});const d=await r.json();if(!r.ok)throw new Error(d.error||d.omada_msg||('HTTP '+r.status));return d}}
async function triggerPing(){{try{{const d=await post('/api/check');alert('WAN: '+(d.online?'ONLINE':'OFFLINE')+' ('+d.latency_ms+' ms)');location.reload()}}catch(e){{alert('Błąd: '+e)}}}}
async function triggerDiag(){{try{{await post('/api/diagnostics');alert('Diagnostyka zakończona.');location.reload()}}catch(e){{alert('Błąd: '+e)}}}}
async function triggerReboot(){{if(!confirm('Czy na pewno wysłać polecenie restartu ER605?'))return;try{{const d=await post('/api/reboot');alert('Wynik: '+d.status+' (HTTP '+d.http_code+')');location.reload()}}catch(e){{alert('Restart nie powiódł się: '+e)}}}}
function applyPreset(){{const val=document.getElementById('presetCmd').value;if(!val)return;const [method,path]=val.split('|');document.getElementById('apiMethod').value=method;document.getElementById('apiPath').value=path.replaceAll('{{site}}',{site}).replaceAll('{{mac}}',{mac_js})}}
async function sendApiRequest(){{const out=document.getElementById('apiConsoleOutput');out.textContent='Wysyłanie...';try{{const d=await post('/api/omada/console',{{method:document.getElementById('apiMethod').value,path:document.getElementById('apiPath').value}});out.textContent='HTTP: '+d.http_code+'\nKOD OMADA: '+d.error_code+' — '+d.error_description+'\n\n'+JSON.stringify(d.response,null,2)}}catch(e){{out.textContent='BŁĄD: '+e}}}}
</script></body></html>'''
        data = html_doc.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True


def run_server(port=8080):
    server = ThreadingHTTPServer(("0.0.0.0", int(port)), WebHandler)
    server.daemon_threads = True
    log_message(f"Panel WWW watchdoga wystartował na porcie {port}.")
    server.serve_forever()


if __name__ == "__main__":
    load_state()
    cfg = load_config()
    threading.Thread(target=watchdog_loop, daemon=True, name="watchdog").start()
    run_server(cfg.get("web_port", 8080))
