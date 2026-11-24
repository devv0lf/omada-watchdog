#!/usr/bin/env python3
"""Omada ER605 Watchdog i lokalny panel WWW (wersja 2.0)."""
import concurrent.futures
import copy
import html
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
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
    # --- nowe opcje (wszystkie opcjonalne) ---
    "web_bind": "0.0.0.0",                  # np. "192.168.0.10", żeby nasłuchiwać tylko na jednym IP
    "verify_ssl": False,                    # False = kontroler z certyfikatem self-signed
    "ca_file": "",                          # ścieżka do certyfikatu kontrolera (włącza weryfikację łańcucha)
    "failed_reboot_cooldown_minutes": 10,   # przerwa po NIEUDANEJ próbie restartu
    "cooldown_recheck_minutes": 5,          # jak często sprawdzać WAN w trakcie cooldownu
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
TCP_FALLBACK_PORTS = (443, 53)
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]*$")

lock = threading.RLock()
reboot_lock = threading.Lock()
diag_lock = threading.Lock()
state = {
    "wan_online": True, "latency_ms": 0.0, "consecutive_fails": 0,
    "last_check": None, "next_check": None, "last_reboot": None,
    "last_reboot_ts": None, "last_attempt_ts": None,
    "cooldown_active": False, "history": [], "reboot_history": [],
    "diagnostics_history": [], "logs": [],
}


# ----------------------------------------------------------------- narzędzia
def log_message(message):
    entry = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(entry, flush=True)
    with lock:
        state["logs"].append(entry)
        state["logs"] = state["logs"][-100:]


def cfg_num(cfg, key, default, minimum, cast=int):
    """Bezpieczne czytanie liczby z configu (zła wartość = domyślna)."""
    try:
        value = cast(cfg.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def cfg_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "tak", "on")
    return bool(value)


def run_with_timeout(fn, timeout):
    box = {}

    def target():
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"przekroczono limit {timeout} s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def json_for_script(value):
    """json.dumps bezpieczny do wstawienia w <script>."""
    return (json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("&", "\\u0026"))


def esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


# ------------------------------------------------------------------- config
_last_good_cfg = None
_last_cfg_error = None


def load_config():
    """Czyta config.json; przy błędzie zwraca ostatnią poprawną konfigurację."""
    global _last_good_cfg, _last_cfg_error
    error = None
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
        if "retry_interval_minutes" not in raw and "retry_delay_seconds" in raw:
            try:
                seconds = max(1, int(raw["retry_delay_seconds"]))
                cfg["retry_interval_minutes"] = max(1, (seconds + 59) // 60)
            except (TypeError, ValueError):
                pass
        _last_good_cfg = cfg
        _last_cfg_error = None
        return dict(cfg)
    except FileNotFoundError:
        error = f"Brak pliku konfiguracyjnego {CONFIG_PATH}"
    except Exception as exc:  # noqa: BLE001
        error = f"Błąd wczytywania config.json: {exc}"
    if error != _last_cfg_error:  # nie spamuj logów przy każdym żądaniu
        _last_cfg_error = error
        suffix = " Używam ostatniej poprawnej konfiguracji." if _last_good_cfg else ""
        log_message(f"BŁĄD: {error}.{suffix}")
    return dict(_last_good_cfg or DEFAULT_CONFIG)


def configured_ping_hosts(cfg):
    hosts = cfg.get("ping_hosts", DEFAULT_CONFIG["ping_hosts"])
    if isinstance(hosts, str):
        hosts = [item.strip() for item in hosts.split(",")]
    if not isinstance(hosts, list):
        return list(DEFAULT_CONFIG["ping_hosts"])
    clean = [str(h).strip() for h in hosts if HOST_RE.match(str(h).strip())]
    return clean or list(DEFAULT_CONFIG["ping_hosts"])


def format_mac_dashes(mac):
    clean = str(mac).replace(":", "").replace("-", "").strip().upper()
    if len(clean) == 12 and all(c in "0123456789ABCDEF" for c in clean):
        return "-".join(clean[i:i + 2] for i in range(0, 12, 2))
    return str(mac).strip()


def validate_for_reboot(cfg):
    """Zwraca tekst błędu albo None, jeśli konfiguracja nadaje się do restartu."""
    mac = re.sub(r"[:\-\s]", "", str(cfg.get("er605_mac", "")))
    if not re.fullmatch(r"[0-9A-Fa-f]{12}", mac):
        return "Nieprawidłowy er605_mac w config.json (wymagane 12 znaków hex)"
    if not str(cfg.get("site_id", "")).strip():
        return "Brak site_id w config.json"
    if not str(cfg.get("controller_url", "")).strip().lower().startswith(("http://", "https://")):
        return "Nieprawidłowy controller_url w config.json"
    for key in ("omada_id", "client_id", "client_secret"):
        if not str(cfg.get(key, "")).strip():
            return f"Brak {key} w config.json"
    return None


# -------------------------------------------------------------------- stan
def load_state():
    if not os.path.exists(STATE_PATH):
        return
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            raise ValueError("state.json musi zawierać obiekt JSON")
        with lock:
            for key in ("reboot_history", "diagnostics_history"):
                if isinstance(saved.get(key), list):
                    state[key] = [x for x in saved[key] if isinstance(x, dict)]
            if isinstance(saved.get("history"), list):
                state["history"] = [x for x in saved["history"]
                                    if isinstance(x, dict) and isinstance(x.get("ts"), (int, float))]
            state["last_reboot"] = saved.get("last_reboot")
            for key in ("last_reboot_ts", "last_attempt_ts"):
                if isinstance(saved.get(key), (int, float)):
                    state[key] = float(saved[key])
            # Stary state.json miał tylko znacznik ISO.
            if state["last_reboot_ts"] is None and state["last_reboot"]:
                try:
                    state["last_reboot_ts"] = datetime.fromisoformat(state["last_reboot"]).timestamp()
                except (TypeError, ValueError):
                    pass
    except Exception as exc:  # noqa: BLE001
        log_message(f"Błąd odczytu state.json: {exc}")


def save_state():
    with lock:
        data = {"reboot_history": state["reboot_history"][:50],
                "history": state["history"][-3000:],
                "diagnostics_history": state["diagnostics_history"][:30],
                "last_reboot": state["last_reboot"],
                "last_reboot_ts": state["last_reboot_ts"],
                "last_attempt_ts": state["last_attempt_ts"]}
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="state.", suffix=".tmp", dir=BASE_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STATE_PATH)
    except Exception as exc:  # noqa: BLE001
        log_message(f"Błąd zapisu state.json: {exc}")
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def add_history(online, latency):
    now = time.time()
    with lock:
        state["history"].append({"ts": now, "t": datetime.now().strftime("%H:%M"),
                                 "online": bool(online), "lat": latency})
        cutoff = now - 2 * 86400
        state["history"] = [p for p in state["history"] if p.get("ts", 0) >= cutoff][-3000:]


