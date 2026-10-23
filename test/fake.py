#!/usr/bin/env python3
"""Nachgebauter Proxmox-Host für Tests und Vorschau – ohne echtes Proxmox.

  python3 test/fake.py ORDNER          legt die Attrappe in ORDNER an und gibt die Umgebungsvariablen aus
  python3 test/fake.py ORDNER --demo   startet zusätzlich die Weboberfläche (Passwort: demo-passwort)

Die Attrappe ersetzt pct, pvesh, qm, pveversion, systemctl und apt-get. „pct exec … sh -c SKRIPT“ führt das
echte Prüfskript des Helfers aus – mit nachgebauten Befehlen und Dateien je Container.
"""

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

PROJEKT = Path(__file__).resolve().parents[1]
HELFER = PROJEKT / "deploy" / "proxmox" / "ecspve-helfer"
DEMO_GEHEIM = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def einheit(name, aktiv="active", sub="running", typ="simple", datei="enabled"):
    return {"Id": name, "LoadState": "loaded", "ActiveState": aktiv, "SubState": sub, "Type": typ,
            "UnitFileState": datei}


GAESTE = {
    101: {"name": "ecswebsite", "tags": "ecswebsite", "mem": 0.42, "disk": 0.31, "cpu": 0.021, "uptime": 1_210_000,
          "os": "Debian GNU/Linux 13 (trixie)", "ip": "192.168.10.51", "updates": 3,
          "einheiten": [einheit("ecswebsite-web.service"), einheit("ecswebsite-worker.service"),
                        einheit("ecswebsite-nightly.service", "inactive", "dead", "oneshot", "static"),
                        einheit("ecswebsite-nightly.timer", "active", "waiting", "", "enabled"),
                        einheit("ecswebsite-tls.service", "inactive", "dead", "oneshot", "static"),
                        einheit("nginx.service")],
          "http": {"http://127.0.0.1:8000/healthz": "200"}},
    102: {"name": "turnierapp", "mem": 0.18, "disk": 0.22, "cpu": 0.004, "uptime": 1_209_000,
          "os": "Debian GNU/Linux 12 (bookworm)", "ip": "192.168.10.52", "updates": 0,
          "einheiten": [einheit("turnierapp.service"), einheit("caddy.service")],
          "http": {"http://127.0.0.1:3000/healthz": "200"}},
    103: {"name": "ecswiki", "tags": "ecswiki", "mem": 0.27, "disk": 0.40, "cpu": 0.003, "uptime": 1_208_000,
          "os": "Debian GNU/Linux 13 (trixie)", "ip": "192.168.10.53", "updates": 0, "neustart": True,
          "einheiten": [einheit("ecswiki.service"), einheit("caddy.service"),
                        einheit("ecswiki-sicherung.service", "inactive", "dead", "oneshot", "static"),
                        einheit("ecswiki-sicherung.timer", "active", "waiting", "", "enabled")],
          "http": {"http://127.0.0.1:3100/healthz": "200"}},
    104: {"name": "eventcrew", "mem": 0.61, "disk": 0.48, "cpu": 0.012, "uptime": 86_000,
          "os": "Debian GNU/Linux 12 (bookworm)", "ip": "192.168.10.54", "updates": 7,
          "einheiten": [einheit("eventcrew-web.service"),
                        einheit("eventcrew-worker.service", "failed", "failed")],
          "befehl_ok": True},
    105: {"name": "streetworkdoc", "tags": "streetworkdoc", "mem": 0.22, "disk": 0.12, "cpu": 0.002,
          "uptime": 604_000, "os": "Debian GNU/Linux 13 (trixie)", "ip": "192.168.10.55", "updates": 0,
          "einheiten": [einheit("streetworkdoc.service"),
                        einheit("streetworkdoc-backup.service", "inactive", "dead", "oneshot", "static"),
                        einheit("streetworkdoc-backup.timer", "active", "waiting", "", "enabled")],
          "http": {"https://127.0.0.1:8443/api/v1/info": "200"}},
    110: {"name": "ecspve", "tags": "ecspve", "mem": 0.09, "disk": 0.15, "cpu": 0.001, "uptime": 300_000,
          "os": "Debian GNU/Linux 13 (trixie)", "ip": "192.168.10.60", "updates": 0,
          "einheiten": [einheit("ecspve.service"),
                        einheit("ecspve-nacht.service", "inactive", "dead", "oneshot", "static"),
                        einheit("ecspve-nacht.timer", "active", "waiting", "", "enabled")],
          "befehl_ok": True},
    120: {"name": "caddy", "mem": 0.11, "disk": 0.20, "cpu": 0.002, "uptime": 1_210_500,
          "os": "Debian GNU/Linux 12 (bookworm)", "ip": "192.168.10.2", "updates": 2,
          "einheiten": [einheit("caddy.service")]},
    200: {"name": "testserver", "typ": "qemu", "status": "stopped"},
    9000: {"name": "debian-13-vorlage", "status": "stopped", "template": True},
}


