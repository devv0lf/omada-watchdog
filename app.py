#!/usr/bin/env python3
"""
Omada ER605 Watchdog & Cybernetic Dashboard
Współpracuje z TP-Link Omada SDN Controller Open API v1
"""

import os
import sys
import json
import time
import socket
import ssl
import subprocess
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.error
import urllib.parse

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# Domyślne wartości konfiguracji
DEFAULT_CONFIG = {
    "controller_url": "https://192.168.0.4/",
    "omada_id": "",
    "site_id": "",
    "client_id": "",
    "client_secret": "",
    "er605_mac": "",
    "check_interval_minutes": 30,
    "retry_interval_minutes": 1,
    "max_retries": 3,
    "cooldown_hours": 3,
    "web_port": 8080
}

OMADA_ERRORS = {
    0: "Operacja powiodła się (Sukces)",
    -1000: "Ogólny błąd systemu kontrolera",
    -1001: "Brak wymaganego parametru w zapytaniu (np. page/pageSize)",
    -1002: "Nieprawidłowy parametr w zapytaniu",
    -1003: "Brak autoryzacji lub wygasły token sesji",
    -1004: "Brak uprawnień do wykonania operacji",
    -1005: "Zasób nie istnieje",
    -1006: "Zasób jest zablokowany lub zajęty",
    -1600: "Nieobsługiwana ścieżka (zły endpoint lub brak na tym poziomie)",
    -30001: "Błędny Client ID lub Client Secret",
    -30002: "Nieprawidłowy format tokenu autoryzacji",
    -39000: "Przekroczono limit zapytań (Rate limit)"
}

lock = threading.Lock()
state = {
    "wan_online": True,
    "latency_ms": 0.0,
    "consecutive_fails": 0,
    "last_check": None,
    "next_check": None,
    "last_reboot": None,
    "cooldown_active": False,
    "history": [],            # Historia pomiarów 24h
    "reboot_history": [],     # Audyt restartów
    "diagnostics_history": [],# Zrzuty pełnej diagnostyki
    "logs": []                # Dziennik tekstowy
}


def log_message(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] {msg}"
    print(entry, flush=True)
    with lock:
        state["logs"].append(entry)
        if len(state["logs"]) > 100:
            state["logs"] = state["logs"][-100:]


def load_config():
    if not os.path.exists(CONFIG_PATH):
        log_message(f"BŁĄD: Brak pliku konfiguracyjnego {CONFIG_PATH}")
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            merged = DEFAULT_CONFIG.copy()
            merged.update(cfg)
            return merged
    except Exception as e:
        log_message(f"BŁĄD wczytywania config.json: {e}")
        return DEFAULT_CONFIG.copy()


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
                with lock:
                    state["reboot_history"] = saved.get("reboot_history", [])
                    state["history"] = saved.get("history", [])
                    state["diagnostics_history"] = saved.get("diagnostics_history", [])
                    state["last_reboot"] = saved.get("last_reboot")
        except Exception as e:
            log_message(f"Błąd odczytu state.json: {e}")


def save_state():
    with lock:
        data = {
            "reboot_history": state["reboot_history"][-50:],
            "history": state["history"][-288:],
            "diagnostics_history": state["diagnostics_history"][-30:],
            "last_reboot": state["last_reboot"]
        }
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log_message(f"Błąd zapisu state.json: {e}")


def format_mac_dashes(mac_str):
    clean = mac_str.replace(":", "").replace("-", "").strip().upper()
    if len(clean) == 12:
        return "-".join(clean[i:i+2] for i in range(0, 12, 2))
    return mac_str.strip()


def check_cooldown(cfg):
    with lock:
        last = state["last_reboot"]
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
        hours = cfg.get("cooldown_hours", 3)
        return (datetime.now() - last_dt) < timedelta(hours=hours)
    except Exception:
        return False


# ==============================================================================
# OMADA OPEN API CLIENT
# ==============================================================================

class OmadaClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base_url = cfg.get("controller_url", "").rstrip("/")
        self.omada_id = cfg.get("omada_id", "")
        self.site_id = cfg.get("site_id", "")
        self.client_id = cfg.get("client_id", "")
        self.client_secret = cfg.get("client_secret", "")
        self.token = None
        self.token_expire = 0
        self.ssl_ctx = ssl.create_default_context()
        self.ssl_ctx.check_hostname = False
        self.ssl_ctx.verify_mode = ssl.CERT_NONE

    def get_token(self):
        now = time.time()
        if self.token and now < self.token_expire:
            return self.token

        url = f"{self.base_url}/openapi/authorize/token"
        payload = {
            "omadacId": self.omada_id,
            "appId": self.client_id,
            "secret": self.client_secret
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )

        try:
            with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("errorCode", -1) == 0:
                    result = data.get("result", {})
                    self.token = result.get("accessToken")
                    expires_in = result.get("expiresIn", 7200)
                    self.token_expire = now + max(60, expires_in - 120)
                    return self.token
                else:
                    log_message(f"Błąd uzyskiwania tokenu Omada: {data.get('msg')} (kod {data.get('errorCode')})")
                    return None
        except Exception as e:
            log_message(f"Wyjątek podczas pobierania tokenu Omada: {e}")
            return None

    def request(self, method, path, body=None):
        token = self.get_token()
        if not token:
            return 401, -1003, {"error": "Brak poprawnego tokenu autoryzacji"}

        path_clean = path.strip()
        if not path_clean.startswith("/"):
            path_clean = "/" + path_clean

        # Automatyczne uzupełnienie prefiksu OpenAPI, jeśli podano relatywną ścieżkę
        if not path_clean.startswith("/openapi/"):
            full_url = f"{self.base_url}/openapi/v1/{self.omada_id}{path_clean}"
        else:
            full_url = f"{self.base_url}{path_clean}"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"AccessToken={token}"
        }
        data_bytes = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(full_url, data=data_bytes, headers=headers, method=method.upper())

        try:
            with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=12) as resp:
                raw = resp.read().decode("utf-8")
                parsed = json.loads(raw)
                code = parsed.get("errorCode", 0)
                return resp.status, code, parsed
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode("utf-8")
                parsed = json.loads(raw)
                return e.code, parsed.get("errorCode", -1000), parsed
            except Exception:
                return e.code, -1000, {"raw": str(e)}
        except Exception as e:
            return 500, -1000, {"error": str(e)}

    def reboot_device(self, mac_str):
        mac_dash = format_mac_dashes(mac_str)
        path = f"/sites/{self.site_id}/devices/{mac_dash}/reboot"
        return self.request("POST", path, body={})

    def get_er605_status(self, mac_str):
        target_dash = format_mac_dashes(mac_str)
        target_colon = target_dash.replace("-", ":")
        path = f"/sites/{self.site_id}/devices?page=1&pageSize=50"
        status, code, res = self.request("GET", path)

        if status != 200 or code != 0:
            return {
                "http_code": status,
                "error_code": code,
                "error_description": OMADA_ERRORS.get(code, "Błąd kontrolera"),
                "raw_response": res
            }

        devices_list = []
        result_obj = res.get("result", {})
        if isinstance(result_obj, dict):
            devices_list = result_obj.get("data", [])
        elif isinstance(result_obj, list):
            devices_list = result_obj

        found = None
        for dev in devices_list:
            dev_mac = str(dev.get("mac", "")).upper()
            if dev_mac in (target_dash, target_colon):
                found = dev
                break

        if not found:
            return {
                "http_code": status,
                "error_code": 404,
                "error_description": f"Nie znaleziono routera o MAC {target_dash} na liście urządzeń witryny",
                "devices_count": len(devices_list),
                "available_macs": [d.get("mac") for d in devices_list]
            }

        return {
            "http_code": status,
            "error_code": 0,
            "error_description": "Sukces (router zidentyfikowany)",
            "device": found,
            "summary": {
                "name": found.get("name"),
                "model": found.get("showModel") or found.get("model"),
                "ip": found.get("ip"),
                "status": found.get("status"),
                "status_category": found.get("statusCategory"),
                "uptime": found.get("uptime") or found.get("uptimeStr"),
                "wan_ports": found.get("wanPorts") or found.get("portList") or []
            }
        }


# ==============================================================================
# DIAGNOSTYKA SIECIOWA
# ==============================================================================

def ping_target(host, timeout=2):
    try:
        cmd = ["ping", "-c", "1", "-W", str(timeout), host]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout + 1)
        ok = (proc.returncode == 0)
        latency = None
        if ok:
            for line in proc.stdout.splitlines():
                if "time=" in line:
                    parts = line.split("time=")
                    if len(parts) > 1:
                        latency = float(parts[1].split()[0].replace("ms", ""))
        return ok, latency, proc.stdout
    except Exception as e:
        return False, None, str(e)


