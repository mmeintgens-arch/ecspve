"""Tests für Helfer und Weboberfläche – gegen den nachgebauten Proxmox-Host aus test/fake.py.

  python3 -m unittest discover -s test -v
"""

import http.client
import importlib.machinery
import importlib.util
import json
import os
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
                         [["pct", "exec", "102", "--", "turnierapp-update"]])
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

    def test_dienst_und_strom(self):
        rc, job = helfer_aufruf("dienst", "104", "eventcrew-worker.service")
        self.assertEqual(warte_auf_auftrag(job["id"])["status"], "ok")
        rc, job = helfer_aufruf("start", "200")
        self.assertEqual(warte_auf_auftrag(job["id"])["status"], "ok")


class Weboberflaeche(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ORDNER / "conf" / "passwort").write_text(ecspve.passwort_hash("richtig-richtig") + "\n")
        cls.sammler = ecspve.Sammler()
        cls.sammler.start()
        ecspve.Anfrage.sammler, ecspve.Anfrage.anmeldung = cls.sammler, ecspve.Anmeldung()
        cls.server = ecspve.Webserver(("127.0.0.1", 0), ecspve.Anfrage)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        ende = time.time() + 60
        while time.time() < ende and not (cls.sammler.ueb and cls.sammler.proben):
            time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def ruhig(self):
        """Warten, bis kein Auftrag mehr läuft – Gäste mit laufendem Auftrag melden keine Störungen."""
        ende = time.time() + 30
        while time.time() < ende:
            if not any(a["status"] in ("wartet", "laeuft") for a in self.sammler.status()["auftraege"]):
                return
            self.sammler.wecker.set()
            time.sleep(0.5)
        raise AssertionError("Aufträge laufen noch")

    def anfrage(self, methode, pfad, daten=None, kopf=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        kopf = dict(kopf or {})
        body = None
        if daten is not None:
            body = json.dumps(daten)
            kopf.setdefault("Content-Type", "application/json")
        c.request(methode, pfad, body=body, headers=kopf)
        r = c.getresponse()
        return r.status, r.read().decode(), dict(r.getheaders())

    def anmelden(self):
        status, _, kopf = self.anfrage("POST", "/api/anmelden", {"passwort": "richtig-richtig"}, {"X-ECSPVE": "1"})
        self.assertEqual(status, 200)
        return kopf["Set-Cookie"].split(";")[0]

    def test_passwort(self):
        self.assertTrue(ecspve.passwort_pruefen("richtig-richtig"))
        self.assertFalse(ecspve.passwort_pruefen("falsch"))

    def test_ohne_anmeldung(self):
        self.assertEqual(self.anfrage("GET", "/api/status")[0], 401)
        self.assertEqual(self.anfrage("GET", "/metrics")[0], 401)
        self.assertEqual(self.anfrage("GET", "/")[0], 200)
        self.assertEqual(self.anfrage("GET", "/ping")[1], "ok\n")
        # POST ohne eigenen Header (fremdes Formular) wird abgelehnt
        self.assertEqual(self.anfrage("POST", "/api/anmelden", {"passwort": "richtig-richtig"})[0], 403)
        self.assertEqual(self.anfrage("POST", "/api/aktion", {"id": 101, "aktion": "start"}, {"X-ECSPVE": "1"})[0], 401)

    def test_health(self):
        self.ruhig()
        status, text, _ = self.anfrage("GET", "/health")
        self.assertEqual(status, 503)
        self.assertIn("1 Probleme", text)
        self.assertNotIn("eventcrew", text)  # ohne Token keine Details
        status, text, _ = self.anfrage("GET", "/health?token=test-token-123")
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
        status, text, _ = self.anfrage("GET", "/api/status?token=test-token-123")
        daten = json.loads(text)
        self.assertFalse(daten["ok"])
        self.assertNotIn("schnittstellen", daten)  # Token nur für angemeldete Benutzer
        self.assertEqual(self.anfrage("GET", "/metrics", kopf={"Authorization": "Bearer falsch"})[0], 401)

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
        # Abmelden beendet die Sitzung
        self.anfrage("POST", "/api/abmelden", {}, kopf)
        self.assertEqual(self.anfrage("GET", "/api/status", kopf={"Cookie": cookie})[0], 401)

    def test_anmeldesperre(self):
        ip = "203.0.113.7"
        a = ecspve.Anmeldung()
        for _ in range(5):
            self.assertFalse(a.gesperrt(ip))
            a.fehlversuch(ip)
        self.assertTrue(a.gesperrt(ip))


if __name__ == "__main__":
    unittest.main()