def alle_gaeste(ordner):
    """Die festen Gäste plus die, die ein Test mit „pct create“ angelegt hat."""
    gaeste = dict(GAESTE)
    for gid, g in zustand_lesen(ordner).get("neu", {}).items():
        gaeste[int(gid)] = g
    return gaeste


def ressourcen(ordner):
    zustand = zustand_lesen(ordner)
    res = [{"type": "node", "node": "pve", "id": "node/pve", "status": "online", "cpu": 0.046, "maxcpu": 8,
            "mem": 9.1 * 2**30, "maxmem": 31.2 * 2**30, "disk": 18.4 * 2**30, "maxdisk": 94 * 2**30,
            "uptime": 1_211_000},
           {"type": "storage", "node": "pve", "storage": "local-lvm", "id": "storage/pve/local-lvm"}]
    for gid, g in alle_gaeste(ordner).items():
        typ = g.get("typ", "lxc")
        status = zustand.get(str(gid), g.get("status", "running"))
        maxmem = (2 if gid in (101, 104) else 1) * 2**30
        maxdisk = (16 if gid == 101 else 8 if gid == 104 else 4) * 2**30
        laeuft = status == "running"
        res.append({"type": typ, "vmid": gid, "id": f"{typ}/{gid}", "node": "pve", "name": g["name"],
                    "status": status, "template": 1 if g.get("template") else 0, "tags": g.get("tags", ""),
                    "cpu": g.get("cpu", 0) if laeuft else 0, "maxcpu": 2,
                    "mem": int(maxmem * g.get("mem", 0)) if laeuft else 0,
                    "maxmem": maxmem, "disk": int(maxdisk * g.get("disk", 0)) if typ == "lxc" else 0,
                    "maxdisk": maxdisk, "uptime": g.get("uptime", 0) if laeuft else 0})
    return res


# --- die nachgebauten Befehle ----------------------------------------------------------------------------

def zustand_lesen(ordner):
    try:
        return json.loads((ordner / "zustand.json").read_text())
    except (OSError, ValueError):
        return {}


def zustand_setzen(ordner, gid, status, neu=None):
    z = zustand_lesen(ordner)
    z[str(gid)] = status
    if neu:
        z.setdefault("neu", {})[str(gid)] = neu
    (ordner / "zustand.json").write_text(json.dumps(z))