def run_full_network_diagnostics(cfg, trigger="MANUAL"):
    log_message(f"Rozpoczęto pełną diagnostykę WAN (Wyzwalacz: {trigger})...")

    # 1. Trasa domyślna LXC
    brama_lxc = "Nieznana"
    ping_bramy_res = {"ok": False, "output": "Brak"}
    try:
        proc = subprocess.run(["ip", "route", "show", "default"], stdout=subprocess.PIPE, text=True, timeout=3)
        out = proc.stdout.strip()
        if "via" in out:
            parts = out.split()
            idx = parts.index("via")
            if idx + 1 < len(parts):
                brama_lxc = parts[idx + 1]
                ok, lat, rout = ping_target(brama_lxc, timeout=1)
                ping_bramy_res = {"target": brama_lxc, "ok": ok, "latency_ms": lat, "output": rout}
    except Exception as e:
        brama_lxc = f"Błąd: {e}"

    # 2. Publiczne IP (Cloudflare, Google)
    pub_results = []
    for ip in ["1.1.1.1", "8.8.8.8"]:
        ok, lat, rout = ping_target(ip, timeout=2)
        pub_results.append({"target": ip, "ok": ok, "latency_ms": lat, "output": rout})

    # 3. Test DNS
    dns_res = {"ok": False, "output": ""}
    try:
        addr = socket.getaddrinfo("example.com", 80)
        dns_lines = [f"{a[4][0]:<15} {a[1].name if hasattr(a[1], 'name') else a[1]} example.com" for a in addr]
        dns_res = {"ok": True, "output": "\n".join(dns_lines)}
    except Exception as e:
        dns_res = {"ok": False, "output": f"DNS resolution failed: {e}"}

    # 4. Stan ER605 przez Omada API (lista urządzeń z filtrem po MAC)
    omada_client = OmadaClient(cfg)
    omada_status = omada_client.get_er605_status(cfg.get("er605_mac", ""))

    diag_entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trigger": trigger,
        "api_omada": {
            "http_code": omada_status.get("http_code"),
            "code": omada_status.get("error_code"),
            "description": omada_status.get("error_description")
        },
        "lxc_route": {
            "brama_lxc": brama_lxc,
            "ping_bramy": ping_bramy_res
        },
        "public_pings": pub_results,
        "test_dns": dns_res,
        "omada_raw": omada_status
    }

    with lock:
        state["diagnostics_history"].insert(0, diag_entry)
        if len(state["diagnostics_history"]) > 30:
            state["diagnostics_history"] = state["diagnostics_history"][:30]

    save_state()
    log_message(f"Diagnostyka zakończona: Omada HTTP={omada_status.get('http_code')}, Brama={brama_lxc}, PingPub={pub_results[0]['ok']}, DNS={dns_res['ok']}")
    return diag_entry


def check_wan_connectivity():
    targets = ["1.1.1.1", "8.8.8.8", "1.0.0.1"]
    latencies = []
    successes = 0

    for t in targets:
        ok, lat, _ = ping_target(t, timeout=2)
        if ok and lat is not None:
            successes += 1
            latencies.append(lat)

    is_online = (successes >= 1)
    avg_lat = round(sum(latencies) / len(latencies), 1) if latencies else 0.0
    return is_online, avg_lat


def perform_reboot(cfg, trigger="AUTO_WATCHDOG_FAIL"):
    mac = cfg.get("er605_mac", "")
    log_message(f"Inicjalizacja procedury restartu ER605 (MAC: {mac}, Trigger: {trigger})...")

    # Pobieramy zrzut stanu przed restartem
    diag_before = run_full_network_diagnostics(cfg, trigger=f"PRE_REBOOT_{trigger}")

    client = OmadaClient(cfg)
    http_code, err_code, res = client.reboot_device(mac)
    success = (http_code == 200 and err_code == 0)

    now_iso = datetime.now().isoformat()
    reboot_record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trigger": trigger,
        "status": "SUCCESS" if success else "FAILED",
        "http_code": http_code,
        "omada_code": err_code,
        "omada_msg": res.get("msg", OMADA_ERRORS.get(err_code, "Nieznana odpowiedź")),
        "pre_reboot_snapshot": diag_before
    }

    with lock:
        state["last_reboot"] = now_iso
        state["reboot_history"].insert(0, reboot_record)
        if len(state["reboot_history"]) > 50:
            state["reboot_history"] = state["reboot_history"][:50]

    save_state()

    if success:
        log_message(f"SUKCES: Komenda restartu routera ER605 została przyjęta przez kontroler Omada.")
    else:
        log_message(f"BŁĄD: Kontroler odrzucił polecenie restartu: HTTP {http_code}, kod {err_code}, treść: {res}")
    return success, reboot_record


# ==============================================================================
# GŁÓWNA PĘTLA WATCHDOGA (ADAPTACYJNE INTERWAŁY)
# ==============================================================================