def cooldown_remaining(cfg):
    """Ile sekund zostało do końca blokady restartów (0 = brak blokady)."""
    now = time.time()
    with lock:
        ok_ts, attempt_ts = state["last_reboot_ts"], state["last_attempt_ts"]
    remaining = 0.0
    hours = cfg_num(cfg, "cooldown_hours", 3, 0, float)
    failed_minutes = cfg_num(cfg, "failed_reboot_cooldown_minutes", 10, 0, float)
    # Znaczniki "z przyszłości" (cofnięty zegar) ignorujemy.
    if ok_ts and ok_ts <= now + 300:
        remaining = max(remaining, ok_ts + hours * 3600 - now)
    if attempt_ts and attempt_ts <= now + 300:
        remaining = max(remaining, attempt_ts + failed_minutes * 60 - now)
    return max(0.0, remaining)


# ------------------------------------------------------------ klient Omada
class OmadaClient:
    def __init__(self, cfg):
        self.base_url = str(cfg.get("controller_url", "")).rstrip("/")
        self.omada_id = str(cfg.get("omada_id", ""))
        self.site_id = str(cfg.get("site_id", ""))
        self.client_id = str(cfg.get("client_id", ""))
        self.client_secret = str(cfg.get("client_secret", ""))
        self.token = None
        self.token_expire = 0.0
        self._lock = threading.RLock()
        ca_file = str(cfg.get("ca_file", "") or "").strip()
        if ca_file or cfg_bool(cfg.get("verify_ssl", False)):
            self.ssl_ctx = ssl.create_default_context(cafile=ca_file or None)
            self.ssl_ctx.check_hostname = False  # kontroler zwykle jest adresowany po IP
            self.ssl_ctx.verify_mode = ssl.CERT_REQUIRED
        else:  # lokalny kontroler z certyfikatem self-signed
            self.ssl_ctx = ssl.create_default_context()
            self.ssl_ctx.check_hostname = False
            self.ssl_ctx.verify_mode = ssl.CERT_NONE

    def invalidate_token(self):
        with self._lock:
            self.token = None
            self.token_expire = 0.0

    def get_token(self):
        with self._lock:
            now = time.time()
            if self.token and now < self.token_expire:
                return self.token
            if not all((self.base_url, self.omada_id, self.client_id, self.client_secret)):
                log_message("Błąd Omada: uzupełnij URL, Omada ID, Client ID i Client Secret.")
                return None
            url = f"{self.base_url}/openapi/authorize/token?grant_type=client_credentials"
            payload = {"omadacId": self.omada_id, "client_id": self.client_id,
                       "client_secret": self.client_secret}
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if not isinstance(data, dict) or data.get("errorCode", -1) != 0:
                    code = data.get("errorCode") if isinstance(data, dict) else "?"
                    msg = data.get("msg") if isinstance(data, dict) else data
                    log_message(f"Błąd tokenu Omada: {msg} (kod {code})")
                    return None
                result = data.get("result") or {}
                self.token = result.get("accessToken")
                try:
                    expires_in = int(result.get("expiresIn", 7200))
                except (TypeError, ValueError):
                    expires_in = 7200
                self.token_expire = now + max(60, expires_in - 120)
                return self.token
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                log_message(f"HTTP {exc.code} podczas pobierania tokenu Omada: {raw[:500]}")
                return None
            except Exception as exc:  # noqa: BLE001
                log_message(f"Wyjątek podczas pobierania tokenu Omada: {exc}")
                return None

    def _build_url(self, path):
        path = str(path).strip()
        if not path or "\r" in path or "\n" in path:
            return None, "Nieprawidłowa ścieżka API"
        if not path.startswith("/"):
            path = "/" + path
        parsed = urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc or path.startswith("//"):
            return None, "Podaj ścieżkę API, nie pełny URL"
        if path.startswith("/openapi/"):
            return f"{self.base_url}{path}", None
        return f"{self.base_url}/openapi/v1/{urllib.parse.quote(self.omada_id, safe='')}{path}", None

    def request(self, method, path, body=None):
        method = str(method).upper()
        if method not in ("GET", "POST"):
            return 400, -1002, {"error": "Dozwolone metody: GET i POST"}
        full_url, err = self._build_url(path)
        if err:
            return 400, -1002, {"error": err}
        if method == "POST" and body is None:
            body = {}
        data_bytes = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in (1, 2):
            token = self.get_token()
            if not token:
                return 401, -1003, {"error": "Nie udało się pobrać tokenu autoryzacji"}
            headers = {"Content-Type": "application/json", "Authorization": f"AccessToken={token}"}
            req = urllib.request.Request(full_url, data=data_bytes, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=12) as resp:
                    status, raw = resp.status, resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                status, raw = exc.code, exc.read().decode("utf-8", errors="replace") or str(exc)
            except Exception as exc:  # noqa: BLE001
                return 500, -1000, {"error": str(exc)}
            try:
                parsed_body = json.loads(raw)
            except json.JSONDecodeError:
                parsed_body = {"raw": raw}
            if not isinstance(parsed_body, dict):
                parsed_body = {"raw": parsed_body}
            code = parsed_body.get("errorCode", 0 if status == 200 else -1000)
            if attempt == 1 and (status == 401 or code in (-1003, -30002)):
                self.invalidate_token()  # token wygasł lub unieważniony: jedna ponowna próba
                continue
            return status, code, parsed_body
        return 500, -1000, {"error": "Nieoczekiwany koniec pętli żądania"}

    def reboot_device(self, mac):
        mac_dash = urllib.parse.quote(format_mac_dashes(mac), safe="-")
        path = f"/sites/{urllib.parse.quote(self.site_id, safe='')}/devices/{mac_dash}/reboot"
        return self.request("POST", path, body={})

    def get_gateway_wan_status(self, mac):
        mac_dash = format_mac_dashes(mac).upper()
        path = f"/sites/{urllib.parse.quote(self.site_id, safe='')}/gateways/{mac_dash}/wan-status"
        http_code, code, response = self.request("GET", path)
        if http_code != 200 or code != 0:
            return {"http_code": http_code, "error_code": code,
                    "error_description": OMADA_ERRORS.get(code, "Błąd kontrolera"),
                    "wan_ports": [], "raw_response": response}
        wan_list = response.get("result", [])
        if not isinstance(wan_list, list):
            wan_list = []
        return {"http_code": http_code, "error_code": 0,
                "error_description": "Pobrano status portów WAN",
                "wan_ports": wan_list, "raw_response": response}

    def get_er605_status(self, mac):
        target_dash = format_mac_dashes(mac).upper()
        target_colon = target_dash.replace("-", ":")
        base = f"/sites/{urllib.parse.quote(self.site_id, safe='')}/devices"
        page_size, seen_macs, total_seen = 50, [], 0
        for page in range(1, 11):  # maks. 500 urządzeń
            http_code, code, response = self.request("GET", f"{base}?page={page}&pageSize={page_size}")
            if http_code != 200 or code != 0:
                return {"http_code": http_code, "error_code": code,
                        "error_description": OMADA_ERRORS.get(code, "Błąd kontrolera"),
                        "raw_response": response}
            result = response.get("result", {})
            devices = result.get("data", []) if isinstance(result, dict) else result
            if not isinstance(devices, list):
                devices = []
            devices = [d for d in devices if isinstance(d, dict)]
            total_seen += len(devices)
            seen_macs.extend(d.get("mac") for d in devices)
            found = next((d for d in devices if str(d.get("mac", "")).upper()
                          in (target_dash, target_colon)), None)
            if found:
                return {"http_code": http_code, "error_code": 0,
                        "error_description": "Router zidentyfikowany", "device": found,
                        "summary": {"name": found.get("name"),
                                    "model": found.get("showModel") or found.get("model"),
                                    "ip": found.get("ip"), "status": found.get("status"),
                                    "status_category": found.get("statusCategory"),
                                    "uptime": found.get("uptime") or found.get("uptimeStr"),
                                    "wan_ports": found.get("wanPorts") or found.get("portList") or []}}
            total = result.get("totalRows") if isinstance(result, dict) else None
            try:
                finished = total is None or page * page_size >= int(total)
            except (TypeError, ValueError):
                finished = True
            if not devices or finished:
                break
        return {"http_code": 200, "error_code": 404,
                "error_description": f"Nie znaleziono {target_dash} na liście urządzeń",
                "devices_count": total_seen, "available_macs": seen_macs}


