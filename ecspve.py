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
import re
import secrets
import shlex
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

VORGABEN = {
    "PVE_HOST": "", "PVE_URL": "", "ADRESSE": "0.0.0.0", "PORT": "8080", "TOKEN": "", "PROXY_IP": "",
    "SNAPSHOTS_BEHALTEN": "2", "NEUSTART_NACH_UPDATE": "nein", "HOST_AUTO_UPDATE": "nein",
    "MELDUNG_URL": "", "MELDUNG_TOKEN": "", "HEARTBEAT_URL": "", "HEALTH_DETAILS": "nein",
}
SITZUNG_DAUER = 12 * 3600
PBKDF2_RUNDEN = 600_000
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


# --- Anmeldung ------------------------------------------------------------------------------------------

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


class Anmeldung:
    def __init__(self):
        self.lock = threading.Lock()
        self.sitzungen = {}
        self.fehlversuche = {}

    def neu(self):
        token = secrets.token_urlsafe(32)
        with self.lock:
            jetzt = time.time()
            self.sitzungen = {t: b for t, b in self.sitzungen.items() if b > jetzt}
            self.sitzungen[token] = jetzt + SITZUNG_DAUER
        return token

    def gueltig(self, token):
        with self.lock:
            bis = self.sitzungen.get(token or "")
            if not bis or bis < time.time():
                return False
            self.sitzungen[token] = time.time() + SITZUNG_DAUER
            return True

    def ende(self, token):
        with self.lock:
            self.sitzungen.pop(token or "", None)

    def gesperrt(self, ip):
        with self.lock:
            versuche = [t for t in self.fehlversuche.get(ip, []) if t > time.time() - 900]
            self.fehlversuche[ip] = versuche
            return len(versuche) >= 5

    def fehlversuch(self, ip):
        with self.lock:
            self.fehlversuche.setdefault(ip, []).append(time.time())


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
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class Anfrage(http.server.BaseHTTPRequestHandler):
    server_version = "ecspve"
    sys_version = ""
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
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (kopf or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(roh)

    def fehler(self, code, text):
        self.senden(code, {"fehler": text})

    # Wer fragt?
    def client_ip(self):
        ip = self.client_address[0]
        proxies = [p.strip() for p in lade_konfig()["PROXY_IP"].split(",") if p.strip()]
        weiter = self.headers.get("X-Forwarded-For", "")
        if ip in proxies and weiter:
            ip = weiter.split(",")[-1].strip()
        return ip

    def sitzung(self):
        for teil in self.headers.get("Cookie", "").split(";"):
            name, _, wert = teil.strip().partition("=")
            if name == "ecspve":
                return wert
        return ""

    def angemeldet(self):
        return self.anmeldung.gueltig(self.sitzung())

    def token_ok(self):
        token = lade_konfig()["TOKEN"]
        if not token:
            return False
        geliefert = self.headers.get("Authorization", "")
        geliefert = geliefert[7:].strip() if geliefert.lower().startswith("bearer ") else ""
        if not geliefert:
            geliefert = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("token") or [""])[0]
        return bool(geliefert) and hmac.compare_digest(geliefert.encode(), token.encode())

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
        sitzung = self.angemeldet()
        if not sitzung and not self.token_ok():
            return self.fehler(401, "Bitte anmelden.")
        if pfad == "/api/status":
            st = self.sammler.status()
            if sitzung:
                self.sammler.besuch()
                konf = lade_konfig()
                st["schnittstellen"] = {"token": konf["TOKEN"], "pve_url": konf["PVE_URL"]}
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
    def do_POST(self):
        pfad = urllib.parse.urlsplit(self.path).path
        # Eigener Header: ein fremdes Formular kann ihn nicht mitschicken (Schutz vor CSRF)
        if self.headers.get("X-ECSPVE") != "1":
            return self.fehler(403, "Anfrage abgelehnt.")
        try:
            daten = self.body()
        except (ValueError, UnicodeDecodeError):
            return self.fehler(400, "Ungültige Anfrage.")
        if pfad == "/api/anmelden":
            return self.anmelden(daten)
        if pfad == "/api/abmelden":
            self.anmeldung.ende(self.sitzung())
            return self.senden(200, {"ok": True}, kopf={"Set-Cookie": self.cookie("", 0)})
        if not self.angemeldet():
            return self.fehler(401, "Bitte anmelden.")
        try:
            if pfad == "/api/aktion":
                erg = self.aktion(daten)
            elif pfad == "/api/host":
                erg = self.host_aktion(daten)
            elif pfad == "/api/auto":
                erg = self.auto(daten)
            elif pfad == "/api/alle":
                erg = self.alle_aktualisieren()
            elif pfad == "/api/anlegen":
                erg = self.anlegen(daten)
            elif pfad == "/api/pruefung":
                erg = self.pruefung(daten)
            elif pfad == "/api/pruefen":
                self.sammler.pruef_wecker.set()
                self.sammler.wecker.set()
                erg = {"ok": True}
            else:
                return self.fehler(404, "nicht gefunden")
        except HelferFehler as e:
            return self.fehler(409, str(e))
        except (KeyError, TypeError, ValueError):
            return self.fehler(400, "Ungültige Anfrage.")
        self.sammler.auftrag_eintragen(erg)
        self.sammler.wecker.set()
        return self.senden(200, erg)

    def cookie(self, wert, alter):
        sicher = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
        return f"ecspve={wert}; Path=/; Max-Age={alter}; HttpOnly; SameSite=Strict{sicher}"

    def anmelden(self, daten):
        ip = self.client_ip()
        if not PASS_FILE.exists():
            return self.fehler(503, "Noch kein Passwort gesetzt – im Container „ecspve passwort“ ausführen.")
        if self.anmeldung.gesperrt(ip):
            return self.fehler(429, "Zu viele Fehlversuche – bitte 15 Minuten warten.")
        if not passwort_pruefen(str(daten.get("passwort", ""))):
            self.anmeldung.fehlversuch(ip)
            log(f"Fehlgeschlagene Anmeldung von {ip}")
            return self.fehler(403, "Passwort falsch.")
        return self.senden(200, {"ok": True}, kopf={"Set-Cookie": self.cookie(self.anmeldung.neu(), SITZUNG_DAUER)})

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
        passwort = getpass.getpass("Neues Passwort für die Übersicht (mind. 10 Zeichen): ")
        if getpass.getpass("Wiederholen: ") != passwort:
            sys.exit("Die Passwörter stimmen nicht überein.")
    if len(passwort) < 10:
        sys.exit("Das Passwort muss mindestens 10 Zeichen lang sein.")
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PASS_FILE.with_suffix(".tmp")
    tmp.write_text(passwort_hash(passwort) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o640)
    try:
        os.chown(tmp, 0, grp.getgrnam("ecspve").gr_gid)
    except (KeyError, PermissionError):
        pass
    tmp.replace(PASS_FILE)
    print("Passwort gesetzt.")


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
