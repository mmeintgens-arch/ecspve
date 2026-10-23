"""Tests für Helfer und Weboberfläche – gegen den nachgebauten Proxmox-Host aus test/fake.py.

  python3 -m unittest discover -s test -v
"""

import http.client
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJEKT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJEKT / "test"))
import fake  # noqa: E402

ORDNER = Path(tempfile.mkdtemp(prefix="ecspve-test-"))
fake.anlegen(ORDNER)
UMGEBUNG = fake.umgebung(ORDNER)
os.environ.update(UMGEBUNG)
sys.path.insert(0, str(PROJEKT))

lader = importlib.machinery.SourceFileLoader("ecspve_helfer", str(fake.HELFER))
spec = importlib.util.spec_from_loader("ecspve_helfer", lader)
helfer = importlib.util.module_from_spec(spec)
lader.exec_module(helfer)
import ecspve  # noqa: E402


def helfer_aufruf(*args, ssh=None):
    env = dict(os.environ)
    if ssh is not None:
        env.update(SSH_CONNECTION="10.0.0.9 1 10.0.0.1 22", SSH_ORIGINAL_COMMAND=ssh)
    p = subprocess.run([sys.executable, str(fake.HELFER), *args], capture_output=True, text=True, env=env, timeout=60)
    return p.returncode, json.loads(p.stdout or "null")


def warte_auf_auftrag(jid, sekunden=60):
    ende = time.time() + sekunden
    while time.time() < ende:
        job = helfer.lies(jid)
        if job["status"] not in ("wartet", "laeuft"):
            return job
        time.sleep(0.5)
    raise AssertionError(f"Auftrag {jid} wurde nicht fertig")


class Pruefskript(unittest.TestCase):
    def test_einheiten_bewerten(self):
        b = helfer.bewerte_einheit
        self.assertEqual(b({"Id": "a.service", "LoadState": "loaded", "ActiveState": "active"})["zustand"], "ok")
        self.assertEqual(b({"Id": "a.service", "LoadState": "loaded", "ActiveState": "failed"})["zustand"], "fehler")
        # eingeschaltet, läuft aber nicht
        self.assertEqual(b({"Id": "a.service", "LoadState": "loaded", "ActiveState": "inactive",
                            "Type": "simple", "UnitFileState": "enabled"})["zustand"], "fehler")
        # läuft nur per Zeitplan
        self.assertEqual(b({"Id": "a.service", "LoadState": "loaded", "ActiveState": "inactive",
                            "Type": "oneshot", "UnitFileState": "enabled"})["zustand"], "ruhe")
        self.assertEqual(b({"Id": "a.service", "LoadState": "loaded", "ActiveState": "inactive",
                            "Type": "simple", "UnitFileState": "static"})["zustand"], "ruhe")
        self.assertIsNone(b({"Id": "nginx.service", "LoadState": "not-found"}))

    def test_alle_container(self):
        erg = helfer.pruefen_alle()
        self.assertEqual(sorted(erg), ["101", "102", "103", "104", "105", "110", "120"])
        web = erg["101"]
        self.assertEqual(web["apps"], ["ecswebsite"])
        self.assertEqual(web["updates"], 3)
        self.assertEqual(web["ip"], "192.168.10.51")
        self.assertTrue(web["app_update"])
        self.assertEqual([p["ok"] for p in web["pruefungen"]], [True])
        zustaende = {d["name"]: d["zustand"] for d in web["dienste"]}
        self.assertEqual(zustaende["ecswebsite-web.service"], "ok")
        self.assertEqual(zustaende["ecswebsite-nightly.service"], "ruhe")
        self.assertEqual(zustaende["nginx.service"], "ok")
        self.assertNotIn("caddy.service", zustaende)
        self.assertEqual({d["name"]: d["zustand"] for d in erg["104"]["dienste"]}["eventcrew-worker.service"], "fehler")
        self.assertTrue(erg["103"]["neustart"])
        # StreetworkDoc ohne Projektordner auf dem Host: kein App-Update eingerichtet
        self.assertFalse(erg["105"]["app_update"])
        self.assertEqual(erg["120"]["apps"], [])

    def test_app_update_befehle(self):
        konf = {"STREETWORKDOC_QUELLE": "/gibt/es/nicht"}
        self.assertEqual(helfer.app_update_befehle(101, ["ecswebsite"], konf),
                         [[f"{ORDNER}/opt/ecswebsite/pve-install.sh", "--pull", "--update", "--ctid=101"]])
        # ohne Wrapper auf dem Host: Update im Container
        self.assertEqual(helfer.app_update_befehle(102, ["turnierapp"], konf),
                         [["pct", "exec", "102", "--", "env", f"PATH={helfer.PFAD}", "turnierapp-update"]])
        self.assertIsNone(helfer.app_update_befehle(105, ["streetworkdoc"], konf))
        self.assertEqual(helfer.app_update_befehle(130, [], {"UPDATE_130": "mein-update {id}"}),
                         [["bash", "-c", "mein-update 130"]])