_client = None
_client_sig = None
_client_lock = threading.Lock()


def get_omada(cfg):
    """Jeden współdzielony klient (cache tokenu), odtwarzany po zmianie configu."""
    global _client, _client_sig
    keys = ("controller_url", "omada_id", "site_id", "client_id", "client_secret",
            "verify_ssl", "ca_file")
    sig = tuple(str(cfg.get(k)) for k in keys)
    with _client_lock:
        if _client is None or sig != _client_sig:
            _client, _client_sig = OmadaClient(cfg), sig
        return _client


# ------------------------------------------------------- testy łączności
_ping_warned = False


def _warn_ping(detail):
    global _ping_warned
    if not _ping_warned:
        _ping_warned = True
        log_message(f"UWAGA: polecenie ping nie działa poprawnie ({detail}). "
                    "Używam testu TCP jako zapasowego.")


def ping_target(host, timeout=2):
    try:
        proc = subprocess.run(["ping", "-c", "1", "-W", str(int(timeout)), "--", str(host)],
                              capture_output=True, text=True, timeout=timeout + 2)
    except (OSError, subprocess.SubprocessError) as exc:
        if not isinstance(exc, subprocess.TimeoutExpired):
            _warn_ping(str(exc))
        return False, None, str(exc)
    if proc.returncode not in (0, 1):  # 1 = brak odpowiedzi, >=2 = błąd samego pinga
        _warn_ping(f"kod {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:120]}")
    latency = None
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            if "time=" in line:
                try:
                    latency = float(line.split("time=", 1)[1].split()[0].replace("ms", ""))
                except (ValueError, IndexError):
                    pass
    return proc.returncode == 0, latency, (proc.stdout or proc.stderr)


def tcp_probe(host, timeout=2):
    for port in TCP_FALLBACK_PORTS:
        started = time.time()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True, round((time.time() - started) * 1000, 1), port
        except OSError:
            continue
    return False, None, None


def probe_host(host, timeout=2):
    ok, latency, output = ping_target(host, timeout)
    if ok:
        return {"target": host, "ok": True, "latency_ms": latency, "method": "icmp", "output": output}
    tcp_ok, tcp_latency, port = tcp_probe(host, timeout)
    if tcp_ok:
        return {"target": host, "ok": True, "latency_ms": tcp_latency,
                "method": f"tcp/{port}", "output": output}
    return {"target": host, "ok": False, "latency_ms": None, "method": "icmp+tcp", "output": output}


def probe_hosts(hosts, timeout=2):
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(hosts))) as pool:
        return list(pool.map(lambda h: probe_host(h, timeout), hosts))


def check_wan_connectivity(cfg):
    results = probe_hosts(configured_ping_hosts(cfg))
    latencies = [r["latency_ms"] for r in results if r["ok"] and r["latency_ms"] is not None]
    online = any(r["ok"] for r in results)
    return online, round(sum(latencies) / len(latencies), 1) if latencies else 0.0