def watchdog_loop():
    log_message("Watchdog WAN wystartował. Pętla monitorowania aktywna.")
    while True:
        cfg = load_config()
        is_online, latency = check_wan_connectivity()
        now_dt = datetime.now()

        with lock:
            state["wan_online"] = is_online
            state["latency_ms"] = latency
            state["last_check"] = now_dt.strftime("%Y-%m-%d %H:%M:%S")
            state["cooldown_active"] = check_cooldown(cfg)
            state["history"].append({
                "t": now_dt.strftime("%H:%M"),
                "online": is_online,
                "lat": latency
            })
            if len(state["history"]) > 288:
                state["history"] = state["history"][-288:]

        save_state()

        if is_online:
            with lock:
                state["consecutive_fails"] = 0
            sleep_mins = cfg.get("check_interval_minutes", 30)
            next_dt = now_dt + timedelta(minutes=sleep_mins)
            with lock:
                state["next_check"] = next_dt.strftime("%Y-%m-%d %H:%M:%S")
            log_message(f"Status: ONLINE ({latency} ms). Kolejne sprawdzenie za {sleep_mins} min.")
            time.sleep(sleep_mins * 60)

        else:
            with lock:
                state["consecutive_fails"] += 1
                fails = state["consecutive_fails"]

            max_fails = cfg.get("max_retries", 3)
            log_message(f"ALERT: Brak WAN! Próba {fails}/{max_fails}.")

            # Zbieramy pełną diagnostykę po każdej nieudanej próbie
            run_full_network_diagnostics(cfg, trigger=f"OFFLINE_ATTEMPT_{fails}")

            if fails >= max_fails:
                if check_cooldown(cfg):
                    sleep_mins = cfg.get("retry_interval_minutes", 10)
                    next_dt = now_dt + timedelta(minutes=sleep_mins)
                    with lock:
                        state["next_check"] = next_dt.strftime("%Y-%m-%d %H:%M:%S")
                    log_message(f"Cooldown aktywny — pomijanie procedury restartu. Ponowna próba za {sleep_mins} min.")
                    time.sleep(sleep_mins * 60)
                else:
                    log_message(f"Osiągnięto limit {max_fails} prób offline. Wyzwalanie restartu ER605...")
                    perform_reboot(cfg, trigger="AUTO_WATCHDOG_FAIL")
                    # Po restarcie dajemy routerowi 10 minut na synchronizację WAN
                    post_reboot_wait = 10
                    next_dt = datetime.now() + timedelta(minutes=post_reboot_wait)
                    with lock:
                        state["next_check"] = next_dt.strftime("%Y-%m-%d %H:%M:%S")
                    log_message(f"Oczekiwanie {post_reboot_wait} min na rozruch routera i modemu przed kolejnym testem...")
                    time.sleep(post_reboot_wait * 60)
            else:
                # Ponowienie próby co 1 minutę
                sleep_mins = cfg.get("retry_interval_minutes", 1)
                next_dt = now_dt + timedelta(minutes=sleep_mins)
                with lock:
                    state["next_check"] = next_dt.strftime("%Y-%m-%d %H:%M:%S")
                log_message(f"Kolejne natychmiastowe sprawdzenie próby {fails+1}/{max_fails} za {sleep_mins} min.")
                time.sleep(sleep_mins * 60)


# ==============================================================================
# INTERFEJS WEBOWY & API REST
# ==============================================================================

def generate_svg_chart(history):
    if not history:
        return '<div style="color:#008833;padding:20px;text-align:center;">Brak wystarczającej liczby danych do wykresu 24h.</div>'

    width = 900
    height = 140
    padding = 30
    pts = history[-60:]
    n = len(pts)
    if n < 2:
        return '<div style="color:#008833;padding:20px;text-align:center;">Gromadzenie punktów pomiarowych...</div>'

    step = (width - 2 * padding) / (n - 1)
    max_lat = max([p.get("lat", 0.0) for p in pts] + [60.0])

    path_d = []
    bars = []
    for i, p in enumerate(pts):
        x = padding + i * step
        lat = p.get("lat", 0.0)
        online = p.get("online", True)

        if not online:
            bars.append(f'<line x1="{x}" y1="{padding}" x2="{x}" y2="{height - padding}" stroke="#ff3333" stroke-width="3" opacity="0.85"><title>{p.get("t")}: OFFLINE</title></line>')
            y = height - padding
        else:
            y = (height - padding) - (lat / max_lat) * (height - 2 * padding)
            bars.append(f'<circle cx="{x}" cy="{y}" r="2" fill="#00ff66"><title>{p.get("t")}: {lat} ms</title></circle>')

        cmd = "M" if i == 0 else "L"
        path_d.append(f"{cmd} {x:.1f} {y:.1f}")

    svg = f'''
    <svg viewBox="0 0 {width} {height}" style="width:100%;height:auto;display:block;background:rgba(0,10,5,0.7);border-radius:4px;border:1px solid #11331a;">
      <line x1="{padding}" y1="{height-padding}" x2="{width-padding}" y2="{height-padding}" stroke="#1a4d26" stroke-width="1"/>
      <line x1="{padding}" y1="{padding}" x2="{width-padding}" y2="{padding}" stroke="#1a4d26" stroke-width="1" stroke-dasharray="4"/>
      <text x="{padding}" y="{padding-8}" fill="#00aa44" font-size="10">Maks. opóźnienie: {max_lat:.0f} ms</text>
      <text x="{width-padding}" y="{padding-8}" fill="#00aa44" font-size="10" text-anchor="end">Ostatnie 60 cykli</text>
      {"".join(bars)}
      <path d="{' '.join(path_d)}" fill="none" stroke="#00ff66" stroke-width="1.8" opacity="0.75"/>
      <text x="{padding}" y="{height-8}" fill="#00aa44" font-size="10">{pts[0].get("t")}</text>
      <text x="{width-padding}" y="{height-8}" fill="#00aa44" font-size="10" text-anchor="end">{pts[-1].get("t")}</text>
    </svg>
    '''
    return svg