class HelferBefehle(unittest.TestCase):
    def test_uebersicht(self):
        rc, d = helfer_aufruf("uebersicht")
        self.assertEqual(rc, 0)
        self.assertEqual(d["host"]["pve"], "8.4.1")
        self.assertEqual(d["host"]["updates"], 3)
        g = {x["id"]: x for x in d["gaeste"]}
        self.assertTrue(g[101]["onboot"])
        self.assertTrue(g[9000]["vorlage"])
        self.assertEqual(g[200]["typ"], "qemu")

    def test_ungueltige_eingaben(self):
        for args in (["dienst", "104", "x;rm -rf /"], ["update", "101", "alles", "--boese"], ["update", "0101", "alles"],
                     ["start", "999"], ["update", "200", "alles"], ["frei"], ["auftrag", "../../etc/passwd"]):
            rc, d = helfer_aufruf(*args)
            self.assertEqual(rc, 1, args)
            self.assertIn("fehler", d)

    def test_ssh_nur_erlaubte_befehle(self):
        rc, d = helfer_aufruf(ssh="version")
        self.assertEqual((rc, d["version"]), (0, helfer.VERSION))
        rc, d = helfer_aufruf(ssh="_lauf 20260101-000000-abcdef")
        self.assertEqual(rc, 2)
        rc, d = helfer_aufruf(ssh='update 101 "alles; id"')
        self.assertEqual(rc, 1)
        rc, d = helfer_aufruf(ssh="")
        self.assertEqual(rc, 1)

    def test_update_mit_snapshot(self):
        rc, job = helfer_aufruf("update", "102", "alles", "--snapshots=1")
        self.assertEqual(rc, 0, job)
        rc, doppelt = helfer_aufruf("update", "102", "system")
        self.assertEqual(rc, 1)
        self.assertIn("läuft schon", doppelt["fehler"])
        fertig = warte_auf_auftrag(job["id"])
        self.assertEqual(fertig["status"], "ok", fertig)
        rc, job2 = helfer_aufruf("update", "102", "system", "--snapshots=1")
        self.assertEqual(warte_auf_auftrag(job2["id"])["status"], "ok")
        snaps = helfer.gast_conf("pve", "lxc", 102)["snapshots"]
        self.assertEqual(len(snaps), 1, snaps)  # der ältere ist wieder weg
        rc, mit_log = helfer_aufruf("auftrag", job["id"])
        self.assertIn("turnierapp-update", mit_log["protokoll"])

    def test_anlegen(self):
        rc, d = helfer_aufruf("anlegen-optionen")
        self.assertEqual(d["bruecken"], ["vmbr0", "vmbr1"])
        self.assertEqual([s["name"] for s in d["speicher"]], ["local-lvm"])  # inaktive fallen weg
        self.assertTrue(d["vorlage"].startswith("debian-13-standard"))
        for args in (["Gross"], ["ha", "--ip=192.168.10.70"], ["ha", "--ip=192.168.10.70/24", "--gw=10.0.0.1"],
                     ["ha", "--ip=192.168.10.70/24"], ["ha", "--bridge=vmbr9"], ["ha", "--storage=backup"],
                     ["ha", "--ctid=101"], ["ecswiki"], ["ha", "--cores=2;id"], ["ha", "--memory=99"], ["ha", "--boese=1"]):
            rc, d = helfer_aufruf("anlegen", *args)
            self.assertEqual(rc, 1, args)
            self.assertIn("fehler", d)
        rc, job = helfer_aufruf("anlegen", "neuer-ct", "--ip=192.168.10.70/24", "--gw=192.168.10.1", "--vlan=10",
                                "--onboot=0", "--start=0")
        self.assertEqual(rc, 0, job)
        fertig = warte_auf_auftrag(job["id"], 90)
        self.assertEqual(fertig["status"], "ok", fertig)
        self.assertEqual(len(fertig["geheim"]["Root-Passwort"]), 20)
        rc, mit_log = helfer_aufruf("auftrag", job["id"])
        self.assertNotIn(fertig["geheim"]["Root-Passwort"], mit_log["protokoll"])
        self.assertIn("ip=192.168.10.70/24,gw=192.168.10.1,tag=10", mit_log["protokoll"])
        rc, ueb = helfer_aufruf("uebersicht")
        neu = next(g for g in ueb["gaeste"] if g["name"] == "neuer-ct")
        self.assertEqual((neu["id"], neu["status"], neu["onboot"]), (fertig["gast"], "stopped", False))
        self.assertFalse(any("geheim" in a for a in ueb["auftraege"]))  # Passwort nie in der Liste

    def test_eigene_pruefung(self):
        for args in (["120", "file:///etc/shadow"], ["120", "http://x/ ; id"], ["200", "http://127.0.0.1/"]):
            rc, d = helfer_aufruf("pruefung", *args)
            self.assertEqual(rc, 1, args)
        rc, d = helfer_aufruf("pruefung", "120", "http://127.0.0.1:2019/config/")
        self.assertEqual(rc, 0, d)
        self.assertEqual([(p["app"], p["ok"]) for p in helfer.probe(120)["pruefungen"]], [("eigen", False)])
        rc, ueb = helfer_aufruf("uebersicht")
        self.assertEqual(next(g for g in ueb["gaeste"] if g["id"] == 120)["pruefung_eigen"]["quelle"], "oberflaeche")
        helfer_aufruf("pruefung", "120", "--aus")
        self.assertEqual(helfer.probe(120)["pruefungen"], [])
        # helfer.conf hat Vorrang und lässt sich über die Oberfläche nicht ändern
        konf = ORDNER / "helfer.conf"
        vorher = konf.read_text()
        konf.write_text(vorher + "PRUEFUNG_102=http://127.0.0.1:3000/healthz\n")
        try:
            rc, d = helfer_aufruf("pruefung", "102", "http://127.0.0.1/")
            self.assertEqual(rc, 1)
            self.assertIn("helfer.conf", d["fehler"])
        finally:
            konf.write_text(vorher)

    def test_dienst_und_strom(self):
        rc, job = helfer_aufruf("dienst", "104", "eventcrew-worker.service")
        self.assertEqual(warte_auf_auftrag(job["id"])["status"], "ok")
        rc, job = helfer_aufruf("start", "200")
        self.assertEqual(warte_auf_auftrag(job["id"])["status"], "ok")


GEHEIM = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def zf_von(name):
    return (ecspve.benutzer(name) or {}).get("zf")