def run_full_network_diagnostics(cfg, trigger="MANUAL"):
    with diag_lock:
        log_message(f"Rozpoczęto diagnostykę WAN (wyzwalacz: {trigger})...")
        gateway = "Nieznana"
        gateway_ping = {"ok": False, "output": "Brak"}
        try:
            proc = subprocess.run(["ip", "route", "show", "default"], capture_output=True,
                                  text=True, timeout=3)
            parts = proc.stdout.strip().split()
            if "via" in parts:
                gateway = parts[parts.index("via") + 1]
                gw = probe_host(gateway, 1)
                gateway_ping = {"target": gateway, "ok": gw["ok"], "latency_ms": gw["latency_ms"],
                                "output": gw["output"]}
        except Exception as exc:  # noqa: BLE001
            gateway = f"Błąd: {exc}"
        public_pings = probe_hosts(configured_ping_hosts(cfg))
        try:
            answers = run_with_timeout(lambda: socket.getaddrinfo("example.com", 80), 4)
            dns = {"ok": True, "output": "\n".join(sorted({a[4][0] for a in answers}))}
        except Exception as exc:  # noqa: BLE001
            dns = {"ok": False, "output": f"DNS resolution failed: {exc}"}
        omada_status = {}
        wan_status = {}
        try:
            omada_client = get_omada(cfg)
            mac_cfg = cfg.get("er605_mac", "")
            omada_status = omada_client.get_er605_status(mac_cfg)
            wan_status = omada_client.get_gateway_wan_status(mac_cfg)
        except Exception as exc:  # noqa: BLE001
            omada_status = {"http_code": 500, "error_code": -1000,
                            "error_description": f"Wyjątek routera: {exc}"}
            wan_status = {"http_code": 500, "error_code": -1000,
                          "error_description": f"Wyjątek WAN: {exc}", "wan_ports": []}
        wan_ports = wan_status.get("wan_ports", [])
        entry = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "trigger": trigger,
                 "api_omada": {"http_code": omada_status.get("http_code"),
                               "code": omada_status.get("error_code"),
                               "description": omada_status.get("error_description")},
                 "wan_ports": wan_ports,
                 "lxc_route": {"brama_lxc": gateway, "ping_bramy": gateway_ping},
                 "public_pings": public_pings, "test_dns": dns,
                 "omada_raw": {"router": omada_status, "wan_status": wan_status}}
        with lock:
            state["diagnostics_history"].insert(0, entry)
            state["diagnostics_history"] = state["diagnostics_history"][:30]
        save_state()
        wan_info = "brak danych WAN"
        if wan_ports:
            p = wan_ports[0]
            p_up_str = "UP" if p.get("status") == 1 else "DOWN"
            wan_info = f"{p.get('name', 'WAN')}: link={p_up_str}, IP={p.get('ip', 'brak')}, errs=RX:{p.get('rxErrorPkts', 0)}/TX:{p.get('txErrorPkts', 0)}"
        log_message(f"Diagnostyka: Omada HTTP={omada_status.get('http_code')}, {wan_info}, brama LXC={gateway}, "
                    f"ping publiczny={any(p['ok'] for p in public_pings)}, DNS={dns['ok']}")
        return entry


# ----------------------------------------------------------------- restart
def perform_reboot(cfg, trigger="AUTO_WATCHDOG_FAIL", snapshot=None):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not reboot_lock.acquire(blocking=False):
        return False, {"timestamp": stamp, "trigger": trigger, "status": "BUSY", "http_code": 409,
                       "omada_code": None, "omada_msg": "Restart jest już w toku"}
    try:
        problem = validate_for_reboot(cfg)
        if problem:
            log_message(f"Restart anulowany: {problem}")
            return False, {"timestamp": stamp, "trigger": trigger, "status": "INVALID_CONFIG",
                           "http_code": 400, "omada_code": None, "omada_msg": problem}
        mac = cfg.get("er605_mac", "")
        log_message(f"Inicjalizacja restartu ER605 (MAC: {mac}, trigger: {trigger})...")
        if snapshot is None:
            snapshot = run_full_network_diagnostics(cfg, trigger=f"PRE_REBOOT_{trigger}")
        compact = {k: v for k, v in snapshot.items() if k != "omada_raw"}
        http_code, err_code, response = get_omada(cfg).reboot_device(mac)
        success = http_code == 200 and err_code == 0
        now = datetime.now()
        record = {"timestamp": now.strftime("%Y-%m-%d %H:%M:%S"), "trigger": trigger,
                  "status": "SUCCESS" if success else "FAILED", "http_code": http_code,
                  "omada_code": err_code,
                  "omada_msg": response.get("msg") or OMADA_ERRORS.get(err_code, "Nieznana odpowiedź"),
                  "pre_reboot_snapshot": compact}
        with lock:
            state["last_attempt_ts"] = time.time()
            if success:
                state["last_reboot"] = now.isoformat()
                state["last_reboot_ts"] = time.time()
                state["consecutive_fails"] = 0
            state["reboot_history"].insert(0, record)
            state["reboot_history"] = state["reboot_history"][:50]
        save_state()
        log_message("SUKCES: kontroler przyjął polecenie restartu." if success else
                    f"BŁĄD restartu: HTTP {http_code}, kod {err_code}, odpowiedź: {response}")
        return success, record
    finally:
        reboot_lock.release()


