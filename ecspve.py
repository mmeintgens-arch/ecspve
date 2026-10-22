#!/usr/bin/env python3
"""ecspve – Übersicht über Container und Dienste auf dem Proxmox-Host.

Läuft in einem eigenen Container und spricht nur HTTP (HTTPS macht der Reverse Proxy davor). Alles, was den
Host betrifft, erledigt ecspve-helfer auf dem Proxmox-Host – per SSH mit einem Schlüssel, der nur diesen
Helfer aufrufen darf.

  ecspve serve           Weboberfläche (systemd: ecspve.service)
  ecspve nacht           nächtliche Updates (systemd: ecspve-nacht.timer)
  ecspve status          Übersicht im Terminal
  ecspve passwort        Passwort der Weboberfläche setzen (--stdin: aus der Eingabe lesen)
  ecspve token [--neu]   Token für Monitoring-Abfragen anzeigen bzw. neu erzeugen
  ecspve ping            prüft, ob die Weboberfläche antwortet
  ecspve verbindung      prüft die Verbindung zum Helfer auf dem Proxmox-Host
  ecspve zwei-faktor [zuruecksetzen]
                         Stand des zweiten Faktors; zuruecksetzen = Handy verloren und keine Notfallcodes mehr:
                         bei der nächsten Anmeldung wird eine neue Authenticator-App eingerichtet
"""

import base64
import email.header
import getpass
import grp
import hashlib
import hmac
import http.server
import json
import os
import pwd
import re
import secrets
import shlex
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "1.0"
WEB = Path(__file__).resolve().parent / "web"
# Die Umgebungsvariablen gibt es für Tests und Entwicklung, im Betrieb gelten die Standardpfade.
CONF_DIR = Path(os.environ.get("ECSPVE_CONF_DIR", "/etc/ecspve"))
STATE_DIR = Path(os.environ.get("ECSPVE_STATE_DIR", "/var/lib/ecspve"))
CONF_FILE = CONF_DIR / "ecspve.env"
PASS_FILE = CONF_DIR / "passwort"
SSH_DIR = CONF_DIR / "ssh"
EINST_FILE = STATE_DIR / "gaeste.json"
NACHT_FILE = STATE_DIR / "nacht.json"
ZF_FILE = STATE_DIR / "zwei-faktor.json"
SICHERHEIT_FILE = STATE_DIR / "sicherheit.jsonl"

VORGABEN = {
    "PVE_HOST": "", "PVE_URL": "", "ADRESSE": "0.0.0.0", "PORT": "8080", "TOKEN": "", "PROXY_IP": "",
    "SNAPSHOTS_BEHALTEN": "2", "NEUSTART_NACH_UPDATE": "nein", "HOST_AUTO_UPDATE": "nein",
    "MELDUNG_URL": "", "MELDUNG_TOKEN": "", "HEARTBEAT_URL": "", "HEALTH_DETAILS": "nein",
    "MELDUNG_ANMELDUNG": "ja", "TOKEN_IN_URL": "nein",
}
SITZUNG_LEERLAUF = 30 * 60      # so lange ohne Anfrage gültig – die offene Seite fragt alle 10 Sekunden
SITZUNG_HOECHSTENS = 12 * 3600  # danach immer neu anmelden
VORSTUFE_DAUER = 5 * 60         # Passwort stimmt, Code fehlt noch
SPERRE_FENSTER = 15 * 60
SPERRE_IP = 5                   # Fehlversuche je IP im Fenster
SPERRE_GESAMT = 30              # Fehlversuche aller IPs zusammen im Fenster
SPERRE_CODES = 10               # falsche Codes nach richtigem Passwort je Stunde – dann ist das Passwort wohl bekannt
PASSWORT_MIN = 12
PBKDF2_RUNDEN = 600_000
NOTFALL_ZEICHEN = "abcdefghjkmnpqrstuvwxyz23456789"
AUFTRAG_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
DIENST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:-]{0,120}\.service$")


def log(text):
    print(text, file=sys.stderr, flush=True)


def ja(wert):
    return str(wert).strip().lower() in ("ja", "1", "true", "yes", "an", "on")


_konf_cache = (0.0, None)


def lade_konfig():
    global _konf_cache
    if _konf_cache[1] is not None and time.time() - _konf_cache[0] < 5:
        return _konf_cache[1]
    werte = dict(VORGABEN)
    try:
        zeilen = CONF_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        zeilen = []
    for zeile in zeilen:
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#") or "=" not in zeile:
            continue
        k, v = zeile.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        werte[k.strip()] = v
    if not werte["PVE_URL"] and werte["PVE_HOST"]:
        werte["PVE_URL"] = f"https://{werte['PVE_HOST']}:8006"
    _konf_cache = (time.time(), werte)
    return werte


def zahl(text, vorgabe, kleinst=0, groesst=9):
    try:
        return min(max(int(text), kleinst), groesst)
    except (TypeError, ValueError):
        return vorgabe