def befehl(name, args, ordner):
    ct = os.environ.get("FAKE_CT")
    if ct:
        return im_container(name, args, alle_gaeste(ordner)[int(ct)])
    if name == "pvesh":
        pfad = args[1]
        if pfad == "/cluster/resources":
            print(json.dumps(ressourcen(ordner)))
        elif pfad == "/cluster/nextid":
            belegt = set(alle_gaeste(ordner))
            print(json.dumps(str(next(i for i in range(100, 10**6) if i not in belegt))))
        elif pfad == "/nodes/pve/storage":
            if "vztmpl" in args:
                print(json.dumps([{"storage": "local", "type": "dir", "active": 1, "avail": 60 * 2**30, "total": 94 * 2**30}]))
            else:
                print(json.dumps([{"storage": "local-lvm", "type": "lvmthin", "active": 1, "avail": 310 * 2**30,
                                   "total": 400 * 2**30},
                                  {"storage": "backup", "type": "nfs", "active": 0, "avail": 0, "total": 0}]))
        elif pfad == "/nodes/pve/network":
            print(json.dumps([{"iface": "vmbr0", "type": "bridge"}, {"iface": "vmbr1", "type": "bridge"}]))
        elif pfad.endswith("/apt/update"):
            print(json.dumps([{"Package": "pve-manager", "OldVersion": "8.4.1", "Version": "8.4.2"},
                              {"Package": "proxmox-kernel-6.8", "OldVersion": "6.8.12-9", "Version": "6.8.12-10"},
                              {"Package": "openssl", "OldVersion": "3.0.15-1~deb12u1", "Version": "3.0.16-1~deb12u1"}]))
        else:
            print(json.dumps(None))
        return 0
    if name == "pveversion":
        print("pve-manager/8.4.1/2a5fa54a8503f96d (running kernel: 6.8.12-9-pve)")
        return 0
    if name == "systemctl":
        if "show" in args and "ecspve-nacht.timer" in args:
            morgen = time.localtime(time.time() + 86400)
            print("@%d" % time.mktime((morgen.tm_year, morgen.tm_mon, morgen.tm_mday, 4, 37, 0, 0, 0, -1)))
        return 0
    if name == "apt-get":
        print("Paketlisten werden gelesen … Fertig\n(Attrappe) Pakete aktualisiert")
        return 0
    if name in ("pct", "qm"):
        return pct(name, args, ordner)
    if name == "pveam":
        if args[:1] == ["available"]:
            print("system          debian-12-standard_12.12-1_amd64.tar.zst\n"
                  "system          debian-13-standard_13.1-2_amd64.tar.zst\n"
                  "system          ubuntu-24.04-standard_24.04-2_amd64.tar.zst")
        elif args[:1] == ["list"]:
            print("NAME                                                         SIZE\n"
                  "local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst        120.30MB")
        else:
            print(f"(Attrappe) pveam {' '.join(args)}")
        return 0
    if name == "git":
        print("Already up to date.")
        return 0
    if name == "qrencode":
        print(qr_attrappe(args[-1]))
        return 0
    if name in ("pve-install.sh", "turnierapp-update-lxc", "create-lxc.sh"):
        print(f"(Attrappe) {name} {' '.join(args)}\n==> Fertig")
        return 0
    print(f"(Attrappe) unbekannter Befehl {name}", file=sys.stderr)
    return 1


def qr_attrappe(text):
    """Sieht aus wie ein QR-Code (für Vorschau und Tests), lässt sich aber nicht scannen."""
    import hashlib
    n, bits = 29, int(hashlib.sha256(text.encode()).hexdigest() * 4, 16)
    felder = []
    for y in range(n):
        for x in range(n):
            ecke = next(((ex, ey) for ex, ey in ((0, 0), (n - 7, 0), (0, n - 7)) if ex <= x < ex + 7 and ey <= y < ey + 7), None)
            if ecke:
                dx, dy = x - ecke[0], y - ecke[1]
                an = dx in (0, 6) or dy in (0, 6) or (2 <= dx <= 4 and 2 <= dy <= 4)
            else:
                an = (bits >> ((y * n + x) % 1000)) & 1
            if an:
                felder.append(f'<rect x="{x + 2}" y="{y + 2}" width="1" height="1"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n + 4} {n + 4}"><rect width="100%" height="100%" '
            f'fill="#fff"/><g fill="#000">{"".join(felder)}</g></svg>')


def pct(name, args, ordner):
    unter, gid = args[0], args[1] if len(args) > 1 else ""
    if unter == "create":
        return pct_create(int(gid), args, ordner)
    g = alle_gaeste(ordner).get(int(gid)) if gid.isdigit() else None
    if g is None:
        print(f"Configuration file 'nodes/pve/lxc/{gid}.conf' does not exist", file=sys.stderr)
        return 2
    if unter == "status":
        print(f"status: {zustand_lesen(ordner).get(gid, g.get('status', 'running'))}")
        return 0
    if unter in ("start", "shutdown", "reboot", "stop"):
        time.sleep(1)
        zustand_setzen(ordner, gid, "stopped" if unter in ("shutdown", "stop") else "running")
        return 0
    if unter in ("snapshot", "delsnapshot"):
        conf = ordner / "pve" / "nodes" / "pve" / "lxc" / f"{gid}.conf"
        text = conf.read_text()
        if unter == "snapshot":
            conf.write_text(text + f"\n[{args[2]}]\ndescription: Vor Update (ecspve)\n")
        else:
            teile = text.split(f"\n[{args[2]}]\n")
            if len(teile) == 2:
                rest = teile[1].split("\n[", 1)
                conf.write_text(teile[0] + ("\n[" + rest[1] if len(rest) == 2 else "\n"))
        return 0
    if unter == "exec":
        befehl_ct = args[args.index("--") + 1:]
        while befehl_ct and befehl_ct[0] == "env":  # env PATH=… [VAR=…] befehl
            befehl_ct = befehl_ct[1:]
            while befehl_ct and "=" in befehl_ct[0]:
                befehl_ct = befehl_ct[1:]
        if befehl_ct[:2] == ["sh", "-c"]:
            return pruefskript(int(gid), g, befehl_ct[2], ordner)
        if befehl_ct == ["chpasswd"]:
            sys.stdin.read()
            return 0
        if befehl_ct[0] in ("true", "getent"):
            return 0
        print(f"(Attrappe im Container {gid}) {' '.join(befehl_ct)}")
        time.sleep(0.5)
        return 0
    return 0