# ---------------------------------------------------------------- watchdog
def watchdog_step():
    """Jeden cykl monitorowania. Zwraca liczbę minut do następnego cyklu."""
    cfg = load_config()
    online, latency = check_wan_connectivity(cfg)
    now = datetime.now()
    remaining = cooldown_remaining(cfg)
    with lock:
        state["wan_online"] = online
        state["latency_ms"] = latency
        state["last_check"] = now.strftime("%Y-%m-%d %H:%M:%S")
        state["cooldown_active"] = remaining > 0
        state["consecutive_fails"] = 0 if online else state["consecutive_fails"] + 1
        fails = state["consecutive_fails"]
    add_history(online, latency)
    save_state()
    retry_delay = cfg_num(cfg, "retry_interval_minutes", 1, 1)

    if online:
        delay = cfg_num(cfg, "check_interval_minutes", 30, 1)
        log_message(f"Status: ONLINE ({latency} ms). Kolejny test za {delay} min.")
        return delay

    max_fails = cfg_num(cfg, "max_retries", 3, 1)
    log_message(f"ALERT: Brak WAN! Próba {fails}/{max_fails}.")
    snapshot = None
    if fails <= max_fails:  # poza limitem (np. w cooldownie) nie zalewamy historii diagnostyką
        snapshot = run_full_network_diagnostics(cfg, trigger=f"OFFLINE_ATTEMPT_{fails}")
    if fails < max_fails:
        log_message(f"Kolejna próba {fails + 1}/{max_fails} za {retry_delay} min.")
        return retry_delay

    remaining = cooldown_remaining(cfg)
    if remaining > 0:
        delay = cfg_num(cfg, "cooldown_recheck_minutes", 5, 1)
        log_message(f"Cooldown aktywny (jeszcze {int(remaining // 60) + 1} min) — pomijam restart. "
                    f"Kolejny test za {delay} min.")
        return delay
    problem = validate_for_reboot(cfg)
    if problem:
        log_message(f"Nie mogę zrestartować ER605: {problem}. Kolejny test za {retry_delay} min.")
        return retry_delay
    log_message(f"Osiągnięto limit {max_fails} prób offline. Wysyłam restart ER605.")
    success, _ = perform_reboot(cfg, trigger="AUTO_WATCHDOG_FAIL", snapshot=snapshot)
    if success:
        delay = cfg_num(cfg, "post_reboot_wait_minutes", 10, 1)
        log_message(f"Test po restarcie za {delay} min.")
        return delay
    return retry_delay


def watchdog_loop():
    log_message("Watchdog WAN wystartował. Pętla monitorowania aktywna.")
    while True:
        delay = 1
        try:
            delay = watchdog_step()
        except Exception as exc:  # noqa: BLE001 — pętla nigdy nie może umrzeć po cichu
            log_message(f"BŁĄD w pętli watchdoga: {exc!r}. Ponawiam za 1 min.")
        next_check = datetime.fromtimestamp(time.time() + delay * 60)
        with lock:
            state["next_check"] = next_check.strftime("%Y-%m-%d %H:%M:%S")
        time.sleep(delay * 60)


# ------------------------------------------------------------------ panel
def generate_svg_chart(history):
    now = time.time()
    start = now - 86400
    points = [p for p in history if isinstance(p, dict)
              and isinstance(p.get("ts"), (int, float)) and p["ts"] >= start]
    if len(points) < 2:
        return '<div class="empty">Gromadzenie punktów pomiarowych...</div>'
    width, height, pad = 900, 150, 30
    span = width - 2 * pad

    def lat_of(p):
        try:
            return max(0.0, float(p.get("lat") or 0))
        except (TypeError, ValueError):
            return 0.0

    max_lat = max([lat_of(p) for p in points] + [60.0])
    marks, path, pen_down = [], [], False
    for p in points:
        x = pad + (p["ts"] - start) / 86400 * span
        if p.get("online"):
            latency = lat_of(p)
            y = height - pad - latency / max_lat * (height - 2 * pad)
            marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2" fill="#00ff66"><title>{esc(p.get("t"))}: {latency:.1f} ms</title></circle>')
            path.append(("L" if pen_down else "M") + f" {x:.1f} {y:.1f}")
            pen_down = True
        else:
            marks.append(f'<line x1="{x:.1f}" y1="{pad}" x2="{x:.1f}" y2="{height - pad}" stroke="#ff3333" stroke-width="2"><title>{esc(p.get("t"))}: OFFLINE</title></line>')
            pen_down = False
    return f'''<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Wykres WAN z 24 godzin">
      <line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" stroke="#1a4d26"/>
      <text x="{pad}" y="18" fill="#00aa44" font-size="10">Maks. opóźnienie: {max_lat:.0f} ms</text>
      <text x="{width - pad}" y="18" fill="#00aa44" font-size="10" text-anchor="end">Ostatnie 24 h</text>
      {''.join(marks)}<path d="{' '.join(path)}" fill="none" stroke="#00ff66" stroke-width="1.5" opacity=".7"/>
      <text x="{pad}" y="{height - 6}" fill="#00aa44" font-size="10">-24 h</text>
      <text x="{width - pad}" y="{height - 6}" fill="#00aa44" font-size="10" text-anchor="end">teraz</text>
    </svg>'''


PAGE_CSS = r""":root{--bg:#050807;--card:rgba(7,18,11,.88);--border:#13381e;--green:#00ff66;--dim:#00aa44;--red:#ff4444}*{box-sizing:border-box}body{margin:0;padding:16px;background:#050807;background-image:radial-gradient(circle at var(--mx,50%) var(--my,50%),rgba(0,255,102,.08),transparent 60%);color:#b8e6c4;font:14px Consolas,'Courier New',monospace;min-height:100vh}.container{max-width:1100px;margin:auto}header{border-bottom:2px solid var(--border);padding-bottom:12px;margin-bottom:20px}h1{font-size:1.3rem;color:var(--green);margin:0 0 6px;text-shadow:0 0 8px #00ff6655}.node-info,.card-title,.card-sub{color:#77a885;font-size:.85rem}.actions,.row{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px}button,select,input,textarea{font:inherit;background:#091a10;color:var(--green);border:1px solid var(--border);padding:9px 12px;border-radius:4px}button{cursor:pointer}button:hover{background:#11381e;border-color:var(--green)}.danger{color:#ff7777;border-color:#551111}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin-bottom:22px}.card,.diag-card,.console{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:14px}.card-title{text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}.value{font-size:1.5rem;font-weight:bold;color:var(--green)}.section{font-size:1rem;color:var(--green);border-left:3px solid var(--green);padding-left:8px;margin:26px 0 12px}.chart{width:100%;height:auto;display:block;background:#000a05;border:1px solid var(--border);border-radius:4px}.diag-card{margin-bottom:10px}.diag-header{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;border-bottom:1px solid #142e1b;padding-bottom:7px;margin-bottom:8px}.diag-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px;font-size:.85rem;margin:10px 0}.badge{display:inline-block;border:1px solid #16803a;border-radius:3px;padding:2px 6px;margin:2px;font-size:.75rem}.badge-ok{color:var(--green);border-color:#00aa44}.badge-err{color:#ff5555;border-color:#aa2222}.green{color:var(--green)}details{margin-top:8px}summary{cursor:pointer;color:#00cc55}.json-box{background:#030805;border:1px solid #0f2b17;padding:10px;border-radius:4px;color:#9fe0b0;font-size:.8rem;max-height:280px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--border)}th,td{padding:9px;text-align:left;border-bottom:1px solid var(--border);overflow-wrap:anywhere}th{color:var(--green);background:#091c10}.table-wrap{overflow-x:auto}input[type=text]{flex:1;min-width:220px}.console{margin-bottom:20px}.empty{color:#008833;padding:14px;text-align:center}#logOutput{width:100%;height:220px}#apiBody{width:100%;height:70px;margin-bottom:12px}@media(max-width:650px){body{padding:8px}h1{font-size:1.05rem}.actions button{width:100%}.row{flex-direction:column}input[type=text]{width:100%;min-width:0}th,td{font-size:.75rem;padding:6px}}"""