class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Wyciszenie standardowych logów HTTP w konsoli
        return

    def send_json_response(self, code, obj):
        data = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url_p = urllib.parse.urlparse(self.path)
        path = url_p.path

        if path == "/api/status":
            with lock:
                res = {
                    "wan_online": state["wan_online"],
                    "latency_ms": state["latency_ms"],
                    "consecutive_fails": state["consecutive_fails"],
                    "last_check": state["last_check"],
                    "next_check": state["next_check"],
                    "last_reboot": state["last_reboot"],
                    "cooldown_active": state["cooldown_active"]
                }
            self.send_json_response(200, res)
            return

        elif path == "/api/state":
            with lock:
                res = dict(state)
            self.send_json_response(200, res)
            return

        elif path in ("/", "/index.html"):
            self.render_dashboard()
            return

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        url_p = urllib.parse.urlparse(self.path)
        path = url_p.path
        cfg = load_config()

        if path == "/api/check":
            is_on, lat = check_wan_connectivity()
            with lock:
                state["wan_online"] = is_on
                state["latency_ms"] = lat
                state["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_message(f"Ręczny test WAN: {'ONLINE' if is_on else 'OFFLINE'} ({lat} ms)")
            self.send_json_response(200, {"online": is_on, "latency_ms": lat})
            return

        elif path == "/api/diagnostics":
            diag = run_full_network_diagnostics(cfg, trigger="MANUAL_WEB_UI")
            self.send_json_response(200, diag)
            return

        elif path == "/api/reboot":
            success, rec = perform_reboot(cfg, trigger="MANUAL_WEB_UI")
            self.send_json_response(200 if success else 500, rec)
            return

        elif path == "/api/omada/console":
            length = int(self.headers.get("Content-Length", 0))
            body_raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            try:
                payload = json.loads(body_raw)
            except Exception:
                payload = {}

            method = payload.get("method", "GET").upper()
            req_path = payload.get("path", "").strip()
            body_data = payload.get("body")

            client = OmadaClient(cfg)
            http_code, err_code, res = client.request(method, req_path, body=body_data)
            err_desc = OMADA_ERRORS.get(err_code, "Nieznany kod błędu Omada API")

            self.send_json_response(200, {
                "http_code": http_code,
                "error_code": err_code,
                "error_description": err_desc,
                "response": res
            })
            return

        else:
            self.send_response(404)
            self.end_headers()

    def render_dashboard(self):
        cfg = load_config()
        with lock:
            st = dict(state)

        status_text = "ONLINE" if st["wan_online"] else "OFFLINE"
        status_color = "#00ff66" if st["wan_online"] else "#ff3333"
        cooldown_status = "Aktywny" if st["cooldown_active"] else "Nieaktywny"
        svg_chart = generate_svg_chart(st["history"])

        # Generowanie kart diagnostyki (zamiast rozjeżdżającej się tabeli)
        diag_cards_html = []
        for d in st["diagnostics_history"][:10]:
            api_info = d.get("api_omada", {})
            api_code = api_info.get("code", 0)
            api_desc = api_info.get("description", "")
            api_badge_color = "#00ff66" if api_code == 0 else "#ffaa00"

            lxc_info = d.get("lxc_route", {})
            brama = lxc_info.get("brama_lxc", "brak")
            ping_brama = lxc_info.get("ping_bramy", {}).get("ok", False)
            ping_brama_badge = '<span class="badge badge-ok">Brama OK</span>' if ping_brama else '<span class="badge badge-err">Brama FAIL</span>'

            dns_info = d.get("test_dns", {})
            dns_ok = dns_info.get("ok", False)
            dns_badge = '<span class="badge badge-ok">DNS OK</span>' if dns_ok else '<span class="badge badge-err">DNS FAIL</span>'

            raw_json_str = json.dumps(d.get("omada_raw", {}), indent=2, ensure_ascii=False)

            card = f'''
            <div class="diag-card">
              <div class="diag-header">
                <div>
                  <strong style="color:#00ff66;">{d.get("timestamp")}</strong>
                  <span class="badge" style="border-color:#00aa44;color:#00ff66;">{d.get("trigger")}</span>
                </div>
                <div>
                  <span class="badge" style="border-color:{api_badge_color};color:{api_badge_color};">Omada Kod: {api_code}</span>
                  {ping_brama_badge}
                  {dns_badge}
                </div>
              </div>
              <div class="diag-summary">
                <div><strong>Brama LAN LXC:</strong> {brama}</div>
                <div><strong>Stan Omada API:</strong> {api_desc}</div>
              </div>
              <details style="margin-top:8px;">
                <summary style="cursor:pointer;color:#00cc55;font-size:0.85rem;">Pokaż pełne dane ER605 / Odpowiedź API</summary>
                <pre class="json-box">{raw_json_str}</pre>
              </details>
            </div>
            '''
            diag_cards_html.append(card)

        if not diag_cards_html:
            diag_cards_html.append('<div style="color:#008833;padding:15px;text-align:center;">Brak wpisów diagnostycznych.</div>')

        # Wiersze tabeli audytu restartów
        audit_rows = []
        for r in st["reboot_history"][:10]:
            st_color = "#00ff66" if r.get("status") == "SUCCESS" else "#ff3333"
            snap_json = json.dumps(r.get("pre_reboot_snapshot", {}), indent=2, ensure_ascii=False)
            row = f'''
            <tr>
              <td>{r.get("timestamp")}</td>
              <td><code>{r.get("trigger")}</code></td>
              <td style="color:{st_color};font-weight:bold;">{r.get("status")} (HTTP {r.get("http_code", "")})</td>
              <td>
                <details>
                  <summary style="cursor:pointer;color:#00cc55;">Zrzut WAN</summary>
                  <pre class="json-box">{snap_json}</pre>
                </details>
              </td>
            </tr>
            '''
            audit_rows.append(row)

        if not audit_rows:
            audit_rows.append('<tr><td colspan="4" style="text-align:center;color:#008833;">Brak zarejestrowanych restartów.</td></tr>')

        logs_content = "\n".join(st["logs"][-35:])

        html = f"""<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>OMADA ER605 WATCHDOG // SYSTEM CYBERNETYCZNY</title>
  <style>
    :root {{
      --bg: #050807;
      --card-bg: rgba(7, 18, 11, 0.85);
      --border: #13381e;
      --green: #00ff66;
      --dim-green: #00aa44;
      --alert: #ff3333;
      --warn: #ffaa00;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 16px;
      background-color: var(--bg);
      background-image: radial-gradient(circle at var(--mouse-x, 50%) var(--mouse-y, 50%), rgba(0, 255, 102, 0.08) 0%, transparent 60%);
      color: #b8e6c4;
      font-family: Consolas, 'Courier New', monospace;
      font-size: 14px;
      min-height: 100vh;
    }}
    .container {{
      max-width: 1100px;
      margin: 0 auto;
    }}
    header {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      border-bottom: 2px solid var(--border);
      padding-bottom: 12px;
      margin-bottom: 20px;
    }}
    h1 {{
      font-size: 1.35rem;
      color: var(--green);
      margin: 0;
      letter-spacing: 1px;
      text-shadow: 0 0 8px rgba(0,255,102,0.4);
    }}
    .node-info {{
      font-size: 0.85rem;
      color: var(--dim-green);
    }}
    .actions-bar {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 20px;
    }}
    button, select, input, textarea {{
      font-family: inherit;
      background: #091a10;
      color: var(--green);
      border: 1px solid var(--border);
      padding: 8px 14px;
      border-radius: 4px;
      cursor: pointer;
      transition: all 0.2s;
    }}
    button:hover {{
      background: #11381e;
      border-color: var(--green);
      box-shadow: 0 0 10px rgba(0,255,102,0.3);
    }}
    .btn-danger {{
      color: #ff6666;
      border-color: #551111;
    }}
    .btn-danger:hover {{
      background: #330a0a;
      border-color: #ff3333;
      box-shadow: 0 0 10px rgba(255,51,51,0.3);
    }}
    .grid-cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }}
    .card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 16px;
      position: relative;
    }}
    .card-title {{
      font-size: 0.8rem;
      color: var(--dim-green);
      text-transform: uppercase;
      letter-spacing: 1px;
      margin-bottom: 8px;
    }}
    .card-value {{
      font-size: 1.6rem;
      font-weight: bold;
      color: var(--green);
    }}
    .card-sub {{
      font-size: 0.8rem;
      color: #77a885;
      margin-top: 6px;
    }}
    .section-title {{
      font-size: 1.05rem;
      color: var(--green);
      border-left: 3px solid var(--green);
      padding-left: 8px;
      margin: 28px 0 14px 0;
      text-transform: uppercase;
    }}
    .diag-card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 12px;
      margin-bottom: 12px;
    }}
    .diag-header {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 8px;
      border-bottom: 1px solid #142e1b;
      padding-bottom: 6px;
      margin-bottom: 8px;
    }}
    .diag-summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 8px;
      font-size: 0.85rem;
    }}
    .badge {{
      display: inline-block;
      padding: 2px 6px;
      border-radius: 3px;
      border: 1px solid;
      font-size: 0.75rem;
      margin-left: 4px;
    }}
    .badge-ok {{ border-color: #00aa44; color: #00ff66; }}
    .badge-err {{ border-color: #aa2222; color: #ff5555; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-bottom: 16px;
      background: var(--card-bg);
      border-radius: 6px;
      overflow: hidden;
      border: 1px solid var(--border);
    }}
    th, td {{
      padding: 10px 12px;
      text-align: left;
      border-bottom: 1px solid var(--border);
      word-break: break-word;
    }}
    th {{
      background: #091c10;
      color: var(--green);
      font-size: 0.85rem;
    }}
    .json-box {{
      background: #030805;
      border: 1px solid #0f2b17;
      padding: 10px;
      border-radius: 4px;
      color: #9fe0b0;
      font-size: 0.8rem;
      max-height: 250px;
      overflow-y: auto;
      white-space: pre-wrap;
    }}
    .console-form {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 16px;
      margin-bottom: 24px;
    }}
    .form-group {{
      margin-bottom: 12px;
    }}
    .form-group label {{
      display: block;
      color: var(--dim-green);
      font-size: 0.85rem;
      margin-bottom: 4px;
    }}
    .form-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    input[type="text"] {{
      flex: 1;
      min-width: 250px;
    }}
    #logOutput {{
      height: 220px;
    }}
    @media (max-width: 768px) {{
      body {{ padding: 8px; }}
      .card-value {{ font-size: 1.3rem; }}
      header {{ flex-direction: column; align-items: flex-start; gap: 8px; }}
      .form-row {{ flex-direction: column; }}
      input[type="text"] {{ width: 100%; }}
      th, td {{ font-size: 0.8rem; padding: 6px; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>OMADA ER605 WATCHDOG // SYSTEM CYBERNETYCZNY</h1>
        <div class="node-info">Węzeł: LXC 107 | Cel: ER605 BRAMA ({cfg.get("er605_mac")}) | Kontroler: {cfg.get("controller_url")}</div>
      </div>
    </header>

    <div class="actions-bar">
      <button onclick="triggerPing()">TEST PING WAN</button>
      <button onclick="triggerDiag()">PEŁNA DIAGNOSTYKA WAN</button>
      <button class="btn-danger" onclick="triggerReboot()">WYMUŚ RESTART ER605</button>
    </div>

    <div class="grid-cards">
      <div class="card">
        <div class="card-title">STATUS POŁĄCZENIA WAN</div>
        <div class="card-value" style="color:{status_color};">{status_text}</div>
        <div class="card-sub">Latencja: {st["latency_ms"]} ms</div>
      </div>
      <div class="card">
        <div class="card-title">BŁĘDY / KOLEJNY CYKL</div>
        <div class="card-value">{st["consecutive_fails"]} / {cfg.get("max_retries", 3)}</div>
        <div class="card-sub">Następny test: {st.get("next_check", "nieznany")}</div>
      </div>
      <div class="card">
        <div class="card-title">OSTATNI RESTART / COOLDOWN</div>
        <div class="card-value" style="font-size:1.15rem;">{st.get("last_reboot", "Brak")}</div>
        <div class="card-sub">Cooldown {cfg.get("cooldown_hours", 3)}h: {cooldown_status}</div>
      </div>
    </div>

    <div class="section-title">HISTORIA LATENCJI I ŁĄCZNOŚCI (24 GODZINY)</div>
    {svg_chart}

    <div class="section-title">HISTORIA DIAGNOSTYKI (PRÓBY OFFLINE I TESTY RĘCZNE)</div>
    <div id="diagList">
      {"".join(diag_cards_html)}
    </div>

    <div class="section-title">AUDYT RESTARTÓW ROUTERA</div>
    <div style="overflow-x:auto;">
      <table>
        <thead>
          <tr>
            <th>Czas</th>
            <th>Wyzwalacz</th>
            <th>Wynik operacji</th>
            <th>Zrzut stanu przed restartem</th>
          </tr>
        </thead>
        <tbody>
          {"".join(audit_rows)}
        </tbody>
      </table>
    </div>

    <div class="section-title">KONSOLA OMADA OPEN API & DIAGNOSTYKA BŁĘDÓW</div>
    <div class="console-form">
      <div class="form-group">
        <label>Gotowe komendy z prawidłowymi ścieżkami API:</label>
        <select id="presetCmd" onchange="applyPreset()" style="width:100%;">
          <option value="">-- Wybierz gotową komendę do kontrolera --</option>
          <option value="GET|/sites/{cfg.get('site_id')}/devices?page=1&pageSize=50">📋 Lista urządzeń w witrynie (w tym ER605)</option>
          <option value="GET|/sites/{cfg.get('site_id')}/gateways">🌐 Bramy sieciowe (Gateways) w bieżącej witrynie</option>
          <option value="GET|/sites?page=1&pageSize=50">🏛️ Lista witryn w kontrolerze</option>
          <option value="GET|/sites/{cfg.get('site_id')}/events?page=1&pageSize=50">⚠️ Zdarzenia w witrynie (Events)</option>
          <option value="GET|/sites/{cfg.get('site_id')}/alerts?page=1&pageSize=50">🚨 Alerty w witrynie (Alerts)</option>
          <option value="POST|/sites/{cfg.get('site_id')}/devices/{format_mac_dashes(cfg.get('er605_mac'))}/reboot">🔄 Wyślij polecenie restartu ER605</option>
        </select>
      </div>
      <div class="form-row">
        <select id="apiMethod" style="width:100px;">
          <option value="GET">GET</option>
          <option value="POST">POST</option>
        </select>
        <input type="text" id="apiPath" placeholder="/sites/.../devices?page=1&pageSize=50">
        <button onclick="sendApiRequest()">WYŚLIJ DO KONTROLERA</button>
      </div>
      <div class="form-group" style="margin-top:12px;">
        <label>Odpowiedź kontrolera Omada:</label>
        <pre id="apiConsoleOutput" class="json-box">Oczekiwanie na zapytanie...</pre>
      </div>
    </div>

    <div class="section-title">LOGI SYSTEMOWE WATCHDOGA</div>
    <textarea id="logOutput" class="json-box" style="width:100%;" readonly>{logs_content}</textarea>
  </div>

  <script>
    document.addEventListener('mousemove', e => {{
      const x = (e.clientX / window.innerWidth) * 100;
      const y = (e.clientY / window.innerHeight) * 100;
      document.body.style.setProperty('--mouse-x', x + '%');
      document.body.style.setProperty('--mouse-y', y + '%');
    }});

    function triggerPing() {{
      fetch('/api/check', {{ method: 'POST' }})
        .then(r => r.json())
        .then(d => {{
          alert('Test WAN zakończony: ' + (d.online ? 'ONLINE' : 'OFFLINE') + ' (' + d.latency_ms + ' ms)');
          location.reload();
        }});
    }}

    function triggerDiag() {{
      const out = document.getElementById('apiConsoleOutput');
      if (out) out.innerText = 'Trwa wykonywanie pełnej diagnostyki...';
      fetch('/api/diagnostics', {{ method: 'POST' }})
        .then(r => r.json())
        .then(d => {{
          alert('Diagnostyka zakończona pomyślnie!');
          location.reload();
        }});
    }}

    function triggerReboot() {{
      if (!confirm('Czy na pewno chcesz natychmiast wymusić restart routera ER605 przez Omada API?')) return;
      fetch('/api/reboot', {{ method: 'POST' }})
        .then(r => r.json())
        .then(d => {{
          alert('Komenda wysłana! Wynik: ' + d.status + ' (HTTP ' + d.http_code + ')');
          location.reload();
        }});
    }}

    function applyPreset() {{
      const val = document.getElementById('presetCmd').value;
      if (!val) return;
      const parts = val.split('|');
      document.getElementById('apiMethod').value = parts[0];
      document.getElementById('apiPath').value = parts[1];
    }}

    function sendApiRequest() {{
      const method = document.getElementById('apiMethod').value;
      const path = document.getElementById('apiPath').value;
      const out = document.getElementById('apiConsoleOutput');
      out.innerText = 'Wysyłanie zapytania do Omada Open API...';

      fetch('/api/omada/console', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ method: method, path: path }})
      }})
      .then(r => r.json())
      .then(d => {{
        out.innerText = 'HTTP STATUS: ' + d.http_code + '\\nKOD OMADA: ' + d.error_code + ' -> ' + d.error_description + '\\n\\n' + JSON.stringify(d.response, null, 2);
      }})
      .catch(e => {{
        out.innerText = 'BŁĄD POŁĄCZENIA: ' + e;
      }});
    }}
  </script>
</body>
</html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))


def run_server(port=8080):
    server = HTTPServer(("0.0.0.0", port), WebHandler)
    log_message(f"Panel WWW watchdoga wystartował na porcie {port}.")
    server.serve_forever()


if __name__ == "__main__":
    load_state()
    cfg = load_config()

    # Uruchomienie pętli watchdoga w osobnym wątku
    t_watchdog = threading.Thread(target=watchdog_loop, daemon=True)
    t_watchdog.start()

    # Serwer WWW w wątku głównym
    web_port = int(cfg.get("web_port", 8080))
    run_server(web_port)