def pct_create(gid, args, ordner):
    opt = {args[i][2:]: args[i + 1] for i in range(3, len(args) - 1) if args[i].startswith("--")}
    if gid in alle_gaeste(ordner):
        print(f"CT {gid} already exists on node 'pve'", file=sys.stderr)
        return 255
    ip = re.search(r"ip=([0-9.]+)/", opt.get("net0", ""))
    neu = {"name": opt["hostname"], "mem": 0.04, "disk": 0.09, "cpu": 0.001, "uptime": 20,
           "os": "Debian GNU/Linux 13 (trixie)", "ip": ip.group(1) if ip else "192.168.10.77", "updates": 0,
           "einheiten": [], "status": "stopped"}
    zustand_setzen(ordner, gid, "stopped", neu)
    (ordner / "pve/nodes/pve/lxc" / f"{gid}.conf").write_text(
        f"hostname: {opt['hostname']}\nonboot: {opt.get('onboot', '0')}\nnet0: {opt.get('net0', '')}\n")
    wurzel = ordner / "ct" / str(gid)
    (wurzel / "systemd").mkdir(parents=True)
    (wurzel / "os-release").write_text(f'PRETTY_NAME="{neu["os"]}"\n')
    print(f"(Attrappe) Container {gid} aus {args[2]} angelegt")
    return 0


def pruefskript(gid, g, skript, ordner):
    """Führt das echte Prüfskript aus – mit Dateien und Befehlen des nachgebauten Containers."""
    wurzel = ordner / "ct" / str(gid)
    skript = (skript.replace("/etc/systemd/system", str(wurzel / "systemd"))
              .replace("/etc/os-release", str(wurzel / "os-release"))
              .replace("/run/reboot-required", str(wurzel / "reboot-required"))
              .replace("/run/ecspve-updates", str(wurzel / "updates-zwischenspeicher"))
              .replace("/usr/local/bin/ecspve", str(ordner / "ctbin" / "ecspve")))
    umgebung = {**os.environ, "FAKE_CT": str(gid), "PATH": f"{ordner / 'ctbin'}:{os.environ['PATH']}"}
    return os.spawnvpe(os.P_WAIT, "sh", ["sh", "-c", skript], umgebung)


def im_container(name, args, g):
    if name == "systemctl":
        if args[:1] == ["show"]:
            namen = [a for a in args[1:] if not a.startswith("--")]
            bloecke = []
            for n in namen:
                e = next((e for e in g.get("einheiten", []) if e["Id"] == n), None)
                e = e or {"Id": n, "LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead"}
                bloecke.append("\n".join(f"{k}={v}" for k, v in e.items()))
            print("\n\n".join(bloecke))
        elif "--failed" in args:
            for e in g.get("einheiten", []):
                if e["ActiveState"] == "failed":
                    print(f"{e['Id']} loaded failed failed Beschreibung")
        return 0
    if name == "apt-get":
        for i in range(g.get("updates", 0)):
            print(f"Inst paket{i} [1.0] (1.1 Debian:13.1/stable [amd64])")
        return 0
    if name == "hostname":
        print(g.get("ip", ""))
        return 0
    if name == "curl":
        print(g.get("http", {}).get(args[-1], "000"), end="")
        return 0
    if name in ("python3", "ecspve"):
        print("200")
        return 0 if g.get("befehl_ok") else 1
    return 0


# --- Aufbau ----------------------------------------------------------------------------------------------

