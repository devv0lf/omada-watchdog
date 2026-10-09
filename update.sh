#!/usr/bin/env bash
set -e
REPO_USER="devv0lf" # Zmień na swój login GitHub
REPO_NAME="omada-watchdog"
BRANCH="main"

echo ">> Pobieranie najnowszej wersji app.py z GitHuba..."
curl -fsSL "https://raw.githubusercontent.com/${REPO_USER}/${REPO_NAME}/${BRANCH}/app.py" -o /opt/omada-watchdog/app.py

systemctl restart omada-watchdog
echo "✓ Zaktualizowano pomyślnie! Usługa została zrestartowana."
systemctl status omada-watchdog --no-pager