def schreibe_json(pfad, daten):
    pfad.parent.mkdir(parents=True, exist_ok=True)
    tmp = pfad.with_suffix(".tmp")
    tmp.write_text(json.dumps(daten, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(pfad)


def lies_json(pfad, vorgabe):
    try:
        return json.loads(pfad.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return vorgabe


# --- Verbindung zum Helfer auf dem Host ----------------------------------------------------------------

class HelferFehler(Exception):
    pass


def helfer(*args, timeout=60):
    """Ruft ecspve-helfer auf dem Proxmox-Host auf und gibt dessen JSON-Antwort zurück."""
    konf = lade_konfig()
    befehl = [str(a) for a in args]
    lokal = os.environ.get("ECSPVE_HELFER")  # nur für Tests und Entwicklung: Helfer direkt aufrufen
    if lokal:
        cmd = [sys.executable, lokal, *befehl]
    else:
        if not konf["PVE_HOST"]:
            raise HelferFehler(f"PVE_HOST ist nicht gesetzt ({CONF_FILE}).")
        cmd = ["ssh", "-i", str(SSH_DIR / "id_ed25519"), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
               "-o", f"UserKnownHostsFile={SSH_DIR / 'known_hosts'}", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ControlMaster=auto",
               "-o", f"ControlPath={STATE_DIR}/ssh-%C", "-o", "ControlPersist=600",
               f"root@{konf['PVE_HOST']}", shlex.join(befehl)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise HelferFehler("Der Proxmox-Host antwortet nicht (Zeitüberschreitung).")
    except OSError as e:
        raise HelferFehler(f"Helfer nicht aufrufbar: {e}")
    try:
        daten = json.loads(p.stdout)
    except ValueError:
        meldung = (p.stderr.strip().splitlines() or [f"Code {p.returncode}"])[-1]
        raise HelferFehler(f"Keine Antwort vom Proxmox-Host: {meldung[:200]}")
    if isinstance(daten, dict) and daten.get("fehler"):
        raise HelferFehler(daten["fehler"])
    return daten


def update_optionen(konf):
    opts = [f"--snapshots={zahl(konf['SNAPSHOTS_BEHALTEN'], 2)}"]
    if ja(konf["NEUSTART_NACH_UPDATE"]):
        opts.append("--neustart")
    return opts


# --- Benachrichtigungen ----------------------------------------------------------------------------------

def melden(titel, text, wichtig=False):
    """Schickt eine Nachricht an MELDUNG_URL (ntfy-kompatibel: Text im Body, Titel im Header)."""
    konf = lade_konfig()
    if not konf["MELDUNG_URL"]:
        return
    kopf = {"Title": email.header.Header(titel, "utf-8").encode(), "Priority": "high" if wichtig else "default",
            "Tags": "warning" if wichtig else "white_check_mark", "Content-Type": "text/plain; charset=utf-8"}
    if konf["MELDUNG_TOKEN"]:
        kopf["Authorization"] = f"Bearer {konf['MELDUNG_TOKEN']}"
    try:
        req = urllib.request.Request(konf["MELDUNG_URL"], data=text.encode("utf-8"), headers=kopf, method="POST")
        urllib.request.urlopen(req, timeout=15).close()
    except Exception as e:  # noqa: BLE001 – eine Meldung darf die Übersicht nie stören
        log(f"Meldung nicht zugestellt: {e}")


def herzschlag(st):
    """Meldet den Zustand an HEARTBEAT_URL (Uptime Kuma Push, healthchecks.io …)."""
    url = lade_konfig()["HEARTBEAT_URL"]
    if not url:
        return
    if "{status}" in url or "{msg}" in url:
        text = "OK" if st["ok"] else f"{len(st['probleme'])} Probleme: " + "; ".join(p["text"] for p in st["probleme"])
        url = url.replace("{status}", "up" if st["ok"] else "down").replace("{msg}", urllib.parse.quote(text[:200]))
    elif not st["ok"]:
        return  # ohne Platzhalter: nur melden, wenn alles in Ordnung ist – Ausbleiben = Alarm
    try:
        urllib.request.urlopen(url, timeout=15).close()
    except Exception as e:  # noqa: BLE001
        log(f"Heartbeat nicht zugestellt: {e}")


# --- Zustand zusammenstellen -----------------------------------------------------------------------------

def lade_einstellungen():
    daten = lies_json(EINST_FILE, {})
    return daten if isinstance(daten, dict) else {}


_timer_cache = [0.0, None]


def naechster_nachtlauf():
    if time.time() - _timer_cache[0] < 300:
        return _timer_cache[1]
    wert = None
    try:
        p = subprocess.run(["systemctl", "show", "ecspve-nacht.timer", "--property=NextElapseUSecRealtime",
                            "--timestamp=unix", "--value"], capture_output=True, text=True, timeout=10)
        roh = p.stdout.strip()
        wert = float(roh[1:]) if roh.startswith("@") else (roh or None)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    _timer_cache[:] = [time.time(), wert]
    return wert


def baue_status(ueb, ueb_fehler, proben, proben_zeit):
    """Fasst Übersicht und Prüfungen zu dem zusammen, was Oberfläche, Monitoring und Meldungen brauchen."""
    einst = lade_einstellungen()
    probleme, hinweise = [], []
    if ueb_fehler:
        probleme.append({"schluessel": "verbindung", "text": f"Keine Verbindung zum Proxmox-Host: {ueb_fehler}"})
    ueb = ueb or {}
    host = dict(ueb.get("host") or {})
    for name in host.get("dienste_fehler") or []:
        probleme.append({"schluessel": f"host:{name}", "text": f"Host: Dienst {name} ist ausgefallen"})
    if host.get("updates"):
        hinweise.append(f"Host: {host['updates']} Updates verfügbar")
    if host.get("kernel_neustart"):
        hinweise.append("Host: neuer Kernel installiert – Neustart nötig")
    auftraege = ueb.get("auftraege") or []
    laufend = {j["gast"]: j for j in reversed(auftraege) if j.get("status") in ("wartet", "laeuft")}
    gaeste = []
    for g in ueb.get("gaeste") or []:
        if g.get("vorlage"):
            continue
        g = dict(g)
        gid = g["id"]
        pr = proben.get(str(gid)) if g["typ"] == "lxc" and g["status"] == "running" and g["lokal"] else None
        g["probe"] = pr
        g["auto"] = {"system": bool(einst.get(str(gid), {}).get("system")),
                     "app": bool(einst.get(str(gid), {}).get("app"))}
        g["auftrag"] = laufend.get(gid)
        fehler = []
        if g["status"] != "running":
            if g.get("onboot"):
                fehler.append(("aus", "läuft nicht, obwohl Autostart an ist"))
        elif pr and not g["auftrag"]:
            if pr.get("fehler"):
                fehler.append(("probe", f"keine Prüfung möglich: {pr['fehler']}"))
            for d in pr.get("dienste", []):
                if d["zustand"] == "fehler":
                    wie = "ist ausgefallen" if d["text"].startswith("failed") else f"läuft nicht ({d['text']})"
                    fehler.append((f"dienst:{d['name']}", f"Dienst {d['name']} {wie}"))
            for p in pr.get("pruefungen", []):
                if not p["ok"]:
                    fehler.append((f"pruefung:{p['app']}", f"Funktionsprüfung fehlgeschlagen ({p['detail']})"))
        g["probleme"] = [t for _, t in fehler]
        for schluessel, text in fehler:
            probleme.append({"schluessel": f"{gid}:{schluessel}", "text": f"{g['name']} ({gid}): {text}", "gast": gid})
        gaeste.append(g)
    updates_gaeste = sum((g["probe"] or {}).get("updates") or 0 for g in gaeste)
    if updates_gaeste:
        hinweise.append(f"Container: {updates_gaeste} Updates verfügbar")
    konf = lade_konfig()
    return {
        "version": VERSION, "zeit": time.time(), "ok": not probleme, "probleme": probleme, "hinweise": hinweise,
        "verbunden": not ueb_fehler and bool(ueb), "stand": ueb.get("zeit"), "proben_stand": proben_zeit,
        "host": host, "gaeste": gaeste, "auftraege": auftraege[:15],
        "nacht": {**lies_json(NACHT_FILE, {}), "naechster": naechster_nachtlauf(),
                  "host_auto": ja(konf["HOST_AUTO_UPDATE"])},
        "einstellungen": {"snapshots": zahl(konf["SNAPSHOTS_BEHALTEN"], 2),
                          "neustart": ja(konf["NEUSTART_NACH_UPDATE"])},
    }


class Sammler:
    """Fragt den Helfer regelmäßig ab: Übersicht alle 10 Sekunden, Prüfung der Container jede Minute."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ueb, self.ueb_fehler = None, "noch keine Antwort"
        self.proben, self.proben_zeit = {}, None
        self.wecker = threading.Event()
        self.pruef_wecker = threading.Event()
        self.zaehler, self.gemeldet = {}, {}
        self.laufende = set()
        self.letzter_herzschlag = 0.0
        self.letzter_besuch = 0.0

    def beobachtet(self):
        return time.time() - self.letzter_besuch < 120

    def besuch(self):
        """Jemand schaut zu: öfter abfragen. Nach einer Pause sofort frische Daten holen."""
        if not self.beobachtet():
            self.wecker.set()
            self.pruef_wecker.set()
        self.letzter_besuch = time.time()

    def start(self):
        threading.Thread(target=self.schleife_uebersicht, daemon=True).start()
        threading.Thread(target=self.schleife_pruefen, daemon=True).start()

    def status(self):
        with self.lock:
            werte = (self.ueb, self.ueb_fehler, self.proben, self.proben_zeit)
        return baue_status(*werte)

    def auftrag_eintragen(self, job):
        """Gerade gestarteten Auftrag sofort zeigen – nicht erst nach der nächsten Abfrage."""
        with self.lock:
            if self.ueb and isinstance(job, dict) and job.get("id") and job.get("art"):
                self.ueb = {**self.ueb, "auftraege": [job, *self.ueb.get("auftraege", [])]}

    def schleife_uebersicht(self):
        while True:
            try:
                daten = helfer("uebersicht", timeout=60)
                with self.lock:
                    self.ueb, self.ueb_fehler = daten, ""
            except HelferFehler as e:
                with self.lock:
                    self.ueb_fehler = str(e)
            try:
                schnell = self.nach_abfrage()
            except Exception as e:  # noqa: BLE001 – die Schleife muss weiterlaufen
                log(f"Fehler nach der Abfrage: {e!r}")
                schnell = False
            # pvesh und pct sind auf dem Host nicht ganz billig: ohne Zuschauer seltener fragen
            self.wecker.wait(3 if schnell else 10 if self.beobachtet() else 30)
            self.wecker.clear()

    def schleife_pruefen(self):
        while True:
            try:
                daten = helfer("pruefen", timeout=150)
                with self.lock:
                    self.proben, self.proben_zeit = daten.get("gaeste", {}), daten.get("zeit")
            except HelferFehler as e:
                log(f"Prüfung nicht möglich: {e}")
            self.pruef_wecker.wait(60 if self.beobachtet() else 120)
            self.pruef_wecker.clear()

    def nach_abfrage(self):
        """Fertige Aufträge erkennen, Meldungen verschicken, Heartbeat. True = bald wieder abfragen."""
        st = self.status()
        laufende = {j["id"] for j in st["auftraege"] if j.get("status") in ("wartet", "laeuft")}
        if self.laufende - laufende:
            self.pruef_wecker.set()  # ein Auftrag ist fertig: Container gleich neu prüfen
        self.laufende = laufende

        aktuell = {p["schluessel"]: p["text"] for p in st["probleme"]}
        self.zaehler = {k: self.zaehler.get(k, 0) + 1 for k in aktuell}
        # Erst melden, wenn ein Problem drei Abfragen hintereinander besteht – kurze Aussetzer nicht
        neu = {k: aktuell[k] for k, n in self.zaehler.items() if n >= 3 and k not in self.gemeldet}
        behoben = {k: t for k, t in self.gemeldet.items() if k not in aktuell}
        if neu:
            self.gemeldet.update(neu)
            threading.Thread(target=melden, args=("Störung auf dem Proxmox-Host", "\n".join(neu.values()), True),
                             daemon=True).start()
        if behoben:
            for k in behoben:
                del self.gemeldet[k]
            threading.Thread(target=melden, args=("Wieder in Ordnung", "\n".join(behoben.values())),
                             daemon=True).start()
        if time.time() - self.letzter_herzschlag >= 60:
            self.letzter_herzschlag = time.time()
            threading.Thread(target=herzschlag, args=(st,), daemon=True).start()
        return bool(laufende)


# --- Anmeldung: Passwort und zweiter Faktor --------------------------------------------------------------

def passwort_hash(passwort):
    salz = secrets.token_bytes(16)
    wert = hashlib.pbkdf2_hmac("sha256", passwort.encode(), salz, PBKDF2_RUNDEN)
    return f"pbkdf2_sha256${PBKDF2_RUNDEN}${base64.b64encode(salz).decode()}${base64.b64encode(wert).decode()}"


def passwort_pruefen(passwort):
    try:
        art, runden, salz, wert = PASS_FILE.read_text(encoding="utf-8").strip().split("$")
    except (OSError, ValueError):
        return False
    if art != "pbkdf2_sha256":
        return False
    probe = hashlib.pbkdf2_hmac("sha256", passwort.encode(), base64.b64decode(salz), int(runden))
    return hmac.compare_digest(probe, base64.b64decode(wert))


def totp(geheim, schritt):
    """Sechsstelliger Code nach RFC 6238 (SHA-1, 30 Sekunden) – wie in jeder Authenticator-App."""
    schluessel = base64.b32decode(geheim + "=" * (-len(geheim) % 8))
    digest = hmac.new(schluessel, struct.pack(">Q", schritt), hashlib.sha1).digest()
    versatz = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[versatz:versatz + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_schritt(geheim, code, jetzt=None):
    """Zeitschritt, zu dem der Code passt (eine halbe Minute Uhrabweichung erlaubt), sonst None."""
    basis = int((time.time() if jetzt is None else jetzt) // 30)
    treffer = None
    for schritt in (basis - 1, basis, basis + 1):
        if hmac.compare_digest(totp(geheim, schritt), code):
            treffer = schritt
    return treffer


def neues_geheimnis():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def notfall_hash(code):
    return hashlib.sha256(f"ecspve-notfall:{code}".encode()).hexdigest()


def neue_notfallcodes(anzahl=8):
    codes = ["".join(secrets.choice(NOTFALL_ZEICHEN) for _ in range(10)) for _ in range(anzahl)]
    return [f"{c[:5]}-{c[5:]}" for c in codes], [notfall_hash(c) for c in codes]


ZF_LOCK = threading.Lock()


def zf_laden():
    daten = lies_json(ZF_FILE, None)
    return daten if isinstance(daten, dict) and daten.get("geheim") else None


def zf_speichern(daten):
    ZF_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ZF_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(daten), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(ZF_FILE)


def code_pruefen(code, anmeldung=False):
    """Prüft einen Code aus der App oder einen Notfallcode und gibt "app", "notfall" oder None zurück.

    Notfallcodes gelten nur einmal. Bei der Anmeldung gilt auch ein App-Code nur einmal – wer ihn mitliest,
    kann ihn nicht noch einmal benutzen."""
    code = re.sub(r"[\s-]", "", str(code or "")).lower()
    with ZF_LOCK:
        daten = zf_laden()
        if not daten:
            return None
        if re.fullmatch(r"[0-9]{6}", code):
            schritt = totp_schritt(daten["geheim"], code)
            if schritt is None or (anmeldung and schritt <= daten.get("letzter_schritt", 0)):
                return None
            if anmeldung:
                daten["letzter_schritt"] = schritt
                zf_speichern(daten)
            return "app"
        if re.fullmatch(f"[{NOTFALL_ZEICHEN}]{{10}}", code):
            gesucht = notfall_hash(code)
            for i, gespeichert in enumerate(daten.get("notfall", [])):
                if hmac.compare_digest(gespeichert, gesucht):
                    del daten["notfall"][i]
                    zf_speichern(daten)
                    return "notfall"
        return None


def anmeldestand():
    """Ändert sich, wenn Passwort oder zweiter Faktor neu gesetzt werden – ältere Sitzungen gelten dann nicht mehr."""
    try:
        passwort = PASS_FILE.stat().st_mtime_ns
    except OSError:
        passwort = None
    zf = zf_laden()
    return passwort, zf.get("seit") if zf else None


def qr_bild(text):
    """QR-Code als SVG (Debian-Paket qrencode) – ohne das Paket zeigt die Seite nur den Schlüssel."""
    try:
        p = subprocess.run(["qrencode", "-t", "SVG", "-l", "M", "-m", "2", "-o", "-", text],
                           capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0 or b"<svg" not in p.stdout:
        return None
    return "data:image/svg+xml;base64," + base64.b64encode(p.stdout).decode()


def einrichtung(geheim):
    aussteller = urllib.parse.quote("ECS Proxmox")
    uri = (f"otpauth://totp/{aussteller}:{urllib.parse.quote('Übersicht')}?secret={geheim}&issuer={aussteller}"
           "&digits=6&period=30")
    return {"geheim": " ".join(geheim[i:i + 4] for i in range(0, len(geheim), 4)), "uri": uri, "qr": qr_bild(uri)}


EREIGNIS_LOCK = threading.Lock()


def ereignis(ip, text, ok=True):
    """Sicherheitsprotokoll: Anmeldungen, Fehlversuche, Sperren, Änderungen und Aktionen – mit IP."""
    log(f"{'' if ok else 'WARNUNG '}Sicherheit: {text} ({ip})")
    eintrag = json.dumps({"zeit": time.time(), "ip": ip, "text": text, "ok": ok}, ensure_ascii=False)
    with EREIGNIS_LOCK:
        try:
            SICHERHEIT_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(SICHERHEIT_FILE, "a", encoding="utf-8") as f:
                f.write(eintrag + "\n")
            if os.geteuid() == 0:  # root auf der Kommandozeile: Datei bleibt beim Dienst
                try:
                    nutzer = pwd.getpwnam("ecspve")
                    os.chown(SICHERHEIT_FILE, nutzer.pw_uid, nutzer.pw_gid)
                except (KeyError, OSError):
                    pass
            if SICHERHEIT_FILE.stat().st_size > 300_000:
                zeilen = SICHERHEIT_FILE.read_text(encoding="utf-8").splitlines()[-1000:]
                SICHERHEIT_FILE.write_text("\n".join(zeilen) + "\n", encoding="utf-8")
        except OSError as e:
            log(f"Sicherheitsprotokoll nicht schreibbar: {e}")


def letzte_ereignisse(anzahl=15):
    try:
        zeilen = SICHERHEIT_FILE.read_text(encoding="utf-8").splitlines()[-anzahl:]
    except OSError:
        return []
    liste = []
    for zeile in reversed(zeilen):
        try:
            liste.append(json.loads(zeile))
        except ValueError:
            pass
    return liste


class Anmeldung:
    """Sitzungen (nur im Speicher – ein Neustart meldet alle ab) und Sperren nach Fehlversuchen."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sitzungen = {}
        self.fehlversuche = {}
        self.falsche_codes = []

    def neu(self, stufe, ip):
        token = secrets.token_urlsafe(32)
        jetzt = time.time()
        stand = anmeldestand()
        with self.lock:
            self.sitzungen = {t: s for t, s in self.sitzungen.items() if self._gueltig(s, jetzt, stand)}
            self.sitzungen[token] = {"stufe": stufe, "start": jetzt, "aktiv": jetzt, "ip": ip, "stand": stand}
        return token

    @staticmethod
    def _gueltig(s, jetzt, stand):
        dauer = SITZUNG_HOECHSTENS if s["stufe"] == "voll" else VORSTUFE_DAUER
        return jetzt - s["start"] < dauer and jetzt - s["aktiv"] < SITZUNG_LEERLAUF and s["stand"] == stand

    def hole(self, token):
        """Die Sitzung zum Token – None, wenn abgelaufen oder Passwort/zweiter Faktor inzwischen geändert."""
        if not token:
            return None
        stand = anmeldestand()
        with self.lock:
            s = self.sitzungen.get(token)
            if not s:
                return None
            jetzt = time.time()
            if not self._gueltig(s, jetzt, stand):
                del self.sitzungen[token]
                return None
            s["aktiv"] = jetzt
            return s

    def ende(self, token):
        with self.lock:
            self.sitzungen.pop(token or "", None)

    def alle_beenden(self, ausser=None):
        with self.lock:
            self.sitzungen = {t: s for t, s in self.sitzungen.items() if t == ausser}

    def angemeldet(self):
        jetzt, stand = time.time(), anmeldestand()
        with self.lock:
            return sum(1 for s in self.sitzungen.values() if s["stufe"] == "voll" and self._gueltig(s, jetzt, stand))

    def gesperrt(self, ip):
        """"ip", wenn diese IP zu oft falsch lag, "gesamt" bei vielen Fehlversuchen von überall, sonst None."""
        grenze = time.time() - SPERRE_FENSTER
        with self.lock:
            self.fehlversuche = {k: [t for t in v if t > grenze] for k, v in self.fehlversuche.items()}
            self.fehlversuche = {k: v for k, v in self.fehlversuche.items() if v}
            if len(self.fehlversuche.get(ip, [])) >= SPERRE_IP:
                return "ip"
            if sum(len(v) for v in self.fehlversuche.values()) >= SPERRE_GESAMT:
                return "gesamt"
            return None

    def fehlversuch(self, ip):
        """Zählt einen Fehlversuch; True, wenn damit gerade eine Sperre beginnt."""
        vorher = self.gesperrt(ip)
        with self.lock:
            self.fehlversuche.setdefault(ip, []).append(time.time())
        return vorher is None and self.gesperrt(ip) is not None

    def erfolg(self, ip):
        with self.lock:
            self.fehlversuche.pop(ip, None)

    def codes_gesperrt(self):
        """Zu viele falsche Codes nach richtigem Passwort (egal von wo): Code-Schritt für eine Stunde zu."""
        with self.lock:
            self.falsche_codes = [t for t in self.falsche_codes if t > time.time() - 3600]
            return len(self.falsche_codes) >= SPERRE_CODES

    def falscher_code(self):
        """Zählt einen falschen Code nach richtigem Passwort; True, wenn damit die Sperre beginnt."""
        vorher = self.codes_gesperrt()
        with self.lock:
            self.falsche_codes.append(time.time())
        return not vorher and self.codes_gesperrt()


# --- Monitoring ------------------------------------------------------------------------------------------

def metriken(st):
    zeilen = []

    def esc(wert):
        return str(wert).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    def metrik(name, hilfe, werte):
        zeilen.append(f"# HELP ecspve_{name} {hilfe}")
        zeilen.append(f"# TYPE ecspve_{name} gauge")
        for labels, wert in werte:
            if wert is None:
                continue
            wert = float(wert)
            text = str(int(wert)) if wert.is_integer() and abs(wert) < 1e15 else repr(wert)
            lab = ",".join(f'{k}="{esc(v)}"' for k, v in labels.items())
            zeilen.append(f"ecspve_{name}{{{lab}}} {text}" if lab else f"ecspve_{name} {text}")

    metrik("ok", "1, wenn es keine Probleme gibt", [({}, st["ok"])])
    metrik("probleme", "Anzahl der Probleme", [({}, len(st["probleme"]))])
    metrik("verbunden", "1, wenn der Proxmox-Host antwortet", [({}, st["verbunden"])])
    h = st["host"]
    if h:
        lab = {"knoten": h.get("name", "")}
        metrik("host_cpu_anteil", "CPU-Auslastung des Hosts (0–1)", [(lab, h.get("cpu"))])
        metrik("host_speicher_bytes", "belegter Arbeitsspeicher des Hosts", [(lab, h.get("mem"))])
        metrik("host_speicher_max_bytes", "Arbeitsspeicher des Hosts", [(lab, h.get("maxmem"))])
        metrik("host_disk_bytes", "belegter Platz auf dem Root-Dateisystem des Hosts", [(lab, h.get("disk"))])
        metrik("host_disk_max_bytes", "Größe des Root-Dateisystems des Hosts", [(lab, h.get("maxdisk"))])
        metrik("host_uptime_sekunden", "Laufzeit des Hosts", [(lab, h.get("uptime"))])
        metrik("host_updates", "verfügbare Paket-Updates auf dem Host", [(lab, h.get("updates"))])
        metrik("host_neustart_noetig", "1, wenn ein neuer Kernel auf den Neustart wartet",
               [(lab, h.get("kernel_neustart"))])
        metrik("host_dienste_fehler", "ausgefallene Dienste auf dem Host", [(lab, len(h.get("dienste_fehler") or []))])
    gl = [({"id": g["id"], "name": g["name"], "typ": g["typ"]}, g) for g in st["gaeste"]]
    metrik("gast_laeuft", "1, wenn der Gast läuft", [(lab, g["status"] == "running") for lab, g in gl])
    metrik("gast_cpu_anteil", "CPU-Auslastung des Gasts (0–1)", [(lab, g["cpu"]) for lab, g in gl])
    metrik("gast_speicher_bytes", "belegter Arbeitsspeicher", [(lab, g["mem"]) for lab, g in gl])
    metrik("gast_speicher_max_bytes", "zugeteilter Arbeitsspeicher", [(lab, g["maxmem"]) for lab, g in gl])
    metrik("gast_disk_bytes", "belegter Platz", [(lab, g["disk"]) for lab, g in gl if g["maxdisk"]])
    metrik("gast_disk_max_bytes", "zugeteilter Platz", [(lab, g["maxdisk"]) for lab, g in gl if g["maxdisk"]])
    metrik("gast_uptime_sekunden", "Laufzeit des Gasts", [(lab, g["uptime"]) for lab, g in gl])
    metrik("gast_probleme", "Anzahl der Probleme des Gasts", [(lab, len(g["probleme"])) for lab, g in gl])
    metrik("gast_updates", "verfügbare Paket-Updates im Container",
           [(lab, (g["probe"] or {}).get("updates")) for lab, g in gl if g["probe"]])
    metrik("gast_neustart_noetig", "1, wenn der Container nach Updates neu starten sollte",
           [(lab, g["probe"].get("neustart")) for lab, g in gl if g["probe"]])
    dienste, pruef_ok, pruef_zeit = [], [], []
    for lab, g in gl:
        for d in (g["probe"] or {}).get("dienste", []):
            dienste.append(({**lab, "dienst": d["name"]}, d["zustand"] != "fehler"))
        for p in (g["probe"] or {}).get("pruefungen", []):
            pruef_ok.append(({**lab, "app": p["app"]}, p["ok"]))
            if p.get("ms") is not None:
                pruef_zeit.append(({**lab, "app": p["app"]}, p["ms"] / 1000))
    metrik("dienst_ok", "1, wenn der Dienst läuft oder planmäßig ruht", dienste)
    metrik("pruefung_ok", "1, wenn die Funktionsprüfung der App klappt", pruef_ok)
    metrik("pruefung_sekunden", "Antwortzeit der Funktionsprüfung", pruef_zeit)
    nacht = st["nacht"]
    if nacht.get("ende"):
        metrik("nachtlauf_zeitpunkt", "Ende des letzten Nachtlaufs (Unix-Zeit)", [({}, nacht["ende"])])
        metrik("nachtlauf_fehler", "fehlgeschlagene Updates im letzten Nachtlauf", [({}, nacht.get("fehler", 0))])
    return "\n".join(zeilen) + "\n"


# --- Weboberfläche ---------------------------------------------------------------------------------------

STATISCH = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/app.css": ("app.css", "text/css; charset=utf-8"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
OFFEN = ("/ping", "/health")  # auch ohne Umweg über den Reverse Proxy erreichbar
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
SICHERHEITS_KOPF = {
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
    "Content-Security-Policy": CSP, "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


class Anfrage(http.server.BaseHTTPRequestHandler):
    server_version = "ecspve"
    sys_version = ""
    timeout = 30  # langsame oder hängende Verbindungen nicht ewig offen halten
    sammler: Sammler = None
    anmeldung: Anmeldung = None
    einst_lock = threading.Lock()

    def log_message(self, fmt, *args):
        pass

    # Antworten
    def senden(self, code, daten, typ="application/json; charset=utf-8", kopf=None):
        if isinstance(daten, (dict, list)):
            daten = json.dumps(daten, ensure_ascii=False)
        roh = daten.encode("utf-8") if isinstance(daten, str) else daten
        self.send_response(code)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(roh)))
        self.send_header("Cache-Control", "no-store" if typ.startswith("application/json") else "no-cache")
        for k, v in SICHERHEITS_KOPF.items():
            self.send_header(k, v)
        if self.https():
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in (kopf or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(roh)

    def fehler(self, code, text):
        self.senden(code, {"fehler": text})

    # Wer fragt, auf welchem Weg?
    def proxies(self):
        return [p.strip() for p in lade_konfig()["PROXY_IP"].split(",") if p.strip()]

    def vom_proxy(self):
        return self.client_address[0] in self.proxies()

    def client_ip(self):
        ip = self.client_address[0]
        weiter = self.headers.get("X-Forwarded-For", "")
        if self.vom_proxy() and weiter:
            ip = weiter.split(",")[-1].strip()
        return ip

    def https(self):
        return self.vom_proxy() and self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def zugang(self, pfad):
        """Ist PROXY_IP gesetzt, geht alles außer /ping und /health nur über den Reverse Proxy. Gesperrte IPs
        bekommen nur noch die Seite selbst, keine Daten."""
        if pfad in OFFEN:
            return True
        lokal = self.client_address[0] in ("127.0.0.1", "::1")
        if self.proxies() and not self.vom_proxy() and not lokal:
            self.fehler(403, "Bitte über die HTTPS-Adresse eures Reverse Proxys aufrufen.")
            return False
        if pfad not in STATISCH and self.anmeldung.gesperrt(self.client_ip()) == "ip":
            self.fehler(429, "Zu viele Fehlversuche von dieser Adresse – bitte 15 Minuten warten.")
            return False
        return True

    def token_der_sitzung(self):
        for teil in self.headers.get("Cookie", "").split(";"):
            name, _, wert = teil.strip().partition("=")
            if name == "ecspve":
                return wert
        return ""

    def sitzung(self):
        return self.anmeldung.hole(self.token_der_sitzung())

    def angemeldet(self):
        s = self.sitzung()
        return s if s and s["stufe"] == "voll" else None

    def token_ok(self):
        konf = lade_konfig()
        if not konf["TOKEN"]:
            return False
        geliefert = self.headers.get("Authorization", "")
        geliefert = geliefert[7:].strip() if geliefert.lower().startswith("bearer ") else ""
        if not geliefert and ja(konf["TOKEN_IN_URL"]):
            geliefert = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("token") or [""])[0]
        if not geliefert:
            return False
        if hmac.compare_digest(geliefert.encode(), konf["TOKEN"].encode()):
            return True
        self.fehlversuch("Falsches Monitoring-Token")
        return False

    def fehlversuch(self, text):
        ip = self.client_ip()
        ereignis(ip, text, ok=False)
        if self.anmeldung.fehlversuch(ip):
            art = self.anmeldung.gesperrt(ip)
            text = (f"{SPERRE_IP} Fehlversuche von {ip} – diese Adresse ist 15 Minuten gesperrt." if art == "ip" else
                    f"{SPERRE_GESAMT} Fehlversuche von verschiedenen Adressen – Anmeldung für alle 15 Minuten gesperrt.")
            ereignis(ip, text, ok=False)
            threading.Thread(target=melden, args=("Anmeldesperre an der Übersicht", text, True), daemon=True).start()
        time.sleep(1)  # bremst Durchprobieren zusätzlich

    def body(self):
        laenge = int(self.headers.get("Content-Length") or 0)
        if laenge > 65536:
            raise ValueError("zu groß")
        daten = json.loads(self.rfile.read(laenge) or b"{}")
        if not isinstance(daten, dict):
            raise ValueError("kein Objekt")
        return daten

    # GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        pfad = urllib.parse.urlsplit(self.path).path
        if not self.zugang(pfad):
            return
        if pfad in STATISCH:
            datei, typ = STATISCH[pfad]
            try:
                return self.senden(200, (WEB / datei).read_bytes(), typ)
            except OSError:
                return self.fehler(404, "nicht gefunden")
        if pfad == "/ping":
            return self.senden(200, "ok\n", "text/plain; charset=utf-8")
        if pfad == "/health":
            st = self.sammler.status()
            if st["ok"]:
                return self.senden(200, "OK\n", "text/plain; charset=utf-8")
            text = f"FEHLER: {len(st['probleme'])} Probleme\n"
            if ja(lade_konfig()["HEALTH_DETAILS"]) or self.token_ok() or self.angemeldet():
                text += "".join(f"- {p['text']}\n" for p in st["probleme"])
            return self.senden(503, text, "text/plain; charset=utf-8")
        if pfad == "/api/sitzung":
            s = self.sitzung()
            # Ob der zweite Faktor schon steht, erfährt nur, wer das Passwort kennt
            return self.senden(200, {"stufe": s["stufe"] if s else None,
                                     "zwei_faktor": zf_laden() is not None if s else None})
        sitzung = self.angemeldet()
        if not sitzung and not self.token_ok():
            return self.fehler(401, "Bitte anmelden.")
        if pfad == "/api/status":
            st = self.sammler.status()
            if sitzung:
                self.sammler.besuch()
                konf = lade_konfig()
                zf = zf_laden() or {}
                st["schnittstellen"] = {"token": konf["TOKEN"], "pve_url": konf["PVE_URL"],
                                        "token_in_url": ja(konf["TOKEN_IN_URL"])}
                st["sicherheit"] = {
                    "zwei_faktor_seit": zf.get("seit"), "notfall_uebrig": len(zf.get("notfall", [])),
                    "sitzungen": self.anmeldung.angemeldet(), "nur_proxy": bool(self.proxies()), "https": self.https(),
                    "ereignisse": letzte_ereignisse(15),
                }
            return self.senden(200, st)
        if pfad == "/metrics":
            return self.senden(200, metriken(self.sammler.status()), "text/plain; version=0.0.4; charset=utf-8")
        if pfad == "/api/anlegen" and sitzung:
            try:
                return self.senden(200, helfer("anlegen-optionen", timeout=90))
            except HelferFehler as e:
                return self.fehler(502, str(e))
        m = re.fullmatch(r"/api/auftrag/([0-9a-f-]+)", pfad)
        if m and sitzung and AUFTRAG_RE.match(m.group(1)):
            try:
                return self.senden(200, helfer("auftrag", m.group(1), timeout=30))
            except HelferFehler as e:
                return self.fehler(502, str(e))
        return self.fehler(404, "nicht gefunden")

    # POST
    def herkunft_ok(self):
        """Eigener Header (kann ein fremdes Formular nicht setzen) und – falls mitgeschickt – passende Herkunft."""
        if self.headers.get("X-ECSPVE") != "1":
            return False
        herkunft = self.headers.get("Origin")
        if not herkunft:
            return True
        erlaubt = {self.headers.get("Host", "")}
        if self.vom_proxy():
            erlaubt.add(self.headers.get("X-Forwarded-Host", ""))
        return urllib.parse.urlsplit(herkunft).netloc in erlaubt - {""}

    def do_POST(self):
        pfad = urllib.parse.urlsplit(self.path).path
        if not self.zugang(pfad):
            return
        if not self.herkunft_ok():
            return self.fehler(403, "Anfrage abgelehnt.")
        try:
            daten = self.body()
        except (ValueError, UnicodeDecodeError):
            return self.fehler(400, "Ungültige Anfrage.")
        anmelde_schritte = {"/api/anmelden": self.anmelden, "/api/anmelden/code": self.anmelden_code,
                            "/api/zwei-faktor/start": self.zf_start, "/api/zwei-faktor/bestaetigen": self.zf_bestaetigen}
        if pfad in anmelde_schritte:
            return anmelde_schritte[pfad](daten)
        if pfad == "/api/abmelden":
            self.anmeldung.ende(self.token_der_sitzung())
            return self.senden(200, {"ok": True}, kopf={"Set-Cookie": self.cookie("", 0)})
        if not self.angemeldet():
            return self.fehler(401, "Bitte anmelden.")
        aktionen = {"/api/aktion": self.aktion, "/api/host": self.host_aktion, "/api/auto": self.auto,
                    "/api/alle": lambda d: self.alle_aktualisieren(), "/api/anlegen": self.anlegen,
                    "/api/pruefung": self.pruefung, "/api/pruefen": self.pruefen,
                    "/api/zwei-faktor/geraet": self.zf_geraet, "/api/zwei-faktor/notfallcodes": self.zf_notfallcodes,
                    "/api/sitzungen/beenden": self.sitzungen_beenden}
        if pfad not in aktionen:
            return self.fehler(404, "nicht gefunden")
        try:
            erg = aktionen[pfad](daten)
        except HelferFehler as e:
            return self.fehler(409, str(e))
        except Verweigert as e:
            return self.fehler(403, str(e))
        except (KeyError, TypeError, ValueError):
            return self.fehler(400, "Ungültige Anfrage.")
        if pfad not in ("/api/pruefen", "/api/zwei-faktor/geraet", "/api/zwei-faktor/notfallcodes",
                        "/api/sitzungen/beenden"):
            ereignis(self.client_ip(), aktion_text(pfad, daten, self.sammler))
        self.sammler.auftrag_eintragen(erg)
        self.sammler.wecker.set()
        return self.senden(200, erg)

    def cookie(self, wert, alter):
        sicher = "; Secure" if self.https() else ""
        return f"ecspve={wert}; Path=/; Max-Age={alter}; HttpOnly; SameSite=Strict{sicher}"

    # Anmeldung in zwei Schritten
    def anmelde_sperre(self):
        art = self.anmeldung.gesperrt(self.client_ip())
        if art == "ip":
            return "Zu viele Fehlversuche von dieser Adresse – bitte 15 Minuten warten."
        if art == "gesamt":
            return "Zu viele Fehlversuche – die Anmeldung ist für alle 15 Minuten gesperrt."
        return None

    def anmelden(self, daten):
        ip = self.client_ip()
        if self.proxies() and not self.https() and self.client_address[0] not in ("127.0.0.1", "::1"):
            return self.fehler(403, "Anmeldung nur über HTTPS.")
        if not PASS_FILE.exists():
            return self.fehler(503, "Noch kein Passwort gesetzt – im Container „ecspve passwort“ ausführen.")
        sperre = self.anmelde_sperre()
        if sperre:
            return self.fehler(429, sperre)
        if not passwort_pruefen(str(daten.get("passwort", ""))[:1024]):
            self.fehlversuch("Passwort falsch")
            return self.fehler(403, "Passwort falsch.")
        token = self.anmeldung.neu("passwort", ip)
        schritt = "code" if zf_laden() else "einrichten"
        return self.senden(200, {"schritt": schritt}, kopf={"Set-Cookie": self.cookie(token, VORSTUFE_DAUER)})

    def voll_anmelden(self, text):
        """Nach Passwort und zweitem Faktor: neue Sitzung (neues Token – das alte ist damit wertlos)."""
        ip = self.client_ip()
        self.anmeldung.ende(self.token_der_sitzung())
        self.anmeldung.erfolg(ip)
        ereignis(ip, text)
        if ja(lade_konfig()["MELDUNG_ANMELDUNG"]):
            threading.Thread(target=melden, args=("Anmeldung an der Übersicht", f"{text} von {ip}"), daemon=True).start()
        return self.cookie(self.anmeldung.neu("voll", ip), SITZUNG_HOECHSTENS)

    def anmelden_code(self, daten):
        s = self.sitzung()
        if not s or s["stufe"] != "passwort" or not zf_laden():
            return self.fehler(401, "Bitte zuerst das Passwort eingeben.")
        sperre = self.anmelde_sperre()
        if sperre:
            return self.fehler(429, sperre)
        if self.anmeldung.codes_gesperrt():
            return self.fehler(429, "Zu viele falsche Codes – die Anmeldung ist für eine Stunde gesperrt.")
        art = code_pruefen(daten.get("code"), anmeldung=True)
        if not art:
            if self.anmeldung.falscher_code():
                text = (f"{SPERRE_CODES} falsche Codes nach richtigem Passwort innerhalb einer Stunde – das Passwort ist "
                        "vermutlich bekannt. Anmeldung für eine Stunde gesperrt. Bitte im Container „ecspve passwort“ "
                        "ausführen.")
                ereignis(self.client_ip(), text, ok=False)
                threading.Thread(target=melden, args=("Passwort der Übersicht vermutlich bekannt", text, True),
                                 daemon=True).start()
            self.fehlversuch("Passwort richtig, Code falsch")
            return self.fehler(403, "Code falsch oder schon benutzt – bitte den nächsten Code der App nehmen.")
        cookie = self.voll_anmelden("Anmeldung mit Notfallcode" if art == "notfall" else "Anmeldung")
        uebrig = len((zf_laden() or {}).get("notfall", []))
        return self.senden(200, {"ok": True, "notfall": art == "notfall", "notfall_uebrig": uebrig},
                           kopf={"Set-Cookie": cookie})

    def zf_start(self, daten):
        """Neues Geheimnis für die App: beim ersten Mal nach dem Passwort, später nur mit gültigem Code."""
        s = self.sitzung()
        if not s:
            return self.fehler(401, "Bitte anmelden.")
        if s["stufe"] == "voll":
            if not code_pruefen(daten.get("code")):
                self.fehlversuch("Code falsch (zweiten Faktor neu einrichten)")
                return self.fehler(403, "Code falsch.")
        elif zf_laden():
            return self.fehler(403, "Der zweite Faktor ist schon eingerichtet – bitte den Code eingeben.")
        s["neu_geheim"] = neues_geheimnis()
        return self.senden(200, einrichtung(s["neu_geheim"]))

    def zf_bestaetigen(self, daten):
        s = self.sitzung()
        if not s or not s.get("neu_geheim"):
            return self.fehler(401, "Bitte die Einrichtung neu beginnen.")
        sperre = self.anmelde_sperre()
        if sperre:
            return self.fehler(429, sperre)
        code = re.sub(r"\s", "", str(daten.get("code") or ""))
        schritt = totp_schritt(s["neu_geheim"], code) if re.fullmatch(r"[0-9]{6}", code) else None
        if schritt is None:
            self.fehlversuch("Code falsch (Einrichtung des zweiten Faktors)")
            return self.fehler(403, "Der Code passt nicht. Stimmt die Uhrzeit auf dem Handy?")
        codes, hashes = neue_notfallcodes()
        with ZF_LOCK:
            zf_speichern({"geheim": s["neu_geheim"], "seit": time.time(), "letzter_schritt": schritt, "notfall": hashes})
        # Alle bisherigen Sitzungen gelten ab jetzt nicht mehr (anmeldestand hat sich geändert)
        cookie = self.voll_anmelden("Zweiter Faktor eingerichtet, angemeldet")
        threading.Thread(target=melden, args=("Zweiter Faktor neu eingerichtet",
                                              f"Neue Authenticator-App für die Übersicht, von {self.client_ip()}", True),
                         daemon=True).start()
        return self.senden(200, {"notfallcodes": codes}, kopf={"Set-Cookie": cookie})

    def code_verlangen(self, daten, wofuer):
        if not code_pruefen(daten.get("code")):
            self.fehlversuch(f"Code falsch ({wofuer})")
            raise Verweigert("Code falsch.")

    def zf_geraet(self, daten):
        """QR-Code des bestehenden Geheimnisses für ein weiteres Handy – nur mit gültigem Code."""
        self.code_verlangen(daten, "weiteres Gerät")
        ereignis(self.client_ip(), "QR-Code für ein weiteres Gerät angezeigt")
        return einrichtung((zf_laden() or {})["geheim"])

    def zf_notfallcodes(self, daten):
        self.code_verlangen(daten, "neue Notfallcodes")
        codes, hashes = neue_notfallcodes()
        with ZF_LOCK:
            zf = zf_laden()
            zf["notfall"] = hashes
            zf_speichern(zf)
        ereignis(self.client_ip(), "Neue Notfallcodes erzeugt – die alten gelten nicht mehr")
        return {"notfallcodes": codes}

    def sitzungen_beenden(self, daten):
        self.anmeldung.alle_beenden(ausser=self.token_der_sitzung())
        ereignis(self.client_ip(), "Alle anderen Sitzungen abgemeldet")
        return {"ok": True}

    def pruefen(self, daten):
        self.sammler.pruef_wecker.set()
        self.sammler.wecker.set()
        return {"ok": True}

    def gast(self, daten):
        gid = int(daten["id"])
        for g in self.sammler.status()["gaeste"]:
            if g["id"] == gid:
                if not g["lokal"]:
                    raise HelferFehler(f"{g['name']} läuft auf dem Knoten {g['knoten']} – dort verwalten.")
                return g
        raise HelferFehler(f"Gast {gid} gibt es nicht.")

    def aktion(self, daten):
        g = self.gast(daten)
        aktion = daten["aktion"]
        if aktion in ("start", "herunterfahren", "neustart", "stopp"):
            return helfer(aktion, g["id"])
        if aktion == "update":
            was = daten.get("was", "alles")
            if was not in ("alles", "system", "app"):
                raise ValueError(was)
            return helfer("update", g["id"], was, *update_optionen(lade_konfig()))
        if aktion == "dienst":
            dienst = str(daten["dienst"])
            bekannte = [d["name"] for d in (g.get("probe") or {}).get("dienste", [])]
            if not DIENST_RE.match(dienst) or dienst not in bekannte:
                raise ValueError(dienst)
            return helfer("dienst", g["id"], dienst)
        raise ValueError(aktion)

    def anlegen(self, daten):
        """Neuen Container anlegen – geprüft wird alles im Helfer, hier nur die Form der Werte."""
        args = ["anlegen", str(daten["name"]).strip()]
        for k in ("cores", "memory", "disk", "vlan", "ctid"):
            if daten.get(k) not in (None, ""):
                args.append(f"--{k}={int(daten[k])}")
        for k in ("storage", "bridge", "ip", "gw"):
            if daten.get(k):
                args.append(f"--{k}={str(daten[k]).strip()}")
        for k in ("onboot", "start"):
            args.append(f"--{k}={1 if daten.get(k, True) else 0}")
        return helfer(*args, timeout=90)

    def pruefung(self, daten):
        g = self.gast(daten)
        adresse = str(daten.get("adresse") or "").strip()
        erg = helfer("pruefung", g["id"], adresse or "--aus", timeout=30)
        self.sammler.pruef_wecker.set()
        return erg

    def host_aktion(self, daten):
        if daten.get("aktion") == "update":
            return helfer("host-update")
        if daten.get("aktion") == "auffrischen":
            return helfer("auffrischen")
        raise ValueError(daten.get("aktion"))

    def auto(self, daten):
        g = self.gast(daten)
        if g["typ"] != "lxc":
            raise HelferFehler("Automatische Updates gibt es nur für Container.")
        with self.einst_lock:
            einst = lade_einstellungen()
            einst[str(g["id"])] = {"system": bool(daten.get("system")), "app": bool(daten.get("app")), "name": g["name"]}
            schreibe_json(EINST_FILE, einst)
        return {"ok": True}

    def alle_aktualisieren(self):
        opts = update_optionen(lade_konfig())
        gestartet, fehler = [], []
        for g in self.sammler.status()["gaeste"]:
            if g["typ"] != "lxc" or not g["lokal"] or g["status"] != "running" or g["auftrag"]:
                continue
            try:
                job = helfer("update", g["id"], "alles", *opts)
                self.sammler.auftrag_eintragen(job)
                gestartet.append(job["id"])
            except HelferFehler as e:
                fehler.append(f"{g['name']}: {e}")
        return {"auftraege": gestartet, "fehler": fehler}


class Verweigert(Exception):
    pass


def aktion_text(pfad, daten, sammler):
    """Kurze Beschreibung einer Aktion für das Sicherheitsprotokoll."""
    namen = {g["id"]: g["name"] for g in sammler.status()["gaeste"]}
    try:
        gast = f"{namen.get(int(daten.get('id')), daten.get('id'))}" if daten.get("id") is not None else ""
    except (TypeError, ValueError):
        gast = ""
    if pfad == "/api/aktion":
        was = daten.get("dienst") or daten.get("was") or ""
        return f"Aktion {daten.get('aktion')} {was} – {gast}".replace("  ", " ")
    if pfad == "/api/host":
        return f"Host: {daten.get('aktion')}"
    if pfad == "/api/auto":
        an = {k: "an" if daten.get(k) else "aus" for k in ("system", "app")}
        return f"Nachts automatisch für {gast}: System {an['system']}, App {an['app']}"
    if pfad == "/api/anlegen":
        return f"Container anlegen: {str(daten.get('name'))[:63]}"
    if pfad == "/api/pruefung":
        return f"Funktionsprüfung für {gast}: {str(daten.get('adresse') or 'aus')[:200]}"
    if pfad == "/api/alle":
        return "Alle Container aktualisieren"
    return pfad


class Webserver(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve():
    konf = lade_konfig()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    sammler = Sammler()
    sammler.start()
    Anfrage.sammler, Anfrage.anmeldung = sammler, Anmeldung()
    server = Webserver((konf["ADRESSE"], int(konf["PORT"])), Anfrage)
    log(f"ecspve {VERSION} hört auf http://{konf['ADRESSE']}:{konf['PORT']}")
    if not PASS_FILE.exists():
        log("Hinweis: noch kein Passwort gesetzt – „ecspve passwort“ ausführen.")
    server.serve_forever()


# --- Nachtlauf -------------------------------------------------------------------------------------------

def warte_auf(jid, hoechstens=3 * 3600):
    ende = time.time() + hoechstens
    while time.time() < ende:
        time.sleep(10)
        try:
            job = helfer("auftrag", jid, timeout=60)
        except HelferFehler:
            continue
        if job.get("status") not in ("wartet", "laeuft"):
            job.pop("protokoll", None)
            return job
    return {"id": jid, "status": "fehler", "ergebnis": "keine Rückmeldung nach drei Stunden"}


def nacht():
    konf = lade_konfig()
    einst = lade_einstellungen()
    bericht = {"start": time.time(), "ende": None, "ergebnisse": [], "fehler": 0}

    def eintragen(titel, job):
        bericht["ergebnisse"].append({"titel": titel, "status": job.get("status"), "ergebnis": job.get("ergebnis", ""),
                                      "auftrag": job.get("id")})
        bericht["fehler"] = sum(e["status"] == "fehler" for e in bericht["ergebnisse"])
        schreibe_json(NACHT_FILE, bericht)

    try:
        ueb = helfer("uebersicht")
    except HelferFehler as e:
        eintragen("Verbindung zum Proxmox-Host", {"status": "fehler", "ergebnis": str(e)})
        bericht["ende"] = time.time()
        schreibe_json(NACHT_FILE, bericht)
        melden("Nachtlauf nicht möglich", str(e), True)
        return 1

    # Paketlisten überall auffrischen, damit die Übersicht stimmt – auch ohne automatische Updates
    try:
        warte_auf(helfer("auffrischen")["id"], 1800)
    except HelferFehler as e:
        log(f"Auffrischen nicht möglich: {e}")

    plan = []
    # Die Übersicht selbst zuletzt – ihr Update startet diesen Container ggf. neu
    for g in sorted(ueb["gaeste"], key=lambda g: ("ecspve" in g.get("tags", []), g["id"])):
        if g["typ"] != "lxc" or not g["lokal"] or g["status"] != "running" or g["vorlage"]:
            continue
        e = einst.get(str(g["id"]), {})
        if e.get("system") or e.get("app"):
            plan.append((g, "alles" if e.get("system") and e.get("app") else ("system" if e.get("system") else "app")))
    if ja(konf["HOST_AUTO_UPDATE"]):
        try:
            eintragen(f"Proxmox-Host {ueb.get('knoten', '')}", warte_auf(helfer("host-update")["id"]))
        except HelferFehler as e:
            eintragen("Proxmox-Host", {"status": "fehler", "ergebnis": str(e)})
    for g, was in plan:
        titel = f"{g['name']} ({g['id']})"
        try:
            job = helfer("update", g["id"], was, *update_optionen(konf))
            eintragen(titel, warte_auf(job["id"]))
        except HelferFehler as e:
            eintragen(titel, {"status": "fehler", "ergebnis": str(e)})
    bericht["ende"] = time.time()
    schreibe_json(NACHT_FILE, bericht)
    if bericht["ergebnisse"]:
        zeichen = {"ok": "✔", "warnung": "⚠", "fehler": "✘"}
        text = "\n".join(f"{zeichen.get(e['status'], '?')} {e['titel']}: {e['ergebnis']}" for e in bericht["ergebnisse"])
        probleme = sum(e["status"] != "ok" for e in bericht["ergebnisse"])
        melden("Nachtlauf: alles aktualisiert" if not probleme else f"Nachtlauf: {probleme} mit Problemen", text,
               bool(probleme))
    return 1 if bericht["fehler"] else 0


# --- Kommandozeile ---------------------------------------------------------------------------------------

def setze_konfig(schluessel, wert):
    zeilen = CONF_FILE.read_text(encoding="utf-8").splitlines() if CONF_FILE.exists() else []
    neu, gesetzt = [], False
    for zeile in zeilen:
        if re.match(rf"^\s*{schluessel}\s*=", zeile):
            if not gesetzt:
                neu.append(f"{schluessel}={wert}")
            gesetzt = True
        else:
            neu.append(zeile)
    if not gesetzt:
        neu.append(f"{schluessel}={wert}")
    tmp = CONF_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(neu) + "\n", encoding="utf-8")
    if CONF_FILE.exists():
        st = CONF_FILE.stat()
        os.chmod(tmp, st.st_mode & 0o777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass
    tmp.replace(CONF_FILE)


def cmd_passwort(args):
    if "--stdin" in args:
        passwort = sys.stdin.readline().rstrip("\n")
    else:
        passwort = getpass.getpass(f"Neues Passwort für die Übersicht (mind. {PASSWORT_MIN} Zeichen): ")
        if getpass.getpass("Wiederholen: ") != passwort:
            sys.exit("Die Passwörter stimmen nicht überein.")
    if len(passwort) < PASSWORT_MIN:
        sys.exit(f"Das Passwort muss mindestens {PASSWORT_MIN} Zeichen lang sein.")
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PASS_FILE.with_suffix(".tmp")
    tmp.write_text(passwort_hash(passwort) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o640)
    try:
        os.chown(tmp, 0, grp.getgrnam("ecspve").gr_gid)
    except (KeyError, PermissionError):
        pass
    tmp.replace(PASS_FILE)
    ereignis("Kommandozeile", "Passwort neu gesetzt – alle Sitzungen abgemeldet")
    print("Passwort gesetzt. Alle Sitzungen sind damit abgemeldet.")


def cmd_zwei_faktor(args):
    zf = zf_laden()
    if args[:1] == ["zuruecksetzen"]:
        if not zf:
            print("Der zweite Faktor ist nicht eingerichtet – bei der nächsten Anmeldung wird er eingerichtet.")
            return 0
        ZF_FILE.unlink()
        ereignis("Kommandozeile", "Zweiter Faktor zurückgesetzt – alle Sitzungen abgemeldet", ok=False)
        melden("Zweiter Faktor zurückgesetzt", "Auf der Kommandozeile im Container der Übersicht. Bei der nächsten "
               "Anmeldung wird eine neue Authenticator-App eingerichtet.", True)
        print("Zurückgesetzt. Bei der nächsten Anmeldung (Passwort) wird eine neue Authenticator-App eingerichtet.")
        return 0
    if args:
        print(f"Unbekannt: {' '.join(args)} – möglich ist: ecspve zwei-faktor zuruecksetzen", file=sys.stderr)
        return 2
    if not zf:
        print("Zweiter Faktor: noch nicht eingerichtet – das passiert bei der nächsten Anmeldung.")
    else:
        print(f"Zweiter Faktor: eingerichtet seit {time.strftime('%d.%m.%Y %H:%M', time.localtime(zf['seit']))}, "
              f"{len(zf.get('notfall', []))} Notfallcodes übrig.")
    return 0


def cmd_token(args):
    if "--neu" in args or not lade_konfig()["TOKEN"]:
        setze_konfig("TOKEN", secrets.token_urlsafe(24))
        global _konf_cache
        _konf_cache = (0.0, None)
        print("Neues Token erzeugt – bisherige Monitoring-Abfragen mit dem alten Token schlagen jetzt fehl.",
              file=sys.stderr)
    print(lade_konfig()["TOKEN"])


def cmd_ping():
    konf = lade_konfig()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{konf['PORT']}/ping", timeout=5) as r:
            print(r.status)
            return 0 if r.status == 200 else 1
    except Exception as e:  # noqa: BLE001
        print(f"keine Antwort: {e}")
        return 1


def cmd_status():
    try:
        ueb, fehler = helfer("uebersicht"), ""
        proben = helfer("pruefen", timeout=150)
    except HelferFehler as e:
        ueb, fehler, proben = None, str(e), {}
    st = baue_status(ueb, fehler, proben.get("gaeste", {}), proben.get("zeit"))
    h = st["host"]
    if h:
        print(f"Host {h.get('name')} · PVE {h.get('pve') or '?'} · Updates: {h.get('updates', '?')}"
              f"{' · Neustart nötig' if h.get('kernel_neustart') else ''}")
    for g in st["gaeste"]:
        pr = g["probe"] or {}
        dienste = sum(d["zustand"] == "ok" for d in pr.get("dienste", []))
        zeichen = "✘" if g["probleme"] else ("✔" if g["status"] == "running" else "·")
        print(f" {zeichen} {g['id']:>5}  {g['name'][:22]:<22} {g['status']:<8} {pr.get('app_name', ''):<14}"
              f" Dienste ok: {dienste:<3} Updates: {pr.get('updates', '-')}")
    for p in st["probleme"]:
        print(f"Problem: {p['text']}")
    for t in st["hinweise"]:
        print(f"Hinweis: {t}")
    return 0 if st["ok"] else 1


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "hilfe"):
        print(__doc__)
        return 0
    befehl, rest = args[0], args[1:]
    if befehl == "serve":
        serve()
    elif befehl == "nacht":
        return nacht()
    elif befehl == "status":
        return cmd_status()
    elif befehl == "passwort":
        cmd_passwort(rest)
    elif befehl == "token":
        cmd_token(rest)
    elif befehl == "zwei-faktor":
        return cmd_zwei_faktor(rest)
    elif befehl == "ping":
        return cmd_ping()
    elif befehl == "verbindung":
        try:
            antwort = helfer("version", timeout=30)
        except HelferFehler as e:
            print(f"Keine Verbindung: {e}", file=sys.stderr)
            return 1
        print(f"Verbindung in Ordnung – Proxmox-Knoten {antwort.get('knoten')}, Helfer-Version {antwort.get('version')}")
    else:
        print(f"Unbekannter Befehl: {befehl}\n{__doc__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
