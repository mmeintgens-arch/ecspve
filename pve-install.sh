#!/usr/bin/env bash
# ECS Proxmox – Übersicht der Container und Dienste: Einrichtung und Updates auf dem PROXMOX-HOST
#
# Einmalig klonen (privates Repository: Deploy-Key wie bei Wiki und Website, Anleitung in der README):
#   git clone github-ecspve:mmeintgens-arch/ecspve.git /opt/ecspve
#
# Neu anlegen (eigener unprivilegierter Debian-LXC, nur HTTP – HTTPS macht euer Caddy davor):
#   /opt/ecspve/pve-install.sh                        Container mit DHCP, Weboberfläche auf Port 8080
#   /opt/ecspve/pve-install.sh --ip=192.168.10.60/24 --gw=192.168.10.1 --proxy-ip=192.168.10.2
#   --ctid=N  --hostname=NAME (ecspve)  --storage=NAME (erster mit „rootdir“)  --tmpl-storage=NAME (local)
#   --cores=N (1)  --memory=MB (512)  --disk=GB (4)  --bridge=NAME (vmbr0)  --vlan=N  --no-onboot
#   --ip=dhcp|CIDR (dhcp)  --gw=IP  --password=PW (Weboberfläche; sonst Abfrage bzw. zufällig, gespeichert
#   auf dem Host in /root/ecspve-ct<ID>-passwort.txt)
#
# Einstellungen der Übersicht (beim Anlegen und bei Updates):
#   --port=N             Port der Weboberfläche im Container (8080)
#   --proxy-ip=IP[,IP]   IP eures Reverse Proxys, damit die Anmeldesperre die echte Besucher-IP sieht
#                        (--proxy-ip= setzt zurück)
#   --host-ip=IP         Adresse dieses Proxmox-Hosts aus Sicht des Containers (Standard: IP der Bridge)
#
# Update (gleicher Befehl): Gibt es schon einen Container mit dem Tag „ecspve“, wird er aktualisiert – Einstellungen,
# Passwort, Token und Auswahl der automatischen Updates bleiben. Dabei wird auch der Helfer auf dem Host erneuert.
#   /opt/ecspve/pve-install.sh --pull                 neuesten Stand holen (bisheriger Branch) und einspielen
#   /opt/ecspve/pve-install.sh --pull --branch=NAME   auf einen anderen Branch wechseln
#   /opt/ecspve/pve-install.sh --update --ctid=110    bestimmten Container aktualisieren
#   /opt/ecspve/pve-install.sh --new                  trotzdem einen weiteren Container anlegen
#
# Was auf dem Host dazukommt: /usr/local/sbin/ecspve-helfer (die erlaubten Befehle), /etc/ecspve/helfer.conf und
# eine Zeile in /root/.ssh/authorized_keys, die den Schlüssel des Containers auf genau diesen Helfer festlegt
# (command="…",restrict – keine Shell, keine Weiterleitungen).

set -euo pipefail

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mHinweis:\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31mFehler:\033[0m %s\n' "$*" >&2; exit 1; }
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0; }

SRC="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
TAG=ecspve
ORIG_ARGS=("$@")
HELFER=/usr/local/sbin/ecspve-helfer
AUTH_KEYS=/root/.ssh/authorized_keys

CTID='' CT_HOSTNAME=ecspve STORAGE='' TMPL_STORAGE=local DISK=4 CORES=1 MEMORY=512 BRIDGE=vmbr0 VLAN=''
IP4=dhcp GW='' ONBOOT=1 PASSWORD='' PORT='' PROXY_IP='' PROXY_SET=0 HOST_IP='' BRANCH=''
PULL=0 UPDATE=0 NEW=0 CT_OPTS=''

