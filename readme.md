# 🛡️ Omada ER605 Watchdog LXC (Proxmox VE)

Kompletne, bezobsługowe rozwiązanie watchdoga sieciowego uruchamiane w dedykowanym, ultralekkim kontenerze Debian 12 LXC na platformie **Proxmox Virtual Environment (PVE 7 / 8 / 9)**.

Narzędzie monitoruje łączność ze światem zewnętrznym i w przypadku wykrycia trwałej awarii łącza automatycznie inicjuje restart routera **TP-Link Omada ER605** za pośrednictwem oficjalnego **Omada SDN Open API v1**. Posiada wbudowany webowy interfejs w stylu Matrix/Cyberpunk z wykresem 24h, audytem restartów w czasie rzeczywistym oraz endpointami pod Home Assistanta.

---

## 📑 Spis treści
1. [Główne funkcje](#-główne-funkcje)
2. [Zasada działania watchdoga](#-zasada-działania-watchdoga)
3. [Wymagania wstępne w Omada Controller](#-wymagania-wstępne-w-omada-controller)
4. [Instalacja jednym poleceniem](#-instalacja-jednym-poleceniem)
5. [Aktualizacja (Update)](#-aktualizacja-update)
6. [Opis funkcji panelu Web UI](#-opis-funkcji-panelu-web-ui)
7. [Integracja z Home Assistant](#-integracja-z-home-assistant)
8. [Struktura plików w kontenerze](#-struktura-plików-w-kontenerze)
9. [Zarządzanie usługą i diagnostyka](#-zarządzanie-usługą-i-diagnostyka)
10. [Parametry konfiguracyjne (config.json)](#-parametry-konfiguracyjne-configjson)
11. [Architektura API Omady](#-architektura-api-omady)
12. [Bezpieczeństwo](#-bezpieczeństwo)
13. [Licencja](#-licencja)

---

## 🚀 Główne funkcje
- **Automatyczny watchdog ICMP**: Cykliczne testowanie łączności z niezależnymi publicznymi węzłami Anycast (`1.1.1.1` Cloudflare, `8.8.8.8` Google).
- **Inteligentne interwały (30 min / 10 min)**: 
  - W trybie normalnym (ONLINE) stan sprawdzany jest co **30 minut**.
  - W przypadku wykrycia braku internetu interwał natychmiast skraca się do **10 minut**, aby szybko zweryfikować awarię.
- **Potrójna weryfikacja (Debounce)**: Eliminacja fałszywych restartów przy chwilowych wahaniach łącza – restart następuje dopiero po 3 kolejnych nieudanych próbach.
- **Zabezpieczenie Cooldown**: Konfigurowalna blokada czasowa (domyślnie 3 godziny) uniemożliwiająca zapętlenie restartów (*boot loop*), gdy awaria leży po stronie operatora (ISP).
- **Oficjalne Omada SDN Open API v1**: Wykorzystanie autoryzacji OAuth 2.0 (`client_credentials`) oraz bezpośredniego endpointu `/devices/{mac}/reboot`.
- **Weryfikacja Pre-flight podczas instalacji**: Skrypt przed utworzeniem kontenera sprawdza połączenie z kontrolerem, pobiera listę witryn (*sites*) i weryfikuje obecność routera po adresie MAC.
- **Cyberpunk / Matrix Web UI**: Lekki, responsywny panel WWW oparty na czystym HTML/CSS/JS (brak ciężkich frameworków, zerowy narzut RAM):
  - Status połączenia WAN i licznik błędów na żywo.
  - Pomiar latencji ping w milisekundach.
  - Wykres słupkowy z historią ostatnich 24 godzin.
  - Tabela **REBOOT_HISTORY & AUDIT_LOG** (czas, trigger, kod HTTP/API, szczegółowy komunikat kontrolera).
  - Przyciski manualnego sprawdzenia sieci oraz restartu routera.
  - Interaktywne tło reagujące na kursor myszy (*radial gradient glow*).
- **Zgodność z Home Assistant**: Wbudowane endpointy `/api/status` oraz `/api/state` pozwalają na prostą integrację przez REST sensor.
- **Niski narzut**: Kontener zużywa zaledwie ~30-40 MB pamięci RAM.

---

## ⚙️ Zasada działania watchdoga

```mermaid
flowchart TD
    A[Start pętli: Status ONLINE] --> B[Ping 1.1.1.1 / 8.8.8.8]
    B -- Sukces --> C[Zapisz latencję, zeruj błędy]
    C --> D[Czekaj 30 minut]
    D --> B
    B -- Brak odpowiedzi --> E[Błąd 1/3: Skróć interwał do 10 min]
    E --> F[Czekaj 10 minut]
    F --> G[Ponowny test]
    G -- Sukces --> C
    G -- Brak odpowiedzi --> H[Błąd 2/3: Czekaj 10 min]
    H --> I[Trzeci test]
    I -- Sukces --> C
    I -- Brak odpowiedzi --> J{Cooldown minął?}
    J -- Tak --> K[Restart routera przez Open API]
    K --> L[Zapisz zdarzenie w tabeli audytu]
    L --> M[Czekaj 10 minut na wstanie routera]
    M --> B
    J -- Nie --> N[Pomiń restart - ISP ma awarię]
    N --> F
