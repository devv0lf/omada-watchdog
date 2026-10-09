# 🛡️ Omada ER605 Watchdog LXC (Proxmox VE)

Kompletne, bezobsługowe rozwiązanie watchdoga sieciowego uruchamiane w dedykowanym, lekkim kontenerze Debian 12 LXC na platformie **Proxmox Virtual Environment (PVE 7 / 8 / 9)**.

Narzędzie w regularnych interwałach monitoruje łączność ze światem zewnętrznym i w przypadku wykrycia trwałej awarii łącza (3 kolejne nieudane cykle) automatycznie inicjuje restart routera **TP-Link Omada ER605** za pośrednictwem oficjalnego **Omada SDN Open API**. Posiada wbudowany webowy interfejs zarządzający z audytem zdarzeń w czasie rzeczywistym.

---

## 📑 Spis treści

1. [Główne funkcje](#-główne-funkcje)
2. [Zasada działania watchdoga](#-zasada-działania-watchdoga)
3. [Wymagania wstępne w Omada Controller](#-wymagania-wstępne-w-omada-controller)
4. [Instalacja jednym poleceniem](#-instalacja-jednym-poleceniem)
5. [Opis funkcji panelu Web UI](#-opis-funkcji-panelu-web-ui)
6. [Struktura plików w kontenerze](#-struktura-plików-w-kontenerze)
7. [Aktualizacja (Update)](#-aktualizacja-update)
8. [Zarządzanie usługą i diagnostyka](#-zarządzanie-usługą-i-diagnostyka)
9. [Parametry konfiguracyjne](#-parametry-konfiguracyjne-configjson)
10. [Architektura API Omady](#-architektura-api-omady)
11. [Bezpieczeństwo](#-bezpieczeństwo)
12. [Licencja](#-licencja)

---

## 🚀 Główne funkcje

- **Automatyczny watchdog ICMP**: Cykliczne testowanie łączności z wieloma niezależnymi publicznymi węzłami Anycast (`1.1.1.1` Cloudflare, `8.8.8.8` Google).
- **Potrójna weryfikacja (Debounce)**: Eliminacja fałszywych restartów przy chwilowych wahaniach łącza – restart następuje wyłącznie po 3 kolejnych nieudanych próbach (fail counter).
- **Zabezpieczenie Cooldown**: Konfigurowalna blokada czasowa (domyślnie 3 godziny) uniemożliwiająca wejście routera w niekończącą się pętlę restartów (*boot loop*), gdy awaria leży po stronie dostawcy ISP.
- **Oficjalne Omada SDN Open API v1**: Wykorzystanie bezpiecznego mechanizmu OAuth 2.0 (`client_credentials`) oraz dedykowanego endpointu `/devices/{mac}/reboot` zamiast zawodnego skrobania interfejsu (web scraping) czy nieszyfrowanych zapytań.
- **Weryfikacja Pre-flight podczas instalacji**: Instalator przed utworzeniem kontenera testuje połączenie z kontrolerem, pobiera listę witryn (*sites*) i weryfikuje obecność docelowego routera ER605 na podstawie adresu MAC.
- **Cyberpunk / Matrix Web UI**: Lekki, responsywny panel WWW oparty na czystym HTML/CSS/JS (brak ciężkich frameworków, zerowy narzut pamięciowy):
  - Dynamiczny status połączenia WAN na żywo (odświeżanie co 5 sekund).
  - Pomiar latencji ping w milisekundach.
  - Wykres / historia ostatnich prób.
  - Tabela audytowa **REBOOT_HISTORY & AUDIT_LOG** (czas, wyzwalacz, kod odpowiedzi HTTP, szczegółowy komunikat kontrolera).
  - Przycisk natychmiastowego manualnego restartu z potwierdzeniem.
  - Interaktywne tło reagujące na ruch kursora myszy (*radial gradient glow*).
- **Pełna izolacja LXC**: Kontener zużywa zaledwie ~30-40 MB pamięci RAM i minimalną ilość przestrzeni dyskowej.
- **Synchronizacja strefy czasowej**: Automatyczne mapowanie strefy czasowej hosta Proxmox (np. `Europe/Warsaw`) do kontenera.

---

## ⚙️ Zasada działania watchdoga

1. Usługa `omada-watchdog.service` startuje w tle kontenera i uruchamia pętlę sprawdzającą co **30 minut**.
2. W każdej iteracji wysyłany jest pakiet ICMP (ping z timeoutem 2s) do zdefiniowanych celów (`1.1.1.1` oraz `8.8.8.8`):
   - Jeśli którykolwiek cel odpowie: status WAN ustawiany jest na `ONLINE`, licznik błędów jest zerowany, mierzona jest latencja i zapisywana w historii 24h.
   - Jeśli żaden cel nie odpowie: licznik awarii zwiększa się o 1 (`fail_count++`).
3. Po zarejestrowaniu 3 kolejnych nieudanych iteracji (łącznie 90 minut braku internetu):
   - Watchdog sprawdza, czy nie obowiązuje aktywny **cooldown**.
   - Jeśli cooldown wygasł: pobierany jest token dostępowy OAuth z kontrolera Omada, wysyłane jest żądanie restartu routera ER605, cooldown zostaje zresetowany na kolejne 3 godziny, a wynik zdarzenia trafia do tabeli audytowej.
   - Jeśli cooldown jest aktywny: restart jest pomijany, a informacja odnotowywana w logu.

---

## 📋 Wymagania wstępne w Omada Controller

1. Zaloguj się do swojego lokalnego kontrolera **Omada SDN Controller** (np. `https://192.168.0.4:443`).
2. Przejdź do: **Global View** ➔ **Settings** ➔ **Open API**.
3. Włącz przełącznik **Open API**.
4. W sekcji **Attributes** odczytaj i skopiuj:
   - **Interface Access Address**: URL dostępu do API kontrolera (np. `https://192.168.0.4`).
   - **Omada ID**: Identyfikator kontrolera (32-znakowy ciąg hex, np. `f96897b03e1cfbfde177edb61dfb1b2c`).
5. W sekcji aplikacji Open API utwórz nową aplikację i wygeneruj:
   - **Client ID**
   - **Client Secret**
6. Sprawdź i przygotuj adres **MAC** routera ER605 (widoczny w zakładce *Devices*, format: `AA-BB-CC-DD-EE-FF`).

---

## 📥 Instalacja jednym poleceniem

Zaloguj się przez SSH do hosta **Proxmox VE** lub otwórz konsolę `Shell` w panelu PVE i uruchom:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/devv0lf/omada-watchdog/main/install.sh)"