def anlegen(ordner):
    ordner = Path(ordner).resolve()
    if ordner.exists():
        shutil.rmtree(ordner)
    for d in ("bin", "ctbin", "pve/nodes/pve/lxc", "pve/nodes/pve/qemu-server", "boot", "conf", "state", "helfer"):
        (ordner / d).mkdir(parents=True)
    starter = ordner / "fakecmd"
    starter.write_text(f"#!{sys.executable}\nimport os, sys\nsys.path.insert(0, {str(PROJEKT / 'test')!r})\n"
                       f"import fake\nfrom pathlib import Path\n"
                       f"sys.exit(fake.befehl(os.path.basename(sys.argv[0]), sys.argv[1:], Path({str(ordner)!r})))\n")
    starter.chmod(0o755)
    for n in ("pct", "pvesh", "qm", "pveam", "pveversion", "systemctl", "apt-get", "git", "qrencode", "pve-install.sh",
              "turnierapp-update-lxc", "create-lxc.sh"):
        (ordner / "bin" / n).symlink_to(starter)
    for n in ("systemctl", "apt-get", "hostname", "curl", "python3", "ecspve"):
        (ordner / "ctbin" / n).symlink_to(starter)
    for gid, g in GAESTE.items():
        art = "qemu-server" if g.get("typ") == "qemu" else "lxc"
        (ordner / "pve/nodes/pve" / art / f"{gid}.conf").write_text(
            f"hostname: {g['name']}\nonboot: {0 if g.get('status') == 'stopped' else 1}\n")
        wurzel = ordner / "ct" / str(gid) / "systemd"
        wurzel.mkdir(parents=True)
        for e in g.get("einheiten", []):
            if e["Id"] not in ("nginx.service", "caddy.service"):
                (wurzel / e["Id"]).write_text("[Unit]\n")
        (ordner / "ct" / str(gid) / "os-release").write_text(f'PRETTY_NAME="{g.get("os", "Debian")}"\n')
        if g.get("neustart"):
            (ordner / "ct" / str(gid) / "reboot-required").write_text("*** System restart required ***\n")
    (ordner / "boot" / "vmlinuz-6.8.12-9-pve").write_text("")
    # Werkzeuge der Apps auf dem „Host“ (für die App-Updates)
    for pfad in ("opt/ecswebsite/pve-install.sh", "opt/ecswiki/pve-install.sh", "opt/ecspve/pve-install.sh"):
        (ordner / pfad).parent.mkdir(parents=True, exist_ok=True)
        (ordner / pfad).symlink_to(starter)
    (ordner / "helfer.conf").write_text("# Attrappe\nHOST_UPDATE=ja\n")
    (ordner / "conf" / "ecspve.env").write_text("PVE_HOST=192.168.10.5\nPORT=8080\nTOKEN=test-token-123\n")
    return ordner


def umgebung(ordner):
    ordner = Path(ordner).resolve()
    return {
        "PATH": f"{ordner / 'bin'}:{os.environ['PATH']}", "ECSPVE_TEST": "1", "ECSPVE_OHNE_SYSTEMD": "1",
        "ECSPVE_PVE_DIR": str(ordner / "pve"), "ECSPVE_HELFER_DIR": str(ordner / "helfer"),
        "ECSPVE_HELFER_CACHE": str(ordner / "cache"), "ECSPVE_HELFER_KONF": str(ordner / "helfer.conf"),
        "ECSPVE_BOOT_DIR": str(ordner / "boot"), "ECSPVE_KNOTEN": "pve", "ECSPVE_KERNEL": "6.8.12-9-pve",
        "ECSPVE_CONF_DIR": str(ordner / "conf"),
        "ECSPVE_STATE_DIR": str(ordner / "state"), "ECSPVE_HELFER": str(HELFER), "ECSPVE_WURZEL": str(ordner),
    }


if __name__ == "__main__":
    ziel = anlegen(sys.argv[1])
    env = umgebung(ziel)
    if "--demo" in sys.argv:
        os.environ.update(env)
        sys.path.insert(0, str(PROJEKT))
        import ecspve
        (ziel / "conf" / "passwort").write_text(ecspve.passwort_hash("demo-passwort") + "\n")
        if "--erstanmeldung" not in sys.argv:
            # Zweiter Faktor mit festem Schlüssel – Code z. B.: python3 -c "import ecspve,time; print(ecspve.totp(…))"
            ecspve.zf_speichern({"geheim": DEMO_GEHEIM, "seit": time.time() - 86400 * 3, "letzter_schritt": 0,
                                 "notfall": [ecspve.notfall_hash(c) for c in ("demo2demo2", "demo3demo3", "demo4demo4")]})
            print(f"Demo: Passwort demo-passwort, Schlüssel für die Authenticator-App {DEMO_GEHEIM}", flush=True)
        ecspve.serve()
    else:
        for k, v in env.items():
            print(f"export {k}={v!r}")