for arg in "$@"; do
  case $arg in
    --ctid=*) CTID=${arg#*=} ;;
    --hostname=*) CT_HOSTNAME=${arg#*=}; CT_OPTS+=" $arg" ;;
    --storage=*) STORAGE=${arg#*=}; CT_OPTS+=" $arg" ;;
    --tmpl-storage=*) TMPL_STORAGE=${arg#*=}; CT_OPTS+=" $arg" ;;
    --disk=*) DISK=${arg#*=}; CT_OPTS+=" $arg" ;;
    --cores=*) CORES=${arg#*=}; CT_OPTS+=" $arg" ;;
    --memory=*) MEMORY=${arg#*=}; CT_OPTS+=" $arg" ;;
    --bridge=*) BRIDGE=${arg#*=}; CT_OPTS+=" $arg" ;;
    --vlan=*) VLAN=${arg#*=}; CT_OPTS+=" $arg" ;;
    --ip=*) IP4=${arg#*=}; CT_OPTS+=" $arg" ;;
    --gw=*) GW=${arg#*=}; CT_OPTS+=" $arg" ;;
    --no-onboot) ONBOOT=0; CT_OPTS+=" $arg" ;;
    --password=*) PASSWORD=${arg#*=} ;;
    --port=*) PORT=${arg#*=} ;;
    --proxy-ip=*) PROXY_IP=${arg#*=}; PROXY_SET=1 ;;
    --host-ip=*) HOST_IP=${arg#*=} ;;
    --pull) PULL=1 ;;
    --branch=*) BRANCH=${arg#*=}; PULL=1 ;;
    --update) UPDATE=1 ;;
    --new) NEW=1 ;;
    -h | --help) usage ;;
    *) die "Unbekannte Option: $arg (Hilfe: --help)" ;;
  esac
done

# --- Eingaben prüfen -----------------------------------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "Bitte als root auf dem Proxmox-Host ausführen."
command -v pct >/dev/null && command -v pveam >/dev/null && command -v pvesh >/dev/null ||
  die "Dieses Skript läuft nur auf einem Proxmox-VE-Host."
[ -f "$SRC/ecspve.py" ] && [ -f "$SRC/deploy/install.sh" ] && [ -f "$SRC/deploy/proxmox/ecspve-helfer" ] ||
  die "Projektordner unvollständig ($SRC)."
[[ -z $CTID || $CTID =~ ^[0-9]+$ ]] || die "Ungültige --ctid: $CTID"
[[ $CT_HOSTNAME =~ ^[A-Za-z0-9-]+$ ]] || die "Ungültiger --hostname: $CT_HOSTNAME"
[[ $DISK =~ ^[0-9]+$ && $CORES =~ ^[0-9]+$ && $MEMORY =~ ^[0-9]+$ ]] || die "--disk, --cores und --memory brauchen Zahlen."
[[ -z $VLAN || $VLAN =~ ^[0-9]+$ ]] || die "Ungültiges --vlan: $VLAN"
[[ $IP4 == dhcp || $IP4 =~ ^[0-9.]+/[0-9]+$ ]] || die "Ungültige --ip (dhcp oder z. B. 192.168.10.60/24)."
[[ $IP4 == dhcp || -n $GW ]] || die "Bei fester IP bitte auch --gw=… angeben."
[[ -z $PORT || ($PORT =~ ^[0-9]{2,5}$ && $PORT -le 65535) ]] || die "Ungültiger --port: $PORT"
[[ -z $PROXY_IP || $PROXY_IP =~ ^[0-9A-Fa-f.:,]+$ ]] || die "Ungültige --proxy-ip: $PROXY_IP"
[[ -z $HOST_IP || $HOST_IP =~ ^[0-9A-Fa-f.:]+$ ]] || die "Ungültige --host-ip: $HOST_IP"
command -v python3 >/dev/null || { say "python3 für den Helfer installieren"; apt-get install -y -qq python3 >/dev/null; }

# --- Neuesten Stand holen (danach startet das Skript in der neuen Fassung neu) -----------------------------------
if (( PULL )) && [ -z "${ECSPVE_PULLED:-}" ]; then
  [ -d "$SRC/.git" ] || die "$SRC ist kein Git-Projektordner – --pull geht nur mit git clone."
  g=(git -C "$SRC")
  [ -n "$BRANCH" ] || BRANCH=$("${g[@]}" symbolic-ref --short -q HEAD || echo main)
  [[ $BRANCH =~ ^[A-Za-z0-9._/-]+$ ]] || die "Ungültiger Branch: $BRANCH"
  say "Neuesten Stand von „$BRANCH“ holen"
  dirty=$("${g[@]}" status --porcelain --untracked-files=no)
  [ -z "$dirty" ] || die "Im Projektordner gibt es lokale Änderungen:
$dirty
Verwerfen mit: git -C $SRC reset --hard"
  "${g[@]}" fetch --quiet origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" ||
    die "„$BRANCH“ konnte nicht geholt werden. Deploy-Key eingetragen? Test: ssh -T git@github-ecspve"
  "${g[@]}" checkout --quiet -B "$BRANCH" "origin/$BRANCH"
  "${g[@]}" branch --quiet --set-upstream-to="origin/$BRANCH" "$BRANCH"
  echo "Stand: $("${g[@]}" log -1 --format='%h – %s')"
  ECSPVE_PULLED=1 exec "$SRC/pve-install.sh" "${ORIG_ARGS[@]}"
fi
REV=''
[ -d "$SRC/.git" ] && command -v git >/dev/null && REV=$(git -C "$SRC" rev-parse HEAD 2>/dev/null || true)

# --- Neu oder Update? ----------------------------------------------------------------------------------------
has_tag() { pct config "$1" 2>/dev/null | sed -n 's/^tags: *//p' | tr ';, ' '\n\n\n' | grep -qx "$TAG"; }
if (( ! NEW )); then
  if [ -n "$CTID" ]; then
    if pct status "$CTID" >/dev/null 2>&1; then
      has_tag "$CTID" || die "Container $CTID gibt es schon und ist kein Übersicht-Container (Tag „$TAG“ fehlt)."
      UPDATE=1
    elif (( UPDATE )); then
      die "Container $CTID gibt es nicht."
    fi
  else
    mapfile -t EXISTING < <(for id in $(pct list 2>/dev/null | awk 'NR>1 {print $1}'); do has_tag "$id" && echo "$id"; done; true)
    case ${#EXISTING[@]} in
      0) (( ! UPDATE )) || die "Kein Übersicht-Container gefunden (Tag „$TAG“). Für eine Neuinstallation --update weglassen." ;;
      1) CTID=${EXISTING[0]}; UPDATE=1 ;;
      *) die "Mehrere Übersicht-Container gefunden (${EXISTING[*]}). Bitte --ctid=… angeben." ;;
    esac
  fi
fi
(( ! UPDATE )) || [ -z "$CT_OPTS" ] || warn "Container $CTID gibt es schon – diese Optionen gelten nur beim Anlegen:$CT_OPTS"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
# pct gibt im Container nur /sbin:/bin:/usr/sbin:/usr/bin mit – ohne /usr/local, wo ecspve liegt
pct_exec() { pct exec "$CTID" -- env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "$@"; }

# Passwort der Weboberfläche: angegeben, vom abgebrochenen ersten Versuch übernommen, abgefragt oder zufällig
PW_DATEI=''
passwort_festlegen() {
  local datei="/root/ecspve-ct$CTID-passwort.txt"
  [ -n "$PASSWORD" ] || [ ! -f "$datei" ] || PASSWORD=$(sed -n 's/^Passwort: //p' "$datei" | head -n1)
  if [ -z "$PASSWORD" ] && [ -t 0 ]; then
    read -rsp "Passwort für die Übersicht (mind. 12 Zeichen, leer = zufällig): " PASSWORD; echo
    if [ -n "$PASSWORD" ]; then
      read -rsp "Passwort wiederholen: " REPEAT; echo
      [ "$PASSWORD" = "$REPEAT" ] || die "Die Passwörter stimmen nicht überein."
    fi
  fi
  [ -n "$PASSWORD" ] || PASSWORD=$(tr -dc 'A-HJ-NP-Za-km-z2-9' </dev/urandom | head -c 20 || true)
  [ "${#PASSWORD}" -ge 12 ] || die "Das Passwort muss mindestens 12 Zeichen lang sein."
  ( umask 077
    printf 'Übersicht (ecspve), Container %s\nPasswort: %s\nGesetzt:  %s\n' \
      "$CTID" "$PASSWORD" "$(date '+%d.%m.%Y %H:%M')" >"$datei" )
  printf '%s\n' "$PASSWORD" >"$TMP/pw"
  chmod 600 "$TMP/pw"
  PW_DATEI=$datei
}

