#!/usr/bin/env bash
# REGOinstall :: minimaler Bootstrap für die Port-80-Oberfläche auf einer
# frischen LXC -- läuft INNERHALB des Containers, direkt am Ende von
# create-lxc.sh. Installiert absichtlich NUR das Nötigste für die
# Landing-/Install-Seite (Python3 + regoinstall_updater.py), NICHT
# REGObase selbst -- das passiert erst, wenn im Browser auf
# "Installieren" geklickt wird (install-regobase.sh läuft dann als
# Hintergrund-Job, ausgelöst von der Weboberfläche, nicht von hier).
#
# Usage (als root, innerhalb der Ziel-LXC):
#   curl -fsSL https://raw.githubusercontent.com/epogo75/REGOinstall/main/bootstrap-port80.sh | bash

set -euo pipefail

REGOINSTALL_RAW_BASE="https://raw.githubusercontent.com/epogo75/REGOinstall/main"

if [ "$(id -u)" -ne 0 ]; then
  echo "Bitte als root ausführen." >&2
  exit 1
fi

echo "==> Installiere Python3..."
apt-get update -qq
apt-get install -y -qq python3 curl

echo "==> Lade regoinstall_updater.py..."
mkdir -p /opt/regoinstall /etc/regoinstall
curl -fsSL "${REGOINSTALL_RAW_BASE}/update-service/regoinstall_updater.py" -o /opt/regoinstall/regoinstall_updater.py

# apps.json leer = "noch nichts installiert" -- regoinstall_updater.py
# zeigt in diesem Zustand die Installations-Seite statt der Update-/
# Backup-/Restore-Karten. install-regobase.sh (ausgelöst über den
# "Installieren"-Knopf) überschreibt diese Datei am Ende selbst mit dem
# echten REGObase-Eintrag.
if [ ! -f /etc/regoinstall/apps.json ]; then
  echo "[]" > /etc/regoinstall/apps.json
fi

if [ ! -f /etc/regoinstall/config.json ]; then
  cat > /etc/regoinstall/config.json <<EOF
{
  "auth_db": null,
  "install_cmd": ["bash", "-c", "curl -fsSL ${REGOINSTALL_RAW_BASE}/install-regobase.sh | bash"]
}
EOF
  chmod 600 /etc/regoinstall/config.json
fi

cat > /etc/systemd/system/regoinstall.service <<'EOF'
[Unit]
Description=REGOinstall (Update-/Backup-/Install-Oberfläche, Port 80)
After=network-online.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 /opt/regoinstall/regoinstall_updater.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now regoinstall.service

ip="$(hostname -I | awk '{print $1}')"
echo ""
echo "================================================================"
echo " REGOinstall-Oberfläche läuft: http://${ip}/"
echo " Im Browser öffnen und auf 'Installieren' klicken -- der GitHub-"
echo " Anmeldecode (privates Repo) erscheint danach im Log."
echo "================================================================"