PRESET_OPTIONS = '''<option value="">-- Wybierz komendę --</option><option value="GET|/sites/{site}/gateways/{mac}/wan-status">Status WAN1 i kabla (Link UP/DOWN, błędy RX/TX, IP ZTE)</option><option value="GET|/sites/{site}/gateways/{mac}">Szczegóły bramy ER605 i konfiguracja portów</option><option value="GET|/sites/{site}/devices?page=1&amp;pageSize=50">Lista urządzeń witryny</option><option value="GET|/sites?page=1&amp;pageSize=50">Lista witryn</option><option value="GET|/sites/{site}/events?page=1&amp;pageSize=50">Zdarzenia (jeśli obsługiwane)</option><option value="GET|/sites/{site}/alerts?page=1&amp;pageSize=50">Alerty (jeśli obsługiwane)</option><option value="POST|/sites/{site}/devices/{mac}/reboot">Restart ER605</option>'''

PAGE_JS = r"""const SITE=__SITE__,MAC=__MAC__,COOLDOWN=__COOLDOWN__;
const $=id=>document.getElementById(id);
document.addEventListener('mousemove',e=>{document.body.style.setProperty('--mx',(e.clientX/innerWidth*100)+'%');document.body.style.setProperty('--my',(e.clientY/innerHeight*100)+'%')});
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});let d={};try{d=await r.json()}catch(e){}if(!r.ok)throw new Error(d.error||d.omada_msg||('HTTP '+r.status));return d}
async function triggerPing(){try{const d=await post('/api/check');alert('WAN: '+(d.online?'ONLINE':'OFFLINE')+' ('+d.latency_ms+' ms)');location.reload()}catch(e){alert('Błąd: '+e.message)}}
async function triggerDiag(){try{await post('/api/diagnostics');alert('Diagnostyka zakończona.');location.reload()}catch(e){alert('Błąd: '+e.message)}}
async function triggerReboot(){const msg=COOLDOWN?'Cooldown jest aktywny. Wymusić restart ER605 mimo to?':'Czy na pewno wysłać polecenie restartu ER605?';if(!confirm(msg))return;try{const d=await post('/api/reboot');alert('Wynik: '+d.status+' (HTTP '+d.http_code+')');location.reload()}catch(e){alert('Restart nie powiódł się: '+e.message)}}
function applyPreset(){const val=$('presetCmd').value;if(!val)return;const i=val.indexOf('|');const method=val.slice(0,i),path=val.slice(i+1);$('apiMethod').value=method;$('apiPath').value=path.replaceAll('{site}',SITE).replaceAll('{mac}',MAC);$('apiBody').value=method==='POST'?'{}':''}
async function sendApiRequest(){const out=$('apiConsoleOutput');let body;const raw=$('apiBody').value.trim();if(raw){try{body=JSON.parse(raw)}catch(e){out.textContent='BŁĄD: nieprawidłowy JSON w polu Body';return}}out.textContent='Wysyłanie...';try{const d=await post('/api/omada/console',{method:$('apiMethod').value,path:$('apiPath').value,body:body});out.textContent='HTTP: '+d.http_code+'\nKOD OMADA: '+d.error_code+' — '+d.error_description+'\n\n'+JSON.stringify(d.response,null,2)}catch(e){out.textContent='BŁĄD: '+e.message}}
$('btnPing').addEventListener('click',triggerPing);
$('btnDiag').addEventListener('click',triggerDiag);
$('btnReboot').addEventListener('click',triggerReboot);
$('presetCmd').addEventListener('change',applyPreset);
$('btnSend').addEventListener('click',sendApiRequest);
const lg=$('logOutput');lg.scrollTop=lg.scrollHeight;"""


