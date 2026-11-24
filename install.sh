#!/usr/bin/env bash
# ==============================================================================
# Omada ER605 Watchdog LXC Installer dla Proxmox VE
# Repozytorium: https://github.com/devv0lf/omada-watchdog
# ==============================================================================
set -euo pipefail

# 1. Sprawdzenie środowiska Proxmox VE
if [ ! -f /etc/pve/pve-root-ca.pem ] || ! command -v pct &>/dev/null; then
  echo "BŁĄD: Ten skrypt musi być uruchomiony bezpośrednio na hoście Proxmox VE (jako root)."
  exit 1
fi

echo "================================================================="
echo "   OMADA ER605 WATCHDOG // INSTALATOR KONTENERA LXC PROXMOX VE   "
echo "================================================================="
echo

# 2. Wybór ID kontenera
DEFAULT_CTID=$(pvesh get /cluster/nextid 2>/dev/null || echo "107")
read -rp "Podaj ID kontenera LXC [domyślnie: ${DEFAULT_CTID}]: " CT_ID
CT_ID=${CT_ID:-$DEFAULT_CTID}

if pct status "$CT_ID" &>/dev/null; then
  echo "BŁĄD: Kontener o ID $CT_ID już istnieje! Usuń go najpierw lub wybierz inne ID."
  exit 1
fi

# 3. Wybór storage i szablonu
DEFAULT_STORAGE="local-lvm"
if ! pvesm status -storage "$DEFAULT_STORAGE" &>/dev/null; then
  DEFAULT_STORAGE="local"
fi
read -rp "Podaj storage dla dysku LXC [domyślnie: ${DEFAULT_STORAGE}]: " STORAGE
STORAGE=${STORAGE:-$DEFAULT_STORAGE}

echo ">> Wyszukiwanie szablonu Debiana..."
TEMPLATE=$(pveam list local 2>/dev/null | awk '/debian-12/ {print $1}' | tail -n1 || true)
if [ -z "$TEMPLATE" ]; then
  TEMPLATE=$(pveam list "$STORAGE" 2>/dev/null | awk '/debian-12/ {print $1}' | tail -n1 || true)
fi

if [ -z "$TEMPLATE" ]; then
  echo ">> Pobieranie oficjalnego szablonu Debian 12 (bookworm)..."
  pveam update >/dev/null
  AVAILABLE_TMPL=$(pveam available -section system | awk '/debian-12-standard.*amd64/ {print $2}' | tail -n1)
  if [ -n "$AVAILABLE_TMPL" ]; then
    pveam download local "$AVAILABLE_TMPL"
    TEMPLATE="local:vztmpl/${AVAILABLE_TMPL}"
  else
    echo "BŁĄD: Nie znaleziono szablonu debian-12. Pobierz go w Proxmoxie przez GUI (local -> CT Templates)."
    exit 1
  fi
fi

echo "Wybrany szablon: $TEMPLATE"

# 4. Konfiguracja sieci
read -rp "Mostek sieciowy [domyślnie: vmbr0]: " BRIDGE
BRIDGE=${BRIDGE:-vmbr0}

read -rp "Adres IP kontenera (np. 192.168.0.35/24 lub dhcp) [domyślnie: dhcp]: " IP_ADDR
IP_ADDR=${IP_ADDR:-dhcp}

GW_ARG=""
if [ "$IP_ADDR" != "dhcp" ]; then
  read -rp "Brama domyślna (np. 192.168.0.1): " GATEWAY
  if [ -n "$GATEWAY" ]; then
    GW_ARG=",gw=${GATEWAY}"
  fi
fi

