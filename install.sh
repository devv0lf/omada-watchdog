# W instalatorze pobieramy app.py prosto z repozytorium:
echo ">> Pobieranie aplikacji app.py z repozytorium..."
pct exec "$CT_ID" -- curl -fsSL "https://raw.githubusercontent.com/devv0lf/omada-watchdog/main/app.py" -o /opt/omada-watchdog/app.py