class WebHandler(BaseHTTPRequestHandler):
    server_version = "OmadaWatchdog/2.0"
    timeout = 10  # nie pozwól wolnemu klientowi wisieć w nieskończoność

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

    def _post_allowed(self):
        """Ochrona przed CSRF z innych stron (bez logowania): wymagamy JSON + zgodnego Origin."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self.send_json_response(415, {"error": "Wymagany Content-Type: application/json"})
            return False
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != (self.headers.get("Host") or ""):
            self.send_json_response(403, {"error": "Niezgodny nagłówek Origin"})
            return False
        return True

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Nieprawidłowy Content-Length")
        if length < 0 or length > 65536:
            raise ValueError("Nieprawidłowy rozmiar żądania")
        if not length:
            return {}
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Ciało żądania musi być obiektem JSON")
        return payload

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path not in ("/api/check", "/api/diagnostics", "/api/reboot", "/api/omada/console"):
            self.send_error(404)
            return
        if not self._post_allowed():
            return
        try:
            payload = self._read_json_body()
        except Exception as exc:  # noqa: BLE001
            self.send_json_response(400, {"error": str(exc)})
            return
        cfg = load_config()
        try:
            if path == "/api/check":
                online, latency = check_wan_connectivity(cfg)
                with lock:
                    state["wan_online"], state["latency_ms"] = online, latency
                    state["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                add_history(online, latency)
                save_state()
                log_message(f"Ręczny test WAN: {'ONLINE' if online else 'OFFLINE'} ({latency} ms)")
                self.send_json_response(200, {"online": online, "latency_ms": latency})
            elif path == "/api/diagnostics":
                self.send_json_response(200, run_full_network_diagnostics(cfg, "MANUAL_WEB_UI"))
            elif path == "/api/reboot":
                success, record = perform_reboot(cfg, "MANUAL_WEB_UI")
                if success:
                    status = 200
                elif record.get("status") in ("BUSY", "INVALID_CONFIG"):
                    status = record["http_code"]
                else:
                    status = 502
                self.send_json_response(status, record)
            else:  # /api/omada/console
                method = str(payload.get("method", "GET")).upper()
                if method not in ("GET", "POST"):
                    raise ValueError("Dozwolone metody to GET i POST")
                http_code, err_code, response = get_omada(cfg).request(
                    method, payload.get("path", ""), payload.get("body"))
                self.send_json_response(200, {
                    "http_code": http_code, "error_code": err_code,
                    "error_description": OMADA_ERRORS.get(err_code, "Nieznany kod błędu Omada API"),
                    "response": response})
        except ValueError as exc:
            self.send_json_response(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log_message(f"Błąd obsługi {path}: {exc!r}")
            self.send_json_response(500, {"error": f"Błąd wewnętrzny: {exc}"})

    def render_dashboard(self):
        cfg = load_config()
        with lock:
            st = copy.deepcopy(state)
        online = bool(st.get("wan_online"))
        status_text = "ONLINE" if online else "OFFLINE"
        status_color = "#00ff66" if online else "#ff3333"
        remaining = cooldown_remaining(cfg)
        cooldown = f"Aktywny (jeszcze {int(remaining // 60) + 1} min)" if remaining > 0 else "Nieaktywny"
        diag_cards = []
        for item in st["diagnostics_history"][:10]:
            api = item.get("api_omada", {})
            api_c = api.get("code", "?")
            api_color = "#00ff66" if api_c == 0 else "#ffaa00"
            route = item.get("lxc_route", {})
            gw_ok = route.get("ping_bramy", {}).get("ok", False)
            dns_ok = item.get("test_dns", {}).get("ok", False)
            raw = esc(json.dumps(item.get("omada_raw", {}), indent=2, ensure_ascii=False))

            wan_lines = []
            ports = item.get("wan_ports") or []
            if not ports and isinstance(item.get("omada_raw"), dict):
                ports = item["omada_raw"].get("wan_status", {}).get("wan_ports", [])

            for p in ports:
                p_name = esc(p.get("name") or f"WAN{p.get('port', 1)}")
                p_up = (p.get("status") == 1)
                p_inet = (p.get("internetState") == 1)
                speed_code = p.get("speed")
                speed_str = "1000M" if speed_code == 3 else ("100M" if speed_code == 2 else ("10M" if speed_code == 1 else str(speed_code or "?")))
                duplex_str = "Full" if p.get("duplex") == 2 else ("Half" if p.get("duplex") == 1 else "")
                link_text = f"{p_name} LINK UP ({speed_str} {duplex_str})" if p_up else f"{p_name} LINK DOWN (Brak kabla/ZTE off)"
                link_cls = "badge-ok" if p_up else "badge-err"
                inet_cls = "badge-ok" if p_inet else "badge-err"
                inet_text = "Internet: ONLINE" if p_inet else "Internet: OFFLINE"
                rx_err = p.get("rxErrorPkts", 0)
                tx_err = p.get("txErrorPkts", 0)
                err_color = "#ff5555" if (rx_err > 0 or tx_err > 0) else "var(--green)"
                cable_badge = f'<span class="badge" style="color:{err_color};border-color:{err_color}">Kabel błędy RX/TX: {rx_err}/{tx_err}</span>'
                ip_val = esc(p.get("ip") or p.get("wanPortIpv4Config", {}).get("ip") or "brak IP")
                gw_val = esc(p.get("wanPortIpv4Config", {}).get("gateway") or "brak bramy")
                loss = p.get("loss", 0.0)
                lat = p.get("latency", 0)
                wan_lines.append(f'''<div style="margin:4px 0">
                  <div><b>Fizyczny link:</b> <span class="badge {link_cls}">{esc(link_text)}</span> <span class="badge {inet_cls}">{esc(inet_text)}</span> {cable_badge}</div>
                  <div style="margin-top:5px"><b>IP od ZTE:</b> {ip_val} &nbsp;|&nbsp; <b>Brama ZTE:</b> {gw_val} &nbsp;|&nbsp; <b>Opóźnienie:</b> {lat} ms (utrata: {loss}%)</div>
                </div>''')

            wan_html = "".join(wan_lines) if wan_lines else '<div style="color:#77a885;font-size:0.85rem">Brak portów WAN w tym zrzucie.</div>'
            gw_cls = "badge-ok" if gw_ok else "badge-err"
            gw_txt = "Brama LAN OK" if gw_ok else "Brama LAN FAIL"
            dns_cls = "badge-ok" if dns_ok else "badge-err"
            dns_txt = "DNS OK" if dns_ok else "DNS FAIL"

            diag_cards.append(f'''<article class="diag-card"><div class="diag-header"><div><b class="green">{esc(item.get("timestamp"))}</b> <span class="badge">{esc(item.get("trigger"))}</span></div>
              <div><span class="badge" style="color:{api_color}">Omada: {esc(api_c)}</span><span class="badge {gw_cls}">{gw_txt}</span><span class="badge {dns_cls}">{dns_txt}</span></div></div>
              <div class="diag-summary"><div><b>Brama LAN LXC:</b> {esc(route.get("brama_lxc", "brak"))}</div><div><b>Omada API:</b> {esc(api.get("description", ""))}</div></div>
              <div style="background:#020603;border:1px solid #0f2e18;border-radius:4px;padding:8px;margin:8px 0">{wan_html}</div>
              <details><summary>Pokaż surowe dane ER605 / wan-status API</summary><pre class="json-box">{raw}</pre></details></article>''')
        if not diag_cards:
            diag_cards.append('<div class="empty">Brak wpisów diagnostycznych.</div>')
        audit_rows = []
        for record in st["reboot_history"][:10]:
            color = "#00ff66" if record.get("status") == "SUCCESS" else "#ff3333"
            snap_obj = record.get("pre_reboot_snapshot", {})
            snapshot = esc(json.dumps(snap_obj, indent=2, ensure_ascii=False))
            wan_snap_summary = "Brak danych WAN"
            snap_ports = snap_obj.get("wan_ports") or []
            if snap_ports:
                sp = snap_ports[0]
                sp_up = (sp.get("status") == 1)
                sp_ip = sp.get("ip") or "brak IP"
                sp_err = f"RX:{sp.get('rxErrorPkts', 0)}/TX:{sp.get('txErrorPkts', 0)}"
                wan_snap_up = "LINK UP" if sp_up else "LINK DOWN"
                wan_snap_summary = f"WAN1: {wan_snap_up}, IP: {sp_ip}, błędy kabla: {sp_err}"
            audit_rows.append(f'''<tr><td>{esc(record.get("timestamp"))}</td><td><code>{esc(record.get("trigger"))}</code></td>
              <td style="color:{color};font-weight:bold">{esc(record.get("status"))} (HTTP {esc(record.get("http_code"))})</td>
              <td><div><small class="green">{esc(wan_snap_summary)}</small></div><details><summary>Pełny zrzut przed restartem</summary><pre class="json-box">{snapshot}</pre></details></td></tr>''')
        if not audit_rows:
            audit_rows.append('<tr><td colspan="4" class="empty">Brak zarejestrowanych restartów.</td></tr>')
        logs = esc("\n".join(st["logs"][-35:]))
        chart = generate_svg_chart(st["history"])
        mac = esc(cfg.get("er605_mac", ""))
        controller = esc(cfg.get("controller_url", ""))
        fail_count = esc(st.get("consecutive_fails", 0))
        max_retries = esc(cfg.get("max_retries", 3))
        next_check = esc(st.get("next_check") or "nieznany")
        last_reboot = esc(st.get("last_reboot") or "Brak")
        latency = esc(st.get("latency_ms", 0))
        cooldown_hours = esc(cfg.get("cooldown_hours", 3))
        script = (PAGE_JS.replace("__SITE__", json_for_script(str(cfg.get("site_id", ""))))
                  .replace("__MAC__", json_for_script(format_mac_dashes(cfg.get("er605_mac", ""))))
                  .replace("__COOLDOWN__", "true" if remaining > 0 else "false"))
        nonce = secrets.token_urlsafe(16)
        html_doc = f'''<!doctype html><html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Omada ER605 Watchdog</title>
<style>{PAGE_CSS}</style></head><body><main class="container"><header><h1>OMADA ER605 WATCHDOG // SYSTEM CYBERNETYCZNY</h1><div class="node-info">Węzeł: LXC | Cel: ER605 ({mac}) | Kontroler: {controller}</div></header>
<div class="actions"><button id="btnPing">TEST PING WAN</button><button id="btnDiag">PEŁNA DIAGNOSTYKA WAN</button><button id="btnReboot" class="danger">WYMUŚ RESTART ER605</button></div>
<div class="grid"><section class="card"><div class="card-title">Status WAN</div><div class="value" style="color:{status_color}">{status_text}</div><div class="card-sub">Latencja: {latency} ms</div></section>
<section class="card"><div class="card-title">Błędy / kolejny cykl</div><div class="value">{fail_count} / {max_retries}</div><div class="card-sub">Następny test: {next_check}</div></section>
<section class="card"><div class="card-title">Ostatni restart / cooldown</div><div class="value" style="font-size:1rem">{last_reboot}</div><div class="card-sub">Cooldown {cooldown_hours} h: {esc(cooldown)}</div></section></div>
<h2 class="section">Historia łączności i latencji (24 godziny)</h2>{chart}
<h2 class="section">Historia diagnostyki (offline i testy ręczne)</h2>{''.join(diag_cards)}
<h2 class="section">Audyt restartów routera</h2><div class="table-wrap"><table><thead><tr><th>Czas</th><th>Wyzwalacz</th><th>Wynik</th><th>Zrzut przed restartem</th></tr></thead><tbody>{''.join(audit_rows)}</tbody></table></div>
<h2 class="section">Konsola Omada Open API</h2><div class="console"><label for="presetCmd">Gotowe komendy:</label><select id="presetCmd" style="width:100%;margin:6px 0 12px">{PRESET_OPTIONS}</select>
<div class="row"><select id="apiMethod"><option>GET</option><option>POST</option></select><input type="text" id="apiPath" placeholder="/sites/.../devices?page=1&amp;pageSize=50"><button id="btnSend">WYŚLIJ</button></div>
<label for="apiBody">Body (JSON, opcjonalne, dla POST):</label><textarea id="apiBody" spellcheck="false" placeholder="{{}}"></textarea>
<label>Odpowiedź kontrolera:</label><pre id="apiConsoleOutput" class="json-box">Oczekiwanie na zapytanie...</pre></div>
<h2 class="section">Logi systemowe</h2><textarea id="logOutput" class="json-box" readonly>{logs}</textarea></main>
<script nonce="{nonce}">{script}</script></body></html>'''
        data = html_doc.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
                         "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True


def run_server(bind="0.0.0.0", port=8080):
    server = ThreadingHTTPServer((str(bind), int(port)), WebHandler)
    server.daemon_threads = True
    log_message(f"Panel WWW watchdoga wystartował na {bind}:{port}.")
    server.serve_forever()


if __name__ == "__main__":
    load_state()
    startup_cfg = load_config()
    threading.Thread(target=watchdog_loop, daemon=True, name="watchdog").start()
    run_server(startup_cfg.get("web_bind", "0.0.0.0") or "0.0.0.0",
               cfg_num(startup_cfg, "web_port", 8080, 1))