def kommandozeile(*args, eingabe=None):
    p = subprocess.run([sys.executable, str(PROJEKT / "ecspve.py"), *args], capture_output=True, text=True,
                       input=eingabe, timeout=60)
    return p.returncode, p.stdout + p.stderr


def konfig(**werte):
    """ecspve.env für einen Test umschreiben (Grundstand aus fake.py plus Werte)."""
    zeilen = ["PVE_HOST=192.168.10.5", "PORT=8080", "TOKEN=test-token-123"] + [f"{k}={v}" for k, v in werte.items()]
    (ORDNER / "conf" / "ecspve.env").write_text("\n".join(zeilen) + "\n")
    ecspve._konf_cache = (0.0, None)


class Weboberflaeche(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.HASH = ecspve.passwort_hash("richtig-richtig")
        cls.sammler = ecspve.Sammler()
        cls.sammler.start()
        ecspve.Anfrage.sammler, ecspve.Anfrage.anmeldung = cls.sammler, ecspve.Anmeldung()
        cls.server = ecspve.Webserver(("0.0.0.0", 0), ecspve.Anfrage)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        ende = time.time() + 60
        while time.time() < ende and not (cls.sammler.ueb and cls.sammler.proben):
            time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        # Jeder Test mit frischen Sperren, eingerichtetem zweiten Faktor und Grundeinstellungen
        ecspve.Anfrage.anmeldung = ecspve.Anmeldung()
        with ecspve.benutzer_aendern() as d:
            d["benutzer"] = {"admin": {
                "rolle": "admin", "hash": self.HASH, "aktiv": True, "erstellt": 1.0, "muss_aendern": False,
                "zf": {"geheim": GEHEIM, "seit": 1.0, "letzter_schritt": 0, "notfall": [ecspve.notfall_hash("abcde23456")]}}}
        konfig()

    def naechster_code(self, name="admin", geheim=GEHEIM):
        """Ein gültiger, noch nicht benutzter Code (jeder Code gilt bei der Anmeldung nur einmal)."""
        while True:
            jetzt = int(time.time() // 30)
            schritt = max((zf_von(name) or {}).get("letzter_schritt", 0) + 1, jetzt - 1)
            if schritt <= jetzt + 1:
                return ecspve.totp(geheim, schritt)
            time.sleep(1)

    def ruhig(self):
        """Warten, bis kein Auftrag mehr läuft – Gäste mit laufendem Auftrag melden keine Störungen."""
        ende = time.time() + 30
        while time.time() < ende:
            if not any(a["status"] in ("wartet", "laeuft") for a in self.sammler.status()["auftraege"]):
                return
            self.sammler.wecker.set()
            time.sleep(0.5)
        raise AssertionError("Aufträge laufen noch")

    def anfrage(self, methode, pfad, daten=None, kopf=None, von="127.0.0.1"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30, source_address=(von, 0))
        kopf = dict(kopf or {})
        body = None
        if daten is not None:
            body = json.dumps(daten)
            kopf.setdefault("Content-Type", "application/json")
        c.request(methode, pfad, body=body, headers=kopf)
        r = c.getresponse()
        return r.status, r.read().decode(), dict(r.getheaders())

    def anmelden(self, name="admin", passwort="richtig-richtig", geheim=GEHEIM):
        status, text, kopf = self.anfrage("POST", "/api/anmelden", {"benutzer": name, "passwort": passwort}, {"X-ECSPVE": "1"})
        self.assertEqual((status, json.loads(text)["schritt"]), (200, "code"))
        vorstufe = kopf["Set-Cookie"].split(";")[0]
        self.assertEqual(self.anfrage("GET", "/api/status", kopf={"Cookie": vorstufe})[0], 401)  # Passwort allein reicht nicht
        status, text, kopf = self.anfrage("POST", "/api/anmelden/code", {"code": self.naechster_code(name, geheim)},
                                          {"X-ECSPVE": "1", "Cookie": vorstufe})
        self.assertEqual(status, 200, text)
        cookie = kopf["Set-Cookie"].split(";")[0]
        self.assertNotEqual(cookie, vorstufe)  # neues Token nach dem zweiten Faktor
        return cookie

    def test_passwort(self):
        self.assertTrue(ecspve.anmeldung_pruefen("admin", "richtig-richtig"))
        self.assertIsNone(ecspve.anmeldung_pruefen("admin", "falsch"))
        self.assertIsNone(ecspve.anmeldung_pruefen("gibtsnicht", "richtig-richtig"))

    def test_ohne_anmeldung(self):
        self.assertEqual(self.anfrage("GET", "/api/status")[0], 401)
        self.assertEqual(self.anfrage("GET", "/metrics")[0], 401)
        self.assertEqual(self.anfrage("GET", "/")[0], 200)
        self.assertEqual(self.anfrage("GET", "/ping")[1], "ok\n")
        # POST ohne eigenen Header (fremdes Formular) wird abgelehnt
        self.assertEqual(self.anfrage("POST", "/api/anmelden", {"benutzer": "admin", "passwort": "richtig-richtig"})[0], 403)
        self.assertEqual(self.anfrage("POST", "/api/aktion", {"id": 101, "aktion": "start"}, {"X-ECSPVE": "1"})[0], 401)

    def test_health(self):
        self.ruhig()
        status, text, _ = self.anfrage("GET", "/health")
        self.assertEqual(status, 503)
        self.assertIn("1 Probleme", text)
        self.assertNotIn("eventcrew", text)  # ohne Token keine Details
        status, text, _ = self.anfrage("GET", "/health", kopf={"Authorization": "Bearer test-token-123"})
        self.assertIn("eventcrew-worker.service ist ausgefallen", text)

    def test_monitoring_mit_token(self):
        self.ruhig()
        status, text, _ = self.anfrage("GET", "/metrics", kopf={"Authorization": "Bearer test-token-123"})
        self.assertEqual(status, 200)
        self.assertIn('ecspve_dienst_ok{id="104",name="eventcrew",typ="lxc",dienst="eventcrew-worker.service"} 0', text)
        self.assertIn('ecspve_gast_updates{id="101",name="ecswebsite",typ="lxc"} 3', text)
        self.assertIn("ecspve_ok 0", text)
        for zeile in text.splitlines():
            self.assertRegex(zeile, r"^(# (HELP|TYPE) ecspve_\w+ .+|ecspve_\w+(\{[^}]*\})? -?[0-9.e+]+)$")
        status, text, _ = self.anfrage("GET", "/api/status", kopf={"Authorization": "Bearer test-token-123"})
        daten = json.loads(text)
        self.assertFalse(daten["ok"])
        self.assertNotIn("schnittstellen", daten)  # Token nur für angemeldete Benutzer
        self.assertNotIn("sicherheit", daten)
        self.assertEqual(self.anfrage("GET", "/metrics", kopf={"Authorization": "Bearer falsch"})[0], 401)
        # Token in der Adresse nur, wenn ausdrücklich erlaubt
        self.assertEqual(self.anfrage("GET", "/api/status?token=test-token-123")[0], 401)
        konfig(TOKEN_IN_URL="ja")
        self.assertEqual(self.anfrage("GET", "/api/status?token=test-token-123")[0], 200)

    def test_bedienung(self):
        cookie = self.anmelden()
        kopf = {"Cookie": cookie, "X-ECSPVE": "1"}
        status, text, _ = self.anfrage("GET", "/api/status", kopf={"Cookie": cookie})
        daten = json.loads(text)
        self.assertEqual(daten["schnittstellen"]["token"], "test-token-123")
        web = next(g for g in daten["gaeste"] if g["id"] == 101)
        self.assertEqual(web["probe"]["app_name"], "Website")
        self.assertNotIn(9000, [g["id"] for g in daten["gaeste"]])  # Vorlagen nicht zeigen
        # Nachts automatisch
        self.assertEqual(self.anfrage("POST", "/api/auto", {"id": 101, "system": True, "app": False}, kopf)[0], 200)
        self.assertEqual(ecspve.lade_einstellungen()["101"]["system"], True)
        self.assertEqual(self.anfrage("POST", "/api/auto", {"id": 200, "system": True}, kopf)[0], 409)  # VM
        # Dienst, den es im Container nicht gibt
        fremd = {"id": 104, "aktion": "dienst", "dienst": "sshd.service"}
        self.assertEqual(self.anfrage("POST", "/api/aktion", fremd, kopf)[0], 400)
        self.assertEqual(self.anfrage("POST", "/api/aktion", {"id": 104, "aktion": "loeschen"}, kopf)[0], 400)
        status, text, _ = self.anfrage("POST", "/api/aktion", {"id": 104, "aktion": "dienst",
                                                                "dienst": "eventcrew-worker.service"}, kopf)
        self.assertEqual(status, 200, text)
        job = json.loads(text)
        status, text, _ = self.anfrage("GET", f"/api/auftrag/{job['id']}", kopf={"Cookie": cookie})
        self.assertEqual(json.loads(text)["gast"], 104)
        warte_auf_auftrag(job["id"])
        # Container anlegen und eigene Prüfung über die Oberfläche
        status, text, _ = self.anfrage("GET", "/api/anlegen", kopf={"Cookie": cookie})
        self.assertEqual(json.loads(text)["bruecken"], ["vmbr0", "vmbr1"])
        status, text, _ = self.anfrage("POST", "/api/anlegen", {"name": "Mit Leerzeichen"}, kopf)
        self.assertEqual(status, 409)
        self.assertIn("Kleinbuchstaben", json.loads(text)["fehler"])
        status, text, _ = self.anfrage("POST", "/api/anlegen", {"name": "web-neu", "cores": "2", "memory": "2048",
                                                                 "ip": "dhcp", "onboot": False, "start": False}, kopf)
        self.assertEqual(status, 200, text)
        self.assertEqual(warte_auf_auftrag(json.loads(text)["id"], 90)["status"], "ok")
        status, text, _ = self.anfrage("POST", "/api/pruefung", {"id": 120, "adresse": "ftp://x/"}, kopf)
        self.assertEqual(status, 409)
        self.assertEqual(self.anfrage("POST", "/api/pruefung", {"id": 120, "adresse": "http://127.0.0.1:2019/"}, kopf)[0], 200)
        self.assertEqual(self.anfrage("POST", "/api/pruefung", {"id": 120, "adresse": ""}, kopf)[0], 200)
        # Abmelden beendet die Sitzung
        self.anfrage("POST", "/api/abmelden", {}, kopf)
        self.assertEqual(self.anfrage("GET", "/api/status", kopf={"Cookie": cookie})[0], 401)

    def test_anmeldesperre(self):
        ip = "203.0.113.7"
        a = ecspve.Anmeldung()
        for _ in range(5):
            self.assertIsNone(a.gesperrt(ip))
            a.fehlversuch(ip)
        self.assertEqual(a.gesperrt(ip), "ip")
        self.assertIsNone(a.gesperrt("203.0.113.8"))
        for i in range(30):  # viele Adressen zusammen sperren alle
            a.fehlversuch(f"198.51.100.{i}")
        self.assertEqual(a.gesperrt("203.0.113.8"), "gesamt")

    def test_totp_nach_rfc_6238(self):
        geheim = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # „12345678901234567890“
        for zeit, code in ((59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")):
            self.assertEqual(ecspve.totp(geheim, zeit // 30), code)
        self.assertEqual(ecspve.totp_schritt(geheim, "287082", jetzt=59 + 30), 1)  # 30 Sekunden Abweichung
        self.assertIsNone(ecspve.totp_schritt(geheim, "287082", jetzt=59 + 90))

    def test_erste_anmeldung_richtet_zweiten_faktor_ein(self):
        with ecspve.benutzer_aendern() as d:
            d["benutzer"]["admin"]["zf"] = None
        status, text, kopf = self.anfrage("POST", "/api/anmelden", {"benutzer": "admin", "passwort": "richtig-richtig"}, {"X-ECSPVE": "1"})
        self.assertEqual(json.loads(text)["schritt"], "einrichten")
        vorstufe = {"X-ECSPVE": "1", "Cookie": kopf["Set-Cookie"].split(";")[0]}
        self.assertEqual(self.anfrage("GET", "/api/status", kopf=vorstufe)[0], 401)
        status, text, _ = self.anfrage("POST", "/api/zwei-faktor/start", {}, vorstufe)
        d = json.loads(text)
        geheim = d["geheim"].replace(" ", "")
        self.assertTrue(d["uri"].startswith("otpauth://totp/") and f"secret={geheim}" in d["uri"])
        self.assertTrue(d["qr"].startswith("data:image/svg+xml;base64,"))
        self.assertEqual(self.anfrage("POST", "/api/zwei-faktor/bestaetigen", {"code": "000000"}, vorstufe)[0], 403)
        code = ecspve.totp(geheim, int(time.time() // 30))
        status, text, kopf = self.anfrage("POST", "/api/zwei-faktor/bestaetigen", {"code": code}, vorstufe)
        self.assertEqual(status, 200, text)
        codes = json.loads(text)["notfallcodes"]
        self.assertEqual(len(codes), 8)
        self.assertRegex(codes[0], r"^[a-z2-9]{5}-[a-z2-9]{5}$")
        cookie = kopf["Set-Cookie"].split(";")[0]
        self.assertEqual(self.anfrage("GET", "/api/status", kopf={"Cookie": cookie})[0], 200)
        self.assertNotIn(geheim, ecspve.SICHERHEIT_FILE.read_text())
        self.assertEqual(oct(ecspve.BENUTZER_FILE.stat().st_mode & 0o777), "0o600")
        # Steht der zweite Faktor einmal, gibt es ohne Code kein neues Geheimnis mehr
        status, text, kopf = self.anfrage("POST", "/api/anmelden", {"benutzer": "admin", "passwort": "richtig-richtig"}, {"X-ECSPVE": "1"})
        self.assertEqual(json.loads(text)["schritt"], "code")
        vorstufe = {"X-ECSPVE": "1", "Cookie": kopf["Set-Cookie"].split(";")[0]}
        self.assertEqual(self.anfrage("POST", "/api/zwei-faktor/start", {}, vorstufe)[0], 403)

    def test_code_nur_einmal_und_notfallcode(self):
        status, text, kopf = self.anfrage("POST", "/api/anmelden", {"benutzer": "admin", "passwort": "richtig-richtig"}, {"X-ECSPVE": "1"})
        vorstufe = {"X-ECSPVE": "1", "Cookie": kopf["Set-Cookie"].split(";")[0]}
        self.assertEqual(self.anfrage("POST", "/api/anmelden/code", {"code": "123"}, vorstufe)[0], 403)
        code = self.naechster_code()
        self.assertEqual(self.anfrage("POST", "/api/anmelden/code", {"code": code}, vorstufe)[0], 200)
        # Derselbe Code ein zweites Mal (mitgelesen): abgelehnt
        status, text, kopf = self.anfrage("POST", "/api/anmelden", {"benutzer": "admin", "passwort": "richtig-richtig"}, {"X-ECSPVE": "1"})
        vorstufe = {"X-ECSPVE": "1", "Cookie": kopf["Set-Cookie"].split(";")[0]}
        self.assertEqual(self.anfrage("POST", "/api/anmelden/code", {"code": code}, vorstufe)[0], 403)
        # Notfallcode (Schreibweise egal) – genau einmal
        status, text, _ = self.anfrage("POST", "/api/anmelden/code", {"code": "ABCDE-23456"}, vorstufe)
        self.assertEqual((status, json.loads(text)["notfall"], json.loads(text)["notfall_uebrig"]), (200, True, 0))
        status, text, kopf = self.anfrage("POST", "/api/anmelden", {"benutzer": "admin", "passwort": "richtig-richtig"}, {"X-ECSPVE": "1"})
        vorstufe = {"X-ECSPVE": "1", "Cookie": kopf["Set-Cookie"].split(";")[0]}
        self.assertEqual(self.anfrage("POST", "/api/anmelden/code", {"code": "abcde23456"}, vorstufe)[0], 403)

    def test_sperre_nach_fehlversuchen(self):
        for _ in range(5):
            self.assertEqual(self.anfrage("POST", "/api/anmelden", {"benutzer": "admin", "passwort": "falsch"}, {"X-ECSPVE": "1"})[0], 403)
        status, text, _ = self.anfrage("POST", "/api/anmelden", {"benutzer": "admin", "passwort": "richtig-richtig"}, {"X-ECSPVE": "1"})
        self.assertEqual(status, 429)  # auch mit richtigem Passwort
        self.assertEqual(self.anfrage("GET", "/api/status", kopf={"Authorization": "Bearer test-token-123"})[0], 429)
        self.assertEqual(self.anfrage("GET", "/ping")[0], 200)
        self.assertEqual(self.anfrage("GET", "/")[0], 200)
        texte = [e["text"] for e in ecspve.letzte_ereignisse(10)]
        self.assertTrue(any("gesperrt" in t for t in texte), texte)

    def test_falsche_codes_von_ueberall_sperren(self):
        for i in range(10):
            vom_proxy = {"X-ECSPVE": "1", "X-Forwarded-Proto": "https", "X-Forwarded-For": f"198.51.100.{i}"}
            konfig(PROXY_IP="127.0.0.3")
            status, text, kopf = self.anfrage("POST", "/api/anmelden", {"benutzer": "admin", "passwort": "richtig-richtig"}, vom_proxy,
                                              von="127.0.0.3")
            vom_proxy["Cookie"] = kopf["Set-Cookie"].split(";")[0]
            self.assertEqual(self.anfrage("POST", "/api/anmelden/code", {"code": "000000"}, vom_proxy,
                                          von="127.0.0.3")[0], 403)
        # Jetzt hilft auch der richtige Code nicht mehr – egal von welcher Adresse
        status, text, _ = self.anfrage("POST", "/api/anmelden/code", {"code": self.naechster_code()}, vom_proxy,
                                       von="127.0.0.3")
        self.assertEqual(status, 429)
        self.assertTrue(any("vermutlich bekannt" in e["text"] for e in ecspve.letzte_ereignisse(20)))

    def test_sitzung_verraet_nichts(self):
        self.assertEqual(json.loads(self.anfrage("GET", "/api/sitzung")[1]), {"stufe": None, "schritt": None})

    def test_sitzung_endet_bei_neuem_passwort(self):
        cookie = self.anmelden()
        self.assertEqual(self.anfrage("GET", "/api/status", kopf={"Cookie": cookie})[0], 200)
        time.sleep(0.01)
        with ecspve.benutzer_aendern() as d:
            d["benutzer"]["admin"]["hash"] = ecspve.passwort_hash("richtig-richtig")
        self.assertEqual(self.anfrage("GET", "/api/status", kopf={"Cookie": cookie})[0], 401)

    def test_zweiten_faktor_verwalten(self):
        cookie = self.anmelden()
        kopf = {"Cookie": cookie, "X-ECSPVE": "1"}
        self.assertEqual(self.anfrage("POST", "/api/zwei-faktor/geraet", {"code": "000000"}, kopf)[0], 403)
        status, text, _ = self.anfrage("POST", "/api/zwei-faktor/geraet", {"code": self.naechster_code()}, kopf)
        self.assertEqual(json.loads(text)["geheim"].replace(" ", ""), GEHEIM)
        status, text, _ = self.anfrage("POST", "/api/zwei-faktor/notfallcodes", {"code": self.naechster_code()}, kopf)
        self.assertEqual(len(json.loads(text)["notfallcodes"]), 8)
        st = json.loads(self.anfrage("GET", "/api/status", kopf={"Cookie": cookie})[1])["sicherheit"]
        self.assertEqual(st["notfall_uebrig"], 8)
        self.assertIn("Neue Notfallcodes erzeugt – die alten gelten nicht mehr", [e["text"] for e in st["ereignisse"]])
        # Neues Handy: danach gelten alle anderen Sitzungen nicht mehr
        zweite = self.anmelden()
        status, text, _ = self.anfrage("POST", "/api/zwei-faktor/start", {"code": self.naechster_code()}, kopf)
        neu = json.loads(text)["geheim"].replace(" ", "")
        status, text, antwort = self.anfrage("POST", "/api/zwei-faktor/bestaetigen",
                                             {"code": ecspve.totp(neu, int(time.time() // 30))}, kopf)
        self.assertEqual(status, 200, text)
        self.assertEqual(zf_von("admin")["geheim"], neu)
        self.assertEqual(self.anfrage("GET", "/api/status", kopf={"Cookie": zweite})[0], 401)
        neues_cookie = antwort["Set-Cookie"].split(";")[0]
        self.assertEqual(self.anfrage("GET", "/api/status", kopf={"Cookie": neues_cookie})[0], 200)

    def test_alle_anderen_abmelden(self):
        erste, zweite = self.anmelden(), self.anmelden()
        self.anfrage("POST", "/api/sitzungen/beenden", {}, {"Cookie": erste, "X-ECSPVE": "1"})
        self.assertEqual(self.anfrage("GET", "/api/status", kopf={"Cookie": zweite})[0], 401)
        self.assertEqual(self.anfrage("GET", "/api/status", kopf={"Cookie": erste})[0], 200)

    def test_fremde_herkunft_und_kopfzeilen(self):
        status, _, kopf = self.anfrage("GET", "/")
        for name in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
            self.assertIn(name, kopf)
        fremd = {"X-ECSPVE": "1", "Origin": "https://angreifer.example"}
        self.assertEqual(self.anfrage("POST", "/api/anmelden", {"benutzer": "admin", "passwort": "richtig-richtig"}, fremd)[0], 403)
        eigen = {"X-ECSPVE": "1", "Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self.anfrage("POST", "/api/anmelden", {"benutzer": "admin", "passwort": "richtig-richtig"}, eigen)[0], 200)

    def test_nur_ueber_den_proxy(self):
        konfig(PROXY_IP="127.0.0.3")
        # Direkt (nicht vom Proxy, nicht lokal): nur /ping und /health
        self.assertEqual(self.anfrage("GET", "/", von="127.0.0.2")[0], 403)
        self.assertEqual(self.anfrage("GET", "/api/sitzung", von="127.0.0.2")[0], 403)
        self.assertIn(self.anfrage("GET", "/health", von="127.0.0.2")[0], (200, 503))
        # Vom Proxy, aber ohne HTTPS: keine Anmeldung
        status, text, _ = self.anfrage("POST", "/api/anmelden", {"benutzer": "admin", "passwort": "richtig-richtig"},
                                       {"X-ECSPVE": "1", "X-Forwarded-For": "192.0.2.10"}, von="127.0.0.3")
        self.assertEqual(status, 403)
        self.assertIn("HTTPS", json.loads(text)["fehler"])
        # Vom Proxy mit HTTPS: Cookie mit Secure, HSTS, Sperre trifft die echte Besucher-IP
        https = {"X-ECSPVE": "1", "X-Forwarded-Proto": "https", "X-Forwarded-For": "192.0.2.10"}
        status, _, kopf = self.anfrage("POST", "/api/anmelden", {"benutzer": "admin", "passwort": "richtig-richtig"}, https, von="127.0.0.3")
        self.assertEqual(status, 200)
        self.assertIn("Secure", kopf["Set-Cookie"])
        self.assertIn("Strict-Transport-Security", kopf)
        for _ in range(5):
            self.anfrage("POST", "/api/anmelden", {"benutzer": "admin", "passwort": "falsch"}, https, von="127.0.0.3")
        self.assertEqual(self.anfrage("POST", "/api/anmelden", {"benutzer": "admin", "passwort": "x"}, https, von="127.0.0.3")[0], 429)
        anderer = dict(https, **{"X-Forwarded-For": "192.0.2.11"})
        self.assertEqual(self.anfrage("POST", "/api/anmelden", {"benutzer": "admin", "passwort": "richtig-richtig"}, anderer,
                                      von="127.0.0.3")[0], 200)


    def test_benutzer_und_rollen(self):
        admin = {"Cookie": self.anmelden(), "X-ECSPVE": "1"}
        # Benutzer verwalten nur mit frisch bestätigtem Code
        status, text, _ = self.anfrage("POST", "/api/benutzer/anlegen", {"name": "max", "rolle": "lesen"}, admin)
        self.assertEqual((status, json.loads(text).get("code_noetig")), (403, True))
        self.assertEqual(self.anfrage("POST", "/api/benutzer/freigabe", {"code": "000000"}, admin)[0], 403)
        self.assertEqual(self.anfrage("POST", "/api/benutzer/freigabe", {"code": self.naechster_code()}, admin)[0], 200)
        self.assertEqual(self.anfrage("POST", "/api/benutzer/anlegen", {"name": "Max Muster"}, admin)[0], 403)
        status, text, _ = self.anfrage("POST", "/api/benutzer/anlegen", {"name": "max", "rolle": "lesen"}, admin)
        start = json.loads(text)["passwort"]
        self.assertEqual(self.anfrage("POST", "/api/benutzer/anlegen", {"name": "max"}, admin)[0], 403)  # gibt es schon
        # Erste Anmeldung: Startpasswort ersetzen, dann eigenen zweiten Faktor einrichten
        status, text, kopf = self.anfrage("POST", "/api/anmelden", {"benutzer": "max", "passwort": start}, {"X-ECSPVE": "1"})
        self.assertEqual(json.loads(text)["schritt"], "neues-passwort")
        vorstufe = {"X-ECSPVE": "1", "Cookie": kopf["Set-Cookie"].split(";")[0]}
        self.assertEqual(self.anfrage("POST", "/api/zwei-faktor/start", {}, vorstufe)[0], 403)
        self.assertEqual(self.anfrage("POST", "/api/anmelden/neues-passwort", {"passwort": start}, vorstufe)[0], 400)
        self.assertEqual(self.anfrage("POST", "/api/anmelden/neues-passwort", {"passwort": "kurz"}, vorstufe)[0], 400)
        status, text, kopf = self.anfrage("POST", "/api/anmelden/neues-passwort", {"passwort": "max-eigenes-passwort"},
                                          vorstufe)
        self.assertEqual(json.loads(text)["schritt"], "einrichten")
        vorstufe = {"X-ECSPVE": "1", "Cookie": kopf["Set-Cookie"].split(";")[0]}
        geheim = json.loads(self.anfrage("POST", "/api/zwei-faktor/start", {}, vorstufe)[1])["geheim"].replace(" ", "")
        self.assertNotEqual(geheim, GEHEIM)  # jeder hat seinen eigenen
        status, text, kopf = self.anfrage("POST", "/api/zwei-faktor/bestaetigen",
                                          {"code": ecspve.totp(geheim, int(time.time() // 30))}, vorstufe)
        self.assertEqual(status, 200, text)
        max_ = {"X-ECSPVE": "1", "Cookie": kopf["Set-Cookie"].split(";")[0]}
        st = json.loads(self.anfrage("GET", "/api/status", kopf=max_)[1])
        self.assertEqual(st["ich"]["rolle"], "lesen")
        self.assertNotIn("benutzer", st)
        self.assertTrue(all(e["wer"] == "max" for e in st["sicherheit"]["ereignisse"]))
        # Nur ansehen: keine Aktionen, keine Verwaltung
        self.assertEqual(self.anfrage("POST", "/api/aktion", {"id": 101, "aktion": "neustart"}, max_)[0], 403)
        self.assertEqual(self.anfrage("POST", "/api/benutzer/anlegen", {"name": "x"}, max_)[0], 403)
        self.assertNotEqual(self.anfrage("GET", "/api/anlegen", kopf=max_)[0], 200)
        self.assertEqual(self.anfrage("POST", "/api/pruefen", {}, max_)[0], 200)
        # Rolle ändern wirkt sofort, ohne neue Anmeldung
        self.assertEqual(self.anfrage("POST", "/api/benutzer/aendern", {"name": "max", "rolle": "bedienen"}, admin)[0], 200)
        self.assertEqual(self.anfrage("POST", "/api/aktion", {"id": 101, "aktion": "gibtsnicht"}, max_)[0], 400)
        # Admin sieht alle, kann sich selbst nicht herabstufen
        namen = [b["name"] for b in json.loads(self.anfrage("GET", "/api/status", kopf=admin)[1])["benutzer"]]
        self.assertEqual(namen, ["admin", "max"])
        self.assertEqual(self.anfrage("POST", "/api/benutzer/aendern", {"name": "admin", "rolle": "lesen"}, admin)[0], 403)
        # Sperren beendet die Sitzung sofort, Löschen entfernt den Zugang
        self.assertEqual(self.anfrage("POST", "/api/benutzer/aendern", {"name": "max", "aktiv": False}, admin)[0], 200)
        self.assertEqual(self.anfrage("GET", "/api/status", kopf=max_)[0], 401)
        status, text, _ = self.anfrage("POST", "/api/anmelden", {"benutzer": "max", "passwort": "max-eigenes-passwort"},
                                       {"X-ECSPVE": "1"})
        self.assertIn("gesperrt", json.loads(text)["fehler"])
        self.assertEqual(self.anfrage("POST", "/api/benutzer/loeschen", {"name": "max"}, admin)[0], 200)
        self.assertIsNone(ecspve.benutzer("max"))
        texte = [(e.get("wer"), e["text"]) for e in ecspve.letzte_ereignisse(40)]
        self.assertIn(("admin", "Benutzer „max“ angelegt (Nur ansehen)"), texte)

    def test_admin_passwort_und_zwei_faktor_zuruecksetzen(self):
        with ecspve.benutzer_aendern() as d:
            d["benutzer"]["eva"] = {"rolle": "bedienen", "hash": self.HASH, "aktiv": True, "muss_aendern": False,
                                    "zf": {"geheim": GEHEIM, "seit": 2.0, "letzter_schritt": 0, "notfall": []}}
        admin = {"Cookie": self.anmelden(), "X-ECSPVE": "1"}
        eva = {"Cookie": self.anmelden("eva"), "X-ECSPVE": "1"}
        self.anfrage("POST", "/api/benutzer/freigabe", {"code": self.naechster_code()}, admin)
        self.assertEqual(self.anfrage("POST", "/api/benutzer/zwei-faktor", {"name": "eva"}, admin)[0], 200)
        self.assertEqual(self.anfrage("GET", "/api/status", kopf=eva)[0], 401)
        status, text, _ = self.anfrage("POST", "/api/anmelden", {"benutzer": "eva", "passwort": "richtig-richtig"},
                                       {"X-ECSPVE": "1"})
        self.assertEqual(json.loads(text)["schritt"], "einrichten")
        status, text, _ = self.anfrage("POST", "/api/benutzer/passwort", {"name": "eva"}, admin)
        neu = json.loads(text)["passwort"]
        self.assertEqual(len(neu), 16)
        self.assertIsNone(ecspve.anmeldung_pruefen("eva", "richtig-richtig"))
        self.assertTrue(ecspve.benutzer("eva")["muss_aendern"])

    def test_eigenes_passwort_aendern(self):
        erste, zweite = self.anmelden(), self.anmelden()
        kopf = {"Cookie": erste, "X-ECSPVE": "1"}
        self.assertEqual(self.anfrage("POST", "/api/konto/passwort", {"alt": "falsch", "neu": "x" * 12}, kopf)[0], 403)
        self.assertEqual(self.anfrage("POST", "/api/konto/passwort", {"alt": "richtig-richtig", "neu": "kurz"}, kopf)[0], 403)
        status, _, antwort = self.anfrage("POST", "/api/konto/passwort",
                                          {"alt": "richtig-richtig", "neu": "ganz-neues-passwort"}, kopf)
        self.assertEqual(status, 200)
        self.assertEqual(self.anfrage("GET", "/api/status", kopf={"Cookie": zweite})[0], 401)
        neues = antwort["Set-Cookie"].split(";")[0]
        self.assertEqual(self.anfrage("GET", "/api/status", kopf={"Cookie": neues})[0], 200)
        self.assertTrue(ecspve.anmeldung_pruefen("admin", "ganz-neues-passwort"))

    def test_bisheriger_zugang_wird_admin(self):
        ecspve.BENUTZER_FILE.unlink()
        ecspve.PASS_FILE.write_text(self.HASH + "\n")
        ecspve.ZF_FILE.write_text(json.dumps({"geheim": GEHEIM, "seit": 5.0, "letzter_schritt": 0, "notfall": []}))
        try:
            u = ecspve.benutzer("admin")
            self.assertEqual((u["rolle"], u["zf"]["geheim"]), ("admin", GEHEIM))
            self.assertTrue(ecspve.anmeldung_pruefen("admin", "richtig-richtig"))
        finally:
            ecspve.PASS_FILE.unlink()
            ecspve.ZF_FILE.unlink()

    def test_kommandozeile(self):
        rc, aus = kommandozeile("benutzer", "anlegen", "bob", "--rolle=lesen")
        self.assertEqual(rc, 0, aus)
        start = re.search(r"Startpasswort: (\S+)", aus).group(1)
        self.assertTrue(ecspve.anmeldung_pruefen("bob", start))
        self.assertIn("bob", kommandozeile("benutzer")[1])
        rc, aus = kommandozeile("benutzer", "rolle", "admin", "lesen")
        self.assertNotEqual(rc, 0)
        self.assertIn("mindestens einen aktiven Admin", aus)
        self.assertEqual(kommandozeile("passwort", "--benutzer=bob", "--stdin", eingabe="bobs-neues-passwort\n")[0], 0)
        self.assertTrue(ecspve.anmeldung_pruefen("bob", "bobs-neues-passwort"))
        self.assertFalse(ecspve.benutzer("bob")["muss_aendern"])
        self.assertEqual(kommandozeile("benutzer", "loeschen", "bob")[0], 0)
        self.assertIsNone(ecspve.benutzer("bob"))


if __name__ == "__main__":
    unittest.main()