# --- Container anlegen ---------------------------------------------------------------------------------------
if (( ! UPDATE )); then
  CTID=${CTID:-$(pvesh get /cluster/nextid)}
  pct status "$CTID" >/dev/null 2>&1 && die "Container $CTID gibt es schon."
  if [ -z "$STORAGE" ]; then
    STORAGE=$(pvesm status --content rootdir 2>/dev/null | awk 'NR>1 && $3 == "active" {print $1; exit}')
    STORAGE=${STORAGE:-local-lvm}
  fi

  passwort_festlegen

  say "Debian-Vorlage suchen"
  pveam update >/dev/null 2>&1 || true
  TEMPLATE=$(pveam available --section system | awk '{print $2}' |
    grep -E '^debian-1[2-9]-standard_.*_amd64\.tar\.(zst|gz|xz)$' | sort -V | tail -n1)
  [ -n "$TEMPLATE" ] || die "Keine Debian-Vorlage gefunden (pveam available --section system)."
  pveam list "$TMPL_STORAGE" | grep -q "$TEMPLATE" || pveam download "$TMPL_STORAGE" "$TEMPLATE"

  NET="name=eth0,bridge=$BRIDGE,ip=$IP4"
  [ -n "$GW" ] && NET+=",gw=$GW"
  [ -n "$VLAN" ] && NET+=",tag=$VLAN"
  NOTES=$(sed -e "s/ctCTID/ct$CTID/g" -e "s/\bCTID\b/$CTID/g" "$SRC/deploy/proxmox/notizen.html")

  say "Container $CTID ($CT_HOSTNAME) anlegen – $CORES CPU, $MEMORY MB RAM, $DISK GB auf $STORAGE"
  pct create "$CTID" "$TMPL_STORAGE:vztmpl/$TEMPLATE" \
    --hostname "$CT_HOSTNAME" --cores "$CORES" --memory "$MEMORY" --swap 256 \
    --rootfs "$STORAGE:$DISK" --net0 "$NET" \
    --unprivileged 1 --features nesting=1 --onboot "$ONBOOT" --ostype debian --timezone host \
    --tags "$TAG" --description "$NOTES" >/dev/null
  pct start "$CTID"
fi

pct status "$CTID" | grep -q running || { pct start "$CTID"; sleep 3; }

say "Auf Netzwerk im Container warten"
for _ in $(seq 1 60); do pct_exec getent hosts deb.debian.org >/dev/null 2>&1 && break; sleep 2; done
pct_exec getent hosts deb.debian.org >/dev/null 2>&1 || die "Container $CTID erreicht das Internet nicht (Netzwerk/VLAN/DNS prüfen: pct enter $CTID)."

# Brach eine frühere Installation nach dem Anlegen ab, fehlt im Container noch das Passwort
if (( UPDATE )) && ! pct_exec test -f /etc/ecspve/passwort; then
  say "Im Container ist noch kein Passwort gesetzt (frühere Installation abgebrochen?)"
  passwort_festlegen
fi

# --- Adresse des Hosts aus Sicht des Containers ------------------------------------------------------------------
NET0=$(pct config "$CTID" | sed -n 's/^net0: //p')
CT_BRIDGE=$(sed -n 's/.*bridge=\([^,]*\).*/\1/p' <<<"$NET0")
CT_IP=$(sed -n 's/.*ip=\([0-9.]*\)\/.*/\1/p' <<<"$NET0")
if [ -z "$HOST_IP" ]; then
  HOST_IP=$(ip -4 -o addr show dev "${CT_BRIDGE:-vmbr0}" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)
  [ -n "$HOST_IP" ] || HOST_IP=$(hostname -I | awk '{print $1}')
fi
[ -n "$HOST_IP" ] || die "Adresse dieses Hosts nicht gefunden – bitte --host-ip=… angeben."
HOSTKEY=$(awk '{print $1" "$2}' /etc/ssh/ssh_host_ed25519_key.pub 2>/dev/null || true)
[[ $HOSTKEY =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+$ ]] || die "SSH-Hostschlüssel fehlt (/etc/ssh/ssh_host_ed25519_key.pub)."

# --- Helfer auf dem Host ---------------------------------------------------------------------------------------
say "Helfer auf dem Host einrichten ($HELFER)"
install -m 0755 "$SRC/deploy/proxmox/ecspve-helfer" "$HELFER"
install -d -m 0700 /etc/ecspve /var/lib/ecspve-helfer
[ -f /etc/ecspve/helfer.conf ] || install -m 0600 "$SRC/deploy/proxmox/helfer.conf" /etc/ecspve/helfer.conf
"$HELFER" version >/dev/null || die "Der Helfer läuft nicht ($HELFER version)."

