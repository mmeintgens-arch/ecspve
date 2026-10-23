#!/usr/bin/env bash
# Übersicht (ecspve) im Container installieren oder aktualisieren.
#
# Läuft IM CONTAINER als root und wird von pve-install.sh auf dem Proxmox-Host aufgerufen (pct exec). Von Hand nur
# zum Reparieren nötig: bash /root/ecspve-src/deploy/install.sh
#
# Werte von pve-install.sh (alle optional, Vorhandenes bleibt sonst stehen):
#   PVE_HOST=IP          Proxmox-Host, auf dem ecspve-helfer läuft
#   PVE_HOSTKEY="…"      SSH-Hostschlüssel des Proxmox-Hosts (Inhalt von ssh_host_ed25519_key.pub)
#   PORT=8080            Port der Weboberfläche (nur HTTP)
#   PROXY_IP=IP[,IP]     IP eures Reverse Proxys (leer setzt zurück, nur wenn übergeben)
#   PASSWORT_DATEI=PFAD  Datei mit dem ersten Passwort der Weboberfläche
#   REVISION=…           Git-Stand für /opt/ecspve/REVISION

set -euo pipefail

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mHinweis:\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31mFehler:\033[0m %s\n' "$*" >&2; exit 1; }

# pct exec gibt nur /sbin:/bin:/usr/sbin:/usr/bin mit – /usr/local gehört aber dazu (dort liegt ecspve)
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

SRC="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
ENV_FILE=/etc/ecspve/ecspve.env
SSH_DIR=/etc/ecspve/ssh

[ "$(id -u)" -eq 0 ] || die "Bitte als root ausführen."
[ ! -d /etc/pve ] || die "Dieses Skript gehört in den Container der Übersicht, nicht auf den Proxmox-Host (dort: pve-install.sh)."
[ -f "$SRC/ecspve.py" ] && [ -d "$SRC/web" ] || die "Projektordner unvollständig ($SRC)."

[ -z "${PVE_HOST:-}" ] || [[ $PVE_HOST =~ ^[0-9A-Za-z.:-]+$ ]] || die "Ungültiger PVE_HOST: $PVE_HOST"
[ -z "${PVE_HOSTKEY:-}" ] || [[ $PVE_HOSTKEY =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+$ ]] || die "Ungültiger PVE_HOSTKEY."
[ -z "${PORT:-}" ] || [[ $PORT =~ ^[0-9]{2,5}$ ]] || die "Ungültiger PORT: $PORT"
[ -z "${PROXY_IP:-}" ] || [[ $PROXY_IP =~ ^[0-9A-Fa-f.:,]+$ ]] || die "Ungültige PROXY_IP: $PROXY_IP"

# --- Pakete --------------------------------------------------------------------------------------------------
say "Pakete installieren"
export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 openssh-client ca-certificates curl qrencode >/dev/null

# --- Benutzer, Ordner, Programm ------------------------------------------------------------------------------
say "Programm nach /opt/ecspve"
id ecspve >/dev/null 2>&1 || useradd --system --home-dir /var/lib/ecspve --shell /usr/sbin/nologin ecspve
install -d -m 0750 -o ecspve -g ecspve /var/lib/ecspve
install -d -m 0750 -o root -g ecspve /etc/ecspve
install -d -m 0700 -o ecspve -g ecspve "$SSH_DIR"
rm -rf /opt/ecspve.neu
install -d -m 0755 /opt/ecspve.neu/web
install -m 0644 "$SRC/ecspve.py" /opt/ecspve.neu/ecspve.py
install -m 0644 "$SRC"/web/* /opt/ecspve.neu/web/
[ -z "${REVISION:-}" ] || printf '%s\n' "$REVISION" >/opt/ecspve.neu/REVISION
rm -rf /opt/ecspve.alt
[ ! -d /opt/ecspve ] || mv /opt/ecspve /opt/ecspve.alt
mv /opt/ecspve.neu /opt/ecspve
rm -rf /opt/ecspve.alt
install -m 0755 "$SRC/deploy/ecspve" /usr/local/bin/ecspve

# --- Einstellungen -------------------------------------------------------------------------------------------
say "Einstellungen ($ENV_FILE)"
[ -f "$ENV_FILE" ] || install -m 0640 -o root -g ecspve "$SRC/deploy/ecspve.env" "$ENV_FILE"
setze() {
  if grep -q "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else printf '%s=%s\n' "$1" "$2" >>"$ENV_FILE"; fi
}
# Neue Einstellungen aus der Vorlage ergänzen, vorhandene bleiben unverändert
while IFS= read -r zeile; do
  [[ $zeile =~ ^([A-Z_]+)= ]] || continue
  grep -q "^${BASH_REMATCH[1]}=" "$ENV_FILE" || printf '%s\n' "$zeile" >>"$ENV_FILE"
done <"$SRC/deploy/ecspve.env"
[ -z "${PVE_HOST:-}" ] || setze PVE_HOST "$PVE_HOST"
[ -z "${PORT:-}" ] || setze PORT "$PORT"
[ -z "${PROXY_IP+x}" ] || setze PROXY_IP "$PROXY_IP"
chown root:ecspve "$ENV_FILE"
chmod 0640 "$ENV_FILE"
grep -q "^TOKEN=." "$ENV_FILE" || python3 /opt/ecspve/ecspve.py token >/dev/null

# --- SSH-Zugang zum Helfer auf dem Proxmox-Host --------------------------------------------------------------
if [ ! -f "$SSH_DIR/id_ed25519" ]; then
  say "SSH-Schlüssel für den Helfer erzeugen"
  runuser -u ecspve -- ssh-keygen -q -t ed25519 -N '' -C "ecspve@$(hostname)" -f "$SSH_DIR/id_ed25519"
fi
if [ -n "${PVE_HOSTKEY:-}" ]; then
  host=$(sed -n 's/^PVE_HOST=//p' "$ENV_FILE" | tail -n1)
  printf '%s %s\n' "$host" "$PVE_HOSTKEY" >"$SSH_DIR/known_hosts"
  chown ecspve:ecspve "$SSH_DIR/known_hosts"
  chmod 0600 "$SSH_DIR/known_hosts"
fi

if [ -n "${PASSWORT_DATEI:-}" ] && [ -s "$PASSWORT_DATEI" ]; then
  say "Passwort der Weboberfläche setzen"
  python3 /opt/ecspve/ecspve.py passwort --stdin <"$PASSWORT_DATEI" >/dev/null
fi

# --- Dienste -------------------------------------------------------------------------------------------------
say "Dienste einrichten"
install -m 0644 "$SRC/deploy/ecspve.service" "$SRC/deploy/ecspve-nacht.service" "$SRC/deploy/ecspve-nacht.timer" \
  /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet ecspve.service ecspve-nacht.timer
systemctl restart ecspve.service
systemctl start ecspve-nacht.timer

for _ in $(seq 1 20); do /usr/local/bin/ecspve ping >/dev/null 2>&1 && break; sleep 1; done
/usr/local/bin/ecspve ping >/dev/null 2>&1 || die "Die Weboberfläche startet nicht – Protokoll: journalctl -u ecspve -n 50"
[ -f /etc/ecspve/passwort ] || warn "Noch kein Passwort gesetzt – im Container: ecspve passwort"
echo "Weboberfläche läuft (Port $(sed -n 's/^PORT=//p' "$ENV_FILE" | tail -n1))."