echo
echo "--- KONFIGURACJA OMADA CONTROLLER OPEN API ---"
read -rp "URL kontrolera Omada [domyślnie: https://192.168.0.4/]: " CONTROLLER_URL
CONTROLLER_URL=${CONTROLLER_URL:-https://192.168.0.4/}
CONTROLLER_URL="${CONTROLLER_URL%/}/"

read -rp "Omada Controller ID (omadacId, np. f96897b03e1cfbfde177edb61dfb1b2c): " OMADA_ID
read -rp "Site ID (zwykle 'Default' lub hash z URL, np. 698cbfa284806d1d204161b9): " SITE_ID
read -rp "Client ID (appId z Open API): " CLIENT_ID
read -rp "Client Secret (secret z Open API): " CLIENT_SECRET
read -rp "Adres MAC routera ER605 (np. B8:FB:B3:2D:B4:5E): " ER605_MAC
read -rp "Port panelu WWW [domyślnie: 8080]: " WEB_PORT
WEB_PORT=${WEB_PORT:-8080}

echo
echo ">> 1/6 Tworzenie kontenera LXC ($CT_ID)..."
NET_CFG="name=eth0,bridge=${BRIDGE},ip=${IP_ADDR}${GW_ARG}"
pct create "$CT_ID" "$TEMPLATE" \
  --ostype debian \
  --hostname omada-watchdog \
  --cores 1 \
  --memory 512 \
  --swap 256 \
  --features nesting=1 \
  --net0 "$NET_CFG" \
  --storage "$STORAGE" \
  --rootfs "${STORAGE}:4" \
  --unprivileged 1 \
  --onboot 1

echo ">> 2/6 Uruchamianie kontenera..."
pct start "$CT_ID"

echo ">> 3/6 Oczekiwanie na sieć w kontenerze..."
for i in {1..20}; do
  if pct exec "$CT_ID" -- ping -c 1 -W 2 1.1.1.1 &>/dev/null; then
    break
  fi
  sleep 1
done

echo ">> 4/6 Instalacja zależności w kontenerze..."
pct exec "$CT_ID" -- bash -c "apt-get update -qq && apt-get install -y -qq python3 curl iproute2 iputils-ping ca-certificates tzdata >/dev/null"

# Ustawienie strefy czasowej
if [ -f /etc/timezone ]; then
  HOST_TZ=$(cat /etc/timezone)
  pct exec "$CT_ID" -- timedatectl set-timezone "$HOST_TZ" 2>/dev/null || true
fi

echo ">> 5/6 Pobieranie aplikacji oraz zapis konfiguracji..."
pct exec "$CT_ID" -- mkdir -p /opt/omada-watchdog
pct exec "$CT_ID" -- curl -fsSL "https://raw.githubusercontent.com/devv0lf/omada-watchdog/main/app.py" -o /opt/omada-watchdog/app.py
pct exec "$CT_ID" -- chmod 755 /opt/omada-watchdog/app.py

# Bezpieczne wygenerowanie config.json na hoście bez problemów ze znakami specjalnymi
TMP_CFG=$(mktemp)
python3 -c '
import json, sys
data = {
    "controller_url": sys.argv[1],
    "omada_url": sys.argv[1],
    "omada_id": sys.argv[2],
    "omadac_id": sys.argv[2],
    "site_id": sys.argv[3],
    "client_id": sys.argv[4],
    "client_secret": sys.argv[5],
    "er605_mac": sys.argv[6],
    "target_mac": sys.argv[6],
    "check_interval_minutes": 30,
    "retry_interval_minutes": 1,
    "max_retries": 3,
    "cooldown_hours": 3,
    "web_port": int(sys.argv[7]),
    "post_reboot_wait_minutes": 10,
    "ping_hosts": ["1.1.1.1", "8.8.8.8", "1.0.0.1"]
}
with open(sys.argv[8], "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
' "$CONTROLLER_URL" "$OMADA_ID" "$SITE_ID" "$CLIENT_ID" "$CLIENT_SECRET" "$ER605_MAC" "$WEB_PORT" "$TMP_CFG"

pct push "$CT_ID" "$TMP_CFG" /opt/omada-watchdog/config.json -perms 0600
rm -f "$TMP_CFG"

echo ">> 6/6 Konfiguracja i uruchomienie usługi systemd..."
TMP_SERVICE=$(mktemp)
cat > "$TMP_SERVICE" <<'EOF'
[Unit]
Description=Omada ER605 Watchdog & Cybernetic Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/omada-watchdog
ExecStart=/usr/bin/python3 /opt/omada-watchdog/app.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

pct push "$CT_ID" "$TMP_SERVICE" /etc/systemd/system/omada-watchdog.service -perms 0644
rm -f "$TMP_SERVICE"

pct exec "$CT_ID" -- systemctl daemon-reload
pct exec "$CT_ID" -- systemctl enable --now omada-watchdog.service

LXC_IP=$(pct exec "$CT_ID" -- ip -4 -o addr show eth0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 || true)
if [ -z "$LXC_IP" ]; then
  LXC_IP="<IP_KONTENERA>"
fi

echo
echo "================================================================="
echo "✓ SUKCES! Watchdog zainstalowany i uruchomiony w kontenerze $CT_ID"
echo "  Panel WWW: http://${LXC_IP}:${WEB_PORT}"
echo "================================================================="
