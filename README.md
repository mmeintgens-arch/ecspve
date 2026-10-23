# ECS Proxmox – Übersicht der Container und Dienste

Eine einzige Webseite für den Proxmox-Host der Event Crew Steinfurt: Läuft alles? Sind Updates fällig? Mit einem
Klick neu starten, aktualisieren oder einen hängenden Dienst wieder anwerfen – ohne Kommandozeile. Dazu
Schnittstellen für Monitoring-Werkzeuge und nächtliche automatische Updates.

![Übersicht](docs/uebersicht.png)

- **Ein eigener, kleiner Container** (Debian, 1 CPU, 512 MB) – spricht nur **HTTP**, HTTPS macht euer Caddy davor.
- **Keine Abhängigkeiten**: Python aus Debian, keine Pakete aus dem Internet, kein Build-Schritt.
- **Kennt eure Apps**: Website, Wiki, Turniersystem, Recherchetool, StreetworkDoc – mit deren eigener
  Funktionsprüfung (`/healthz`) und deren eigenem Update-Weg (`pve-install.sh --pull` usw.).

## Was die Seite kann

| Bereich | Was | Wie |
|---|---|---|
| Überblick | Proxmox-Host: CPU, Arbeitsspeicher, System-Disk, verfügbare Updates (mit Paketliste), „neuer Kernel – Neustart nötig“, ausgefallene Host-Dienste | alle 10 Sekunden |
| | jeder Container und jede VM: läuft/aus, Laufzeit, IP, CPU, RAM, Disk | alle 10 Sekunden |
| | in jedem Container: die eigenen systemd-Dienste und Zeitpläne (grün = läuft, rot = ausgefallen, grau = ruht planmäßig), Funktionsprüfung der App mit Antwortzeit, Betriebssystem, verfügbare Updates, „Neustart empfohlen“, Snapshots vor Updates | jede Minute |
| Bedienen | Starten, Neustart, Herunterfahren, hart ausschalten | Knöpfe in der Karte |
| | Dienst neu starten | Klick auf den Dienst (z. B. den roten `eventcrew-worker`) |
| | Aktualisieren: System und App, nur Betriebssystem, nur App – vorher ein Snapshot | „Aktualisieren“ bzw. „Mehr“ |
| | alle Container nacheinander aktualisieren, Proxmox-Host aktualisieren | Knöpfe über den Karten bzw. in der Host-Karte |
| | neuen Container anlegen: Name, CPU, RAM, Disk, Speicher, Netzwerk, IP – Debian, unprivilegiert, mit erstem Update | „Container anlegen“ |
| | eigene Funktionsprüfung für jeden Container (eine Adresse im Container) | „festlegen“ in der Karte bzw. „Mehr“ |
| | jedes Update, jeder Neustart mit vollständigem Protokoll | Liste „Aufträge“ |
| Automatisch | Nachtlauf um 04:30 (nach den Sicherungen der Apps): je Container per Häkchen **System** und/oder **App**; Paketlisten überall auffrischen; Bericht auf der Seite und per Benachrichtigung | Karte „Automatische Updates“ |
| Monitoring | `/health` (200/503), `/api/status` (JSON), `/metrics` (Prometheus), Push an ntfy, Heartbeat an Uptime Kuma/healthchecks.io | [Monitoring anbinden](#monitoring-anbinden) |

Die Seite ist bewusst einfach: deutsch, hell und dunkel, auch am Handy bedienbar. Jede Person im Team bekommt einen
eigenen Zugang mit eigenem zweiten Faktor und einer Rolle (siehe [Benutzer](#benutzer)).

<img src="docs/mobil.png" alt="Ansicht am Handy" width="280">

## Wie es aufgebaut ist

```
 Browser ──HTTPS──► Caddy (euer Reverse Proxy) ──HTTP──► Container „ecspve“ :8080
                                                          │  Weboberfläche, Anmeldung, Monitoring,
                                                          │  Nachtlauf (systemd-Timer)
                                                          │
                                                          │  SSH – Schlüssel darf NUR ecspve-helfer starten
                                                          ▼
                                      Proxmox-Host: /usr/local/sbin/ecspve-helfer
                                        pvesh · pct · qm · apt-get · pve-install.sh der Apps
                                                          │
                                                          ▼
                                      Container: ecswebsite · ecswiki · turnierapp · eventcrew · …
```

Ein Container kommt von sich aus nicht an `pct` und die anderen Container heran – und soll es auch nicht. Deshalb
liegt auf dem Host ein kleiner **Helfer** mit einer festen Liste erlaubter Befehle (`uebersicht`, `pruefen`,
`start`, `update`, `dienst` …). Der SSH-Schlüssel des Containers ist in `/root/.ssh/authorized_keys` mit
`restrict,command="/usr/local/sbin/ecspve-helfer"` genau darauf festgelegt: keine Shell, keine Weiterleitungen,
Container-IDs und Dienstnamen werden geprüft. Hat der Container eine feste IP, gilt der Schlüssel nur von dort.

Updates und Neustarts laufen auf dem Host als eigene systemd-Einheit (`ecspve-<Auftrag>`) – sie laufen weiter, auch
wenn die Verbindung abreißt oder die Übersicht selbst neu startet. Mehrere Updates laufen nie gleichzeitig.

### Die Apps, die die Übersicht kennt

Erkannt wird eine App an ihrer systemd-Einheit im Container – egal, welche ID oder welchen Namen der Container hat.

| App | erkannt an | Funktionsprüfung im Container | App-Update (auf dem Host) |
|---|---|---|---|
| Website | `ecswebsite-web.service` | `http://127.0.0.1:8000/healthz` | `/opt/ecswebsite/pve-install.sh --pull --update --ctid=<ID>` |
| Wiki | `ecswiki.service` | `http://127.0.0.1:3100/healthz` | `/opt/ecswiki/pve-install.sh --pull --update --ctid=<ID>` |
| Turniersystem | `turnierapp.service` | `http://127.0.0.1:3000/healthz` | `turnierapp-update-lxc <ID> --pull` (sonst `turnierapp-update` im Container) |
| Recherchetool | `eventcrew-web.service` | `deploy/lxc/probe.py` der App | `pct exec <ID> -- bash /opt/eventcrew-app/deploy/lxc/update.sh` |
| StreetworkDoc | `streetworkdoc.service` | `https://127.0.0.1:8443/api/v1/info` | `git pull` + `deploy/proxmox/create-lxc.sh --update <ID>` im Projektordner (`STREETWORKDOC_QUELLE`) |
| Übersicht (diese App) | `ecspve.service` | `ecspve ping` | `/opt/ecspve/pve-install.sh --pull --update --ctid=<ID>` |

Die Übersicht ist nicht auf diese Apps beschränkt: **jeder** Container und jede VM auf dem Host erscheint von selbst,
auch neue – nach etwa 10 Sekunden. Andere Container (z. B. euer Caddy) zeigt sie genauso, mit Diensten, Updates und
Betriebssystem-Update, nur ohne App-Prüfung und App-Update. Eine eigene Funktionsprüfung legt ihr direkt in der Karte
fest (siehe unten), eigene Update-Befehle in `/etc/ecspve/helfer.conf` auf dem Host (siehe
[Einstellungen](#einstellungen)) – die laufen als root auf dem Host und gehören deshalb nicht in die Weboberfläche.
VMs zeigt die Übersicht mit Status, Ressourcen und Starten/Stoppen; in eine VM hinein kommt der Helfer nicht.

### Container anlegen

![Container anlegen](docs/anlegen.png)

**Container anlegen** über den Karten öffnet ein Formular: Name, Container-ID (sonst die nächste freie), CPU-Kerne,
Arbeitsspeicher, Festplatte, Speicherort, Netzwerk-Brücke, VLAN, IP-Adresse (DHCP oder fest mit Gateway), Autostart.
Daraus wird – wie bei den Install-Skripten der Apps – ein unprivilegierter Container mit der neuesten
Debian-Standardvorlage (`nesting=1`, Zeitzone des Hosts). Danach startet er, bekommt ein zufälliges Root-Passwort
und gleich sein erstes Update. Das Passwort steht **nur** im Protokoll dieses Auftrags (nicht in der Auftragsliste,
nicht in der Protokolldatei) – damit meldet ihr euch in der Proxmox-Konsole als root an. Ist „eingeschaltet lassen“ aus,
wird der Container nach dem Einrichten wieder heruntergefahren. Die Software darin installiert ihr wie gewohnt.

Mehrere neue Container und Updates laufen nacheinander, nie gleichzeitig. Löschen geht bewusst nur in der
Proxmox-Oberfläche.

### Eigene Funktionsprüfung

Für Container ohne eingebaute Prüfung steht in der Karte „keine Prüfung · **festlegen**“ (sonst unter **Mehr**). Dort
tragt ihr eine Adresse ein, die die Übersicht jede Minute **im Container selbst** aufruft, z. B.
`http://127.0.0.1:8123/` für Home Assistant oder `http://127.0.0.1:2019/config/` für Caddy. Antwortet sie mit 200 bis
399, ist alles in Ordnung, sonst wird die Karte rot und es gibt eine Benachrichtigung. Bei einer der eingebauten Apps
ersetzt die eigene Adresse deren Prüfung; leer lassen = wieder die eingebaute. Gespeichert wird auf dem Host in
`/var/lib/ecspve-helfer/pruefungen.json`; ein Eintrag `PRUEFUNG_<ID>` in `helfer.conf` hat Vorrang.

## Installation

Alles auf dem **Proxmox-Host** als root.

**1. Projektordner holen.** Das Repository ist privat – wie bei Wiki und Website mit einem eigenen Deploy-Key:

```bash
ssh-keygen -t ed25519 -N '' -C "ecspve@$(hostname)" -f /root/.ssh/ecspve_deploy
cat /root/.ssh/ecspve_deploy.pub     # auf GitHub: ecspve → Settings → Deploy keys → Add (nur lesen)
cat >>/root/.ssh/config <<'EOF'
Host github-ecspve
  HostName github.com
  User git
  IdentityFile /root/.ssh/ecspve_deploy
  IdentitiesOnly yes
EOF
git clone github-ecspve:mmeintgens-arch/ecspve.git /opt/ecspve
```

**2. Container anlegen.** Am besten mit fester IP – dann gilt der SSH-Schlüssel nur von dieser Adresse. `--proxy-ip`
ist die IP eures Caddy (damit die Anmeldesperre die echte Besucher-IP sieht):

```bash
/opt/ecspve/pve-install.sh --ip=192.168.10.60/24 --gw=192.168.10.1 --proxy-ip=192.168.10.2
```

Das Skript fragt nach einem Passwort (leer = zufällig, gespeichert in `/root/ecspve-ct<ID>-passwort.txt`), legt einen
unprivilegierten Debian-Container mit dem Tag `ecspve` an, installiert dort die Übersicht, richtet auf dem Host den
Helfer ein, gibt den Schlüssel frei und prüft die Verbindung. Weitere Optionen: `--help`.

**3. Caddy davor.** Die Übersicht darf alles neu starten – sie gehört nicht offen ins Internet. Im Caddyfile eures
Proxys, z. B. nur aus dem Vereinsnetz:

```caddy
status.eventcrew-steinfurt.de {
	@extern not remote_ip private_ranges
	respond @extern "Nur im Vereinsnetz erreichbar" 403
	reverse_proxy 192.168.10.60:8080
}
```

Oder ohne Domain nur intern (`tls internal`). Ist `--proxy-ip` gesetzt, geht die Seite **nur** über diesen Caddy
(direkt auf Port 8080 antworten nur noch `/health` und `/ping`), und die Anmeldung nur über HTTPS.

**4. Erste Anmeldung mit zweitem Faktor.** `https://status…` öffnen, als Benutzer **admin** mit dem Passwort aus
`/root/ecspve-ct<ID>-passwort.txt` anmelden. Beim ersten Mal richtet die Seite den zweiten Faktor ein: QR-Code mit
einer Authenticator-App scannen (Microsoft Authenticator, Google Authenticator, Aegis, 2FAS …), Code eintragen, die
acht **Notfallcodes** sichern. Danach unter **Benutzer** für jede Person im Team einen eigenen Zugang anlegen.

<img src="docs/zwei-faktor.png" alt="Zweiten Faktor einrichten" width="520">

**5. Automatische Updates auswählen.** In jeder Karte bei „Nachts automatisch“ die Häkchen setzen: **System**
(Debian-Pakete im Container) und/oder **App** (der Update-Weg der App aus der Tabelle oben).

## Automatische Updates

Der Nachtlauf (`ecspve-nacht.timer`, 04:30 plus bis zu 15 Minuten – nach den Sicherungen von Website 03:20 und Wiki
03:40) macht:

1. Paketlisten in allen laufenden Containern auffrischen – damit „Updates verfügbar“ stimmt, auch ohne Häkchen.
2. Proxmox-Host aktualisieren – nur mit `HOST_AUTO_UPDATE=ja` (Standard: nein).
3. Jeden ausgewählten Container nacheinander:
   - **Snapshot** `ecspve_<Datum>` (die letzten 2 bleiben, `SNAPSHOTS_BEHALTEN`). Kann der Speicher keine Snapshots,
     läuft das Update trotzdem und der Auftrag zeigt eine Warnung.
   - **System**: `apt-get update && apt-get full-upgrade` (angepasste Konfigurationsdateien bleiben), `autoremove`.
   - **App**: der eingebaute Update-Weg der App – inklusive deren eigener Sicherung vor dem Update.
   - Danach prüfen: laufen alle Dienste, antwortet die App? Wenn nicht, steht der Auftrag auf „Warnung“, die Karte
     wird rot, und es gibt eine Benachrichtigung.
   - Verlangt ein Update einen Neustart (`/run/reboot-required`), zeigt die Karte „Neustart empfohlen“; mit
     `NEUSTART_NACH_UPDATE=ja` startet der Container gleich neu.
4. Die Übersicht selbst kommt zuletzt dran.

Zurück zu einem Snapshot: in Proxmox beim Container unter **Snapshots** → **Rollback**.

Andere Uhrzeit: im Container `systemctl edit ecspve-nacht.timer` und dort

```ini
[Timer]
OnCalendar=
OnCalendar=*-*-* 05:15:00
```

Den Proxmox-Host selbst startet die Übersicht nie neu – nach einem neuen Kernel zeigt sie „Neustart nötig“, den
Neustart löst ihr zu einer ruhigen Zeit in der Proxmox-Oberfläche aus.

## Monitoring anbinden

Unten auf der Seite stehen die fertigen Adressen und der Header mit dem Token zum Kopieren.

| Schnittstelle | Antwort | Anmeldung |
|---|---|---|
| `GET /health` | `200 OK` oder `503 FEHLER: 2 Probleme` | keine (mit Token zusätzlich die Texte) |
| `GET /api/status` | alles, was die Seite zeigt, als JSON (`ok`, `probleme`, `host`, `gaeste` …) | Token |
| `GET /metrics` | Prometheus-Format, z. B. `ecspve_ok`, `ecspve_gast_laeuft`, `ecspve_dienst_ok`, `ecspve_pruefung_ok`, `ecspve_gast_updates`, `ecspve_host_neustart_noetig` | Token |
| `GET /ping` | `ok` – nur „läuft die Übersicht selbst?“ | keine |

Das Token geht als Header `Authorization: Bearer <Token>` mit. Als `?token=<Token>` in der Adresse nur mit
`TOKEN_IN_URL=ja` – dort landet es in den Protokollen von Proxy und Monitoring. Ein falsches Token zählt als
Fehlversuch (Sperre wie bei der Anmeldung). Neu erzeugen im Container: `ecspve token --neu`.

**Uptime Kuma**

- einfach: Monitor „HTTP(s)“ auf `https://status…/health` – rot, sobald irgendetwas gestört ist.
- genauer: Monitor „HTTP(s) – Json Query“ auf `https://status…/api/status`, Ausdruck `ok`, erwarteter Wert `true`,
  unter **Headers** `{"Authorization": "Bearer <Token>"}`.
- umgekehrt (Push, falls Kuma die Übersicht nicht erreicht): Monitor „Push“ anlegen und dessen Adresse als
  `HEARTBEAT_URL=https://kuma…/api/push/AbCd?status={status}&msg={msg}` eintragen.

**Prometheus / Grafana**

```yaml
scrape_configs:
  - job_name: ecspve
    scheme: https
    authorization:
      credentials: "<Token>"
    static_configs:
      - targets: ["status.eventcrew-steinfurt.de"]
```

Beispiel-Alarm: `ecspve_dienst_ok == 0` oder `ecspve_pruefung_ok == 0` länger als 5 Minuten.

**Checkmk, Zabbix, Nagios** – HTTP-Check auf `/health` (Statuscode 200), oder Prometheus-Anbindung auf `/metrics`.

**Benachrichtigung aufs Handy (ntfy)** – `MELDUNG_URL=https://ntfy.sh/<geheimes-thema>` (oder euer eigener
ntfy-Server, ggf. mit `MELDUNG_TOKEN`). Dann kommen: Störungen (wenn sie mindestens 30 Sekunden bestehen), „wieder in
Ordnung“ und das Ergebnis jedes Nachtlaufs.

## Einstellungen

**Im Container: `/etc/ecspve/ecspve.env`** (die meisten Werte gelten nach wenigen Sekunden, `ADRESSE`/`PORT` nach
`systemctl restart ecspve`)

| Einstellung | Standard | Bedeutung |
|---|---|---|
| `PVE_HOST` | IP des Hosts | Proxmox-Host mit dem Helfer (trägt `pve-install.sh` ein) |
| `PVE_URL` | `https://PVE_HOST:8006` | Adresse der Proxmox-Oberfläche für die Links |
| `ADRESSE`, `PORT` | `0.0.0.0`, `8080` | Webserver (nur HTTP) |
| `PROXY_IP` | – | IP(s) eures Reverse Proxys. **Dringend empfohlen:** dann nur über den Proxy erreichbar, Anmeldung nur mit HTTPS, Sperren treffen die echte Besucher-IP |
| `TOKEN_IN_URL` | `nein` | Monitoring-Token auch als `?token=` in der Adresse annehmen |
| `MELDUNG_ANMELDUNG` | `ja` | bei jeder erfolgreichen Anmeldung eine Nachricht an `MELDUNG_URL` (Sperren und neue Authenticator-Apps immer) |
| `TOKEN` | zufällig | für `/api/status` und `/metrics` |
| `SNAPSHOTS_BEHALTEN` | `2` | Snapshot vor jedem Update, so viele bleiben (0 = keiner) |
| `NEUSTART_NACH_UPDATE` | `nein` | Container neu starten, wenn ein Update es verlangt |
| `HOST_AUTO_UPDATE` | `nein` | Proxmox-Host im Nachtlauf mit aktualisieren |
| `MELDUNG_URL`, `MELDUNG_TOKEN` | – | ntfy-Thema für Benachrichtigungen |
| `HEARTBEAT_URL` | – | Push-Adresse (Uptime Kuma, healthchecks.io), jede Minute |
| `HEALTH_DETAILS` | `nein` | `/health` nennt ohne Token auch die Texte der Probleme |

**Auf dem Host: `/etc/ecspve/helfer.conf`**

| Einstellung | Standard | Bedeutung |
|---|---|---|
| `STREETWORKDOC_QUELLE` | `/root/StreetworkDoc` | Projektordner von StreetworkDoc für dessen App-Update |
| `HOST_UPDATE` | `ja` | Host-Updates über die Übersicht erlauben (`nein` = nur anzeigen) |
| `PRUEFUNG_<ID>` | – | eigene Funktionsprüfung im Container: Adresse (`http://127.0.0.1:8123/`) oder Befehl – hat Vorrang vor der Einstellung in der Weboberfläche |
| `UPDATE_<ID>` | – | eigener App-Update-Befehl auf dem Host, `{id}` = Container-ID |

Die Auswahl „nachts automatisch“ und der Bericht des letzten Nachtlaufs liegen im Container in `/var/lib/ecspve`.

## Bedienung im Terminal

Im Container der Übersicht:

```bash
ecspve status          # dasselbe wie die Seite, als Liste
ecspve verbindung      # erreicht der Container den Helfer auf dem Host?
ecspve benutzer        # alle Benutzer mit Rolle, zweitem Faktor und letzter Anmeldung
ecspve benutzer anlegen NAME --rolle=bedienen   # zeigt das Startpasswort
ecspve benutzer passwort|zwei-faktor|sperren|entsperren|loeschen NAME
ecspve benutzer rolle NAME admin|bedienen|lesen
ecspve passwort        # Passwort von „admin“ neu setzen (meldet admin überall ab)
ecspve zwei-faktor zuruecksetzen NAME   # Handy weg und keine Notfallcodes mehr: neue App bei der nächsten Anmeldung
ecspve token --neu     # neues Token für Monitoring
journalctl -u ecspve -f
```

Auf dem Host:

```bash
ecspve-helfer uebersicht | python3 -m json.tool | less
ecspve-helfer pruefen    | python3 -m json.tool | less
ls -t /var/lib/ecspve-helfer/auftraege/*.log | head   # Protokolle der Aufträge
```

## Update der Übersicht

```bash
/opt/ecspve/pve-install.sh --pull
```

Holt den neuesten Stand, spielt ihn in den Container ein und erneuert den Helfer auf dem Host. Einstellungen,
Passwort, Token und Auswahl bleiben. Mit dem Häkchen **App** in der Karte der Übersicht passiert das auch nachts.

## Sicherheit

Die Seite kann Container anlegen, neu starten und Updates anstoßen. Deshalb ist sie mehrfach abgesichert:

### Benutzer

Jede Person bekommt einen eigenen Zugang – mit eigenem Passwort, eigenem zweiten Faktor und einer Rolle:

![Benutzer verwalten](docs/benutzer.png)

| Rolle | darf |
|---|---|
| **Admin** | alles, dazu Benutzer verwalten |
| **Bedienen** | Neustarts, Dienste neu starten, Updates, „Nachts automatisch“, Container anlegen, Funktionsprüfungen |
| **Nur ansehen** | alles sehen, nichts auslösen (das Root-Passwort neuer Container sieht diese Rolle nicht) |

- Ein Admin legt Benutzer unter **Benutzer → Benutzer anlegen** an (oder im Container `ecspve benutzer anlegen NAME
  --rolle=…`). Die Seite zeigt ein **Startpasswort** – einmal, zum sicheren Weitergeben. Bei der ersten Anmeldung
  ersetzt die Person es durch ein eigenes und richtet ihren eigenen zweiten Faktor ein.
- In der Tabelle: Rolle ändern (wirkt sofort), **Passwort zurücksetzen** (neues Startpasswort), **Zweiten Faktor
  zurücksetzen** (Handy verloren), **Sperren** (meldet sofort überall ab), **Löschen**.
- Benutzer verwalten geht nur mit frisch bestätigtem Code des Admins (gilt dann 10 Minuten) – ein offen gelassener
  Browser reicht dafür nicht. Den eigenen Zugang kann man dort nicht herabstufen oder sperren, und es bleibt immer
  mindestens ein aktiver Admin.
- Admins sehen im Sicherheitsprotokoll alle Einträge, alle anderen nur ihre eigenen. Jede Aktion steht mit Namen drin.
- Der bisherige einzelne Zugang (vor der Benutzerverwaltung) wird beim ersten Start automatisch zum Benutzer
  **admin** – mit demselben Passwort und derselben Authenticator-App.

### Anmeldung mit zweitem Faktor

- Benutzername, Passwort (mindestens 12 Zeichen, PBKDF2 mit 600 000 Runden) **und** ein Code aus einer
  Authenticator-App (TOTP nach RFC 6238, 6 Ziffern, 30 Sekunden). Ohne Code gibt es keine Daten und keine Aktionen.
  Ob es einen Benutzernamen gibt, verrät die Anmeldung nicht.
- Jeder Code gilt bei der Anmeldung nur **einmal** – wer ihn mitliest, kann ihn nicht wiederverwenden.
- Acht **Notfallcodes** für den Fall, dass das Handy weg ist – jeder nur einmal, gespeichert nur als Hash. Neue unter
  **Sicherheit → Neue Notfallcodes**. Ist alles weg: ein Admin setzt den zweiten Faktor zurück (oder im Container
  `ecspve zwei-faktor zuruecksetzen NAME`).
- **Neues Handy einrichten** (nach Verlust) meldet alle anderen Handys und Sitzungen dieses Benutzers ab. Ein neues
  Passwort (**Sicherheit → Passwort ändern**) ebenso.
- Passwort-Hashes und App-Geheimnisse liegen nur im Container (`/var/lib/ecspve/benutzer.json`, nur für den Dienst
  lesbar).
- Bis zur ersten Anmeldung ist der zweite Faktor noch nicht eingerichtet – wer bis dahin das Passwort kennt, könnte
  seine eigene App eintragen. Deshalb gleich nach der Installation anmelden und einrichten.

### Gegen Durchprobieren
- 5 Fehlversuche (Passwort, Code oder Monitoring-Token) von einer Adresse: diese Adresse 15 Minuten gesperrt.
  30 Fehlversuche von verschiedenen Adressen: Anmeldung für alle 15 Minuten gesperrt. Jeder Fehlversuch kostet
  zusätzlich eine Sekunde.
- 10 falsche Codes nach **richtigem** Passwort innerhalb einer Stunde (egal von wo): Dann kennt offenbar jemand das
  Passwort – die Anmeldung ist für eine Stunde gesperrt, und es kommt die Aufforderung, es zu ändern
  (`ecspve passwort`).
- Jede Sperre geht als Benachrichtigung an `MELDUNG_URL`, auf Wunsch auch jede erfolgreiche Anmeldung.

### Sitzungen
- Nach 30 Minuten ohne Seite offen oder spätestens nach 12 Stunden neu anmelden. Nach dem zweiten Faktor gibt es ein
  neues Sitzungs-Token. Nur im Speicher – ein Neustart des Dienstes meldet alle ab.
- **Alle anderen abmelden** unter **Sicherheit**, z. B. wenn ein Rechner verloren ging.
- Cookies sind `HttpOnly`, `SameSite=Strict` und hinter HTTPS `Secure`.

### Zugang und Browser
- Mit `PROXY_IP`: nur über euren Caddy erreichbar, Anmeldung nur über HTTPS. Ohne `PROXY_IP` warnt die Seite.
  Zusätzlich sinnvoll: im Caddy nur das Vereinsnetz oder VPN zulassen (Beispiel oben).
- Aktionen brauchen einen eigenen Header und eine passende Herkunft (`Origin`) – ein fremdes Formular oder eine fremde
  Seite kann nichts auslösen.
- Strenge Content-Security-Policy (nur eigene Skripte, keine eingebetteten), kein Einbetten in fremde Seiten,
  `nosniff`, HSTS hinter HTTPS. Langsame oder hängende Verbindungen werden nach 30 Sekunden getrennt.

### Nachvollziehbar
- Unter **Sicherheit → Letzte Ereignisse** und in `/var/lib/ecspve/sicherheit.jsonl`: Anmeldungen, Fehlversuche,
  Sperren, Änderungen am zweiten Faktor und jede Aktion (Neustart, Update, Container anlegen …) – mit IP.
  Auch im Journal: `journalctl -u ecspve | grep Sicherheit`.

### Host

- Was der Container auf dem Host darf, steht vollständig in `deploy/proxmox/ecspve-helfer` – nichts darüber hinaus,
  keine Shell. Freigabe zurücknehmen: die Zeile mit `ecspve-ct<ID>` aus `/root/.ssh/authorized_keys` löschen.
- Das Monitoring-Token erlaubt nur Lesen.

## Entwicklung und Tests

Ohne Proxmox, gegen einen nachgebauten Host (`test/fake.py` ersetzt `pct`, `pvesh`, `systemctl` … und führt das
echte Prüfskript aus):

```bash
python3 -m unittest discover -s test -v       # Tests (Python 3.11 wie auf Proxmox VE 8, und neuer)
python3 test/fake.py /tmp/ecspve-demo --demo  # Vorschau auf http://127.0.0.1:8080, Passwort: demo-passwort
```

| Datei | Wo sie läuft | Was |
|---|---|---|
| `ecspve.py` | Container | Weboberfläche, Monitoring, Benachrichtigungen, Nachtlauf, Kommandozeile |
| `web/` | Browser | die Seite (HTML, CSS, JavaScript ohne Bibliotheken) |
| `deploy/proxmox/ecspve-helfer` | Proxmox-Host | die erlaubten Befehle, Prüfskript, Aufträge |
| `pve-install.sh` | Proxmox-Host | Container anlegen/aktualisieren, Helfer und Schlüssel einrichten |
| `deploy/install.sh` | Container | Installation im Container (ruft `pve-install.sh` auf) |