# --- Übertragen und installieren -----------------------------------------------------------------------------
say "Übersicht in den Container übertragen${REV:+ (Version ${REV:0:7})}"
tar -czf "$TMP/ecspve.tgz" -C "$SRC" --exclude=.git --exclude=test .
pct push "$CTID" "$TMP/ecspve.tgz" /root/ecspve.tgz
pct_exec bash -c 'rm -rf /root/ecspve-src && mkdir -p /root/ecspve-src && tar -xzf /root/ecspve.tgz -C /root/ecspve-src && rm /root/ecspve.tgz'
ENVS=(PVE_HOST="$HOST_IP" PVE_HOSTKEY="$HOSTKEY")
[ -n "$REV" ] && ENVS+=(REVISION="$REV")
[ -n "$PORT" ] && ENVS+=(PORT="$PORT")
(( PROXY_SET )) && ENVS+=(PROXY_IP="$PROXY_IP")
if [ -f "$TMP/pw" ]; then
  pct push "$CTID" "$TMP/pw" /root/.ecspve-pw --perms 600
  ENVS+=(PASSWORT_DATEI=/root/.ecspve-pw)
fi
say "Installation im Container"
pct_exec env "${ENVS[@]}" bash /root/ecspve-src/deploy/install.sh
pct_exec rm -rf /root/ecspve-src /root/.ecspve-pw

# --- Schlüssel des Containers auf den Helfer festlegen --------------------------------------------------------
say "SSH-Schlüssel des Containers für den Helfer freigeben"
PUBKEY=$(pct_exec cat /etc/ecspve/ssh/id_ed25519.pub | awk '{print $1" "$2}')
[[ $PUBKEY =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+$ ]] || die "Öffentlicher Schlüssel des Containers fehlt."
FROM=''
[ -n "$CT_IP" ] && FROM=",from=\"$CT_IP\""
install -d -m 0700 /root/.ssh
touch "$AUTH_KEYS"
# Alte Zeilen dieses Containers entfernen. In einem Cluster ist die Datei ein Link nach /etc/pve/priv –
# deshalb in die Datei schreiben statt sie zu ersetzen.
ZIEL=$(readlink -f "$AUTH_KEYS")
{ grep -v " ecspve-ct$CTID\$" "$ZIEL" | grep -vF "$PUBKEY" || true
  printf 'restrict,command="%s"%s %s ecspve-ct%s\n' "$HELFER" "$FROM" "$PUBKEY" "$CTID"
} >"$TMP/auth"
cat "$TMP/auth" >"$ZIEL"
chmod 600 "$ZIEL" 2>/dev/null || true

say "Verbindung prüfen"
pct_exec ecspve verbindung || die "Der Container erreicht den Helfer nicht. Prüfen: SSH auf $HOST_IP erreichbar (Firewall?), PermitRootLogin erlaubt Schlüssel? Andere Adresse mit --host-ip=…"

PORT_CT=$(pct_exec sed -n 's/^PORT=//p' /etc/ecspve/ecspve.env | tail -n1)
CT_ADDR=${CT_IP:-$(pct_exec hostname -I | awk '{print $1}')}
say "Fertig – Container $CTID"
echo "  Weboberfläche: http://$CT_ADDR:$PORT_CT  (nur HTTP – im Caddy davor: reverse_proxy $CT_ADDR:$PORT_CT)"
if [ -n "$PW_DATEI" ]; then
  echo "  Passwort:      $PW_DATEI (nur für root lesbar)"
  echo "  Erste Anmeldung: Passwort, dann zweiten Faktor mit einer Authenticator-App einrichten."
fi
pct_exec grep -q '^PROXY_IP=.' /etc/ecspve/ecspve.env ||
  warn "Kein --proxy-ip gesetzt: Die Übersicht ist auch direkt über HTTP erreichbar. Empfohlen: $SRC/pve-install.sh --proxy-ip=<IP eures Caddy> – dann nur noch über den Proxy mit HTTPS."
[ -n "$CT_IP" ] || warn "Der Container hat DHCP – der SSH-Schlüssel gilt deshalb von jeder Adresse. Mit fester IP (--ip=… beim Anlegen) ist er zusätzlich auf diese Adresse beschränkt."
echo "  Update:        $SRC/pve-install.sh --pull"
echo "  Konsole:       pct enter $CTID"
