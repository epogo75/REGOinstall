#!/usr/bin/env bash
# REGOinstall :: Skript 2 -- installiert die komplette REGObase auf einer
# frischen Ubuntu-Maschine/LXC. Läuft INNERHALB des Zielsystems (nach
# Skript 1, create-lxc.sh, falls es eine neue Proxmox-LXC ist).
#
# Usage (als root):
#   curl -fsSL https://raw.githubusercontent.com/epogo75/REGOinstall/main/install-regobase.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/epogo75/REGOinstall/main/install-regobase.sh | bash -s -- --update
#
# Was passiert:
#   1. OS-Pakete: git, Python3+venv, sqlite3, Node.js (NodeSource),
#      Docker Engine + Compose Plugin, gh CLI.
#   2. Klont REGObase + REGOcore + regoeldat-core (private GitHub-Repos,
#      gh-Auth nötig) als Geschwister-Ordner unter INSTALL_ROOT.
#   3. /etc/regobase.env (JWT-Secret + Admin-Passwort) -- wird NICHT
#      angelegt, falls schon vorhanden (siehe "BACKUP EINSPIELEN" unten).
#   4. regobase.service (systemd) -- der eigentliche App-Bootstrap
#      (venv, pip, npm, Migrationen, Frontend-Build) passiert in
#      REGObase/run.sh selbst, dieses Skript dupliziert das nicht.
#   5. Matterbridge-Docker-Container + das mitgelieferte Plugin.
#   6. REGOinstall-Weboberfläche auf Port 80 (Update-/Backup-/Restore-
#      Knöpfe, siehe update-service/regoinstall_updater.py).
#
# ============================================================================
# BACKUP EINSPIELEN (nach der Erstinstallation, bevor der erste echte
# Login/Setup passiert) -- entweder von Hand oder über die Weboberfläche
# auf Port 80 ("Restore"):
#
#   1. systemctl stop regobase
#   2. Alte regobase.db nach $INSTALL_ROOT/REGObase/backend/data/ kopieren.
#   3. WICHTIG: die ZUGEHÖRIGE /etc/regobase.env (mit dem GLEICHEN
#      REGOBASE_JWT_SECRET wie auf dem alten Server) ebenfalls kopieren --
#      Hue-/SwitchBot-Zugangsdaten, KNX-Projekt-Passwort und Deploy-
#      Zugangsdaten sind in der DB mit einem aus REGOBASE_JWT_SECRET
#      abgeleiteten Schlüssel verschlüsselt (regobase/crypto.py). Ein
#      NEUES Secret macht diese Felder unlesbar, ohne dass es beim
#      Kopieren auffällt.
#   4. systemctl start regobase
#
# Falls das Backup VOR diesem Skript schon bereitsteht: /etc/regobase.env
# vorher von Hand mit dem alten Inhalt anlegen (chmod 600) -- write_env_file()
# unten überspringt die Erzeugung dann von selbst.
# ============================================================================

set -euo pipefail

REGOINSTALL_VERSION="0.1"
REGOINSTALL_BUILD="2026-08-15.1"

INSTALL_ROOT="${INSTALL_ROOT:-/opt/regobase-stack}"
GH_ORG="epogo75"
REPOS=(REGObase REGOcore regoeldat-core)
NODE_MAJOR="24"
BACKEND_PORT="${REGOBASE_PORT:-8002}"
FRONTEND_PORT="${REGOBASE_FRONTEND_PORT:-5175}"
REGOINSTALL_RAW_BASE="https://raw.githubusercontent.com/${GH_ORG}/REGOinstall/main"

echo "REGOinstall v${REGOINSTALL_VERSION} (build ${REGOINSTALL_BUILD}) -- install-regobase.sh"

log() { echo "==> $*"; }

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Bitte als root ausführen (sudo ...)." >&2
    exit 1
  fi
}

install_system_packages() {
  log "Installiere Basis-Pakete..."
  apt-get update
  apt-get install -y git python3 python3-venv python3-pip sqlite3 curl ca-certificates gnupg openssl
}

install_node() {
  if command -v node >/dev/null 2>&1; then
    log "Node.js schon vorhanden ($(node --version)), überspringe."
    return
  fi
  log "Installiere Node.js ${NODE_MAJOR}.x (NodeSource)..."
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
  apt-get install -y nodejs
}

install_docker() {
  if command -v docker >/dev/null 2>&1; then
    log "Docker schon vorhanden ($(docker --version)), überspringe."
    return
  fi
  log "Installiere Docker Engine + Compose Plugin..."
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
}

install_gh_cli() {
  if command -v gh >/dev/null 2>&1; then
    return
  fi
  log "Installiere GitHub CLI..."
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
  chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list
  apt-get update
  apt-get install -y gh
}

ensure_gh_auth() {
  if gh auth status >/dev/null 2>&1; then
    return
  fi
  if [ -n "${GH_TOKEN:-}" ]; then
    log "Verwende GH_TOKEN aus der Umgebung für gh-Auth."
    echo "$GH_TOKEN" | gh auth login --with-token
    return
  fi
  log "GitHub-Login nötig (private Repos) -- bitte den Anweisungen folgen:"
  gh auth login
}

clone_repos() {
  # Bei einem erneuten Lauf (fehlgeschlagener erster Versuch, oder
  # dieses Skript wurde inzwischen erneut ausgelöst) NICHT nur prüfen ob
  # der Ordner existiert und dann überspringen -- ein echter Fall live
  # gefunden: der erste Versuch klonte REGObase vor einem Push neuer
  # Commits (u.a. matterbridge-regobase/), ein zweiter Klick auf
  # "Installation starten" sah ".git existiert" und übersprang das
  # Klonen komplett, der Checkout blieb dauerhaft auf dem alten Stand
  # hängen, obwohl GitHub längst aktueller war. Jetzt: schon vorhanden
  # -> pull statt überspringen.
  mkdir -p "$INSTALL_ROOT"
  for repo in "${REPOS[@]}"; do
    if [ -d "$INSTALL_ROOT/$repo/.git" ]; then
      log "$repo schon vorhanden, hole aktuellen Stand..."
      (cd "$INSTALL_ROOT/$repo" && git pull --ff-only)
    else
      log "Klone $repo..."
      gh repo clone "$GH_ORG/$repo" "$INSTALL_ROOT/$repo"
    fi
  done
}

write_env_file() {
  if [ -f /etc/regobase.env ]; then
    log "/etc/regobase.env existiert schon (z.B. von einem eingespielten Backup), überspringe Erzeugung."
    return
  fi
  log "Erzeuge /etc/regobase.env mit neuem JWT-Secret + Admin-Passwort..."
  local jwt_secret admin_password
  jwt_secret="$(openssl rand -hex 32)"
  admin_password="$(openssl rand -base64 18)"
  cat > /etc/regobase.env <<EOF
REGOBASE_JWT_SECRET=${jwt_secret}
REGOBASE_ADMIN_PASSWORD=${admin_password}
EOF
  chmod 600 /etc/regobase.env
  echo ""
  echo "  Erstinstallations-Admin-Login: admin / ${admin_password}"
  echo "  (nur relevant, falls KEIN Backup eingespielt wird -- steht auch in /etc/regobase.env)"
  echo ""
}

install_systemd_unit() {
  log "Richte regobase.service ein..."
  cat > /etc/systemd/system/regobase.service <<EOF
[Unit]
Description=REGObase (backend + frontend)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Environment=HOME=/root
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=REGOBASE_PORT=${BACKEND_PORT}
Environment=REGOBASE_FRONTEND_PORT=${FRONTEND_PORT}
EnvironmentFile=/etc/regobase.env
WorkingDirectory=${INSTALL_ROOT}/REGObase
ExecStart=${INSTALL_ROOT}/REGObase/run.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  chmod +x "${INSTALL_ROOT}/REGObase/run.sh"
  systemctl daemon-reload
  systemctl enable --now regobase.service
}

build_matterbridge_plugin() {
  # tsc braucht die echten matterbridge-Typdeklarationen zum Bauen --
  # package.json deklariert matterbridge absichtlich NICHT als
  # Abhängigkeit (Matterbridges eigene Regel, sonst "nur eine Instanz"-
  # Laufzeitfehler), also hier separat nur fürs Bauen installieren und
  # danach wieder entfernen.
  log "Baue matterbridge-regobase-Plugin..."
  (
    cd "$INSTALL_ROOT/REGObase/matterbridge-regobase"
    npm install --no-fund --no-audit
    npm install --no-save --no-fund --no-audit matterbridge
    npm run build
    rm -rf node_modules package-lock.json
  )
}

install_matterbridge() {
  log "Richte Matterbridge-Container ein..."
  build_matterbridge_plugin
  mkdir -p /docker/matterbridge/{Matterbridge,matterbridge-storage,mattercert}
  cat > /docker/matterbridge/docker-compose.yml <<EOF
services:
  matterbridge:
    image: luligu/matterbridge:latest
    container_name: matterbridge
    restart: unless-stopped
    network_mode: host
    security_opt:
      - apparmor=unconfined
    stop_grace_period: 60s
    volumes:
      - ./Matterbridge:/root/Matterbridge
      - ./matterbridge-storage:/root/.matterbridge
      - ./mattercert:/root/.mattercert
      - ${INSTALL_ROOT}/REGObase/matterbridge-regobase:/root/Matterbridge/matterbridge-regobase
EOF
  (cd /docker/matterbridge && docker compose up -d)
  sleep 5
  docker exec matterbridge matterbridge --add /root/Matterbridge/matterbridge-regobase || true
  docker restart matterbridge
}

install_update_service() {
  log "Richte REGOinstall-Weboberfläche (Port 80) ein..."
  mkdir -p /opt/regoinstall /etc/regoinstall
  curl -fsSL "${REGOINSTALL_RAW_BASE}/update-service/regoinstall_updater.py" -o /opt/regoinstall/regoinstall_updater.py

  if [ ! -f /etc/regoinstall/apps.json ]; then
    local ip
    ip="$(hostname -I | awk '{print $1}')"
    cat > /etc/regoinstall/apps.json <<EOF
[
  {
    "name": "REGObase",
    "url": "http://${ip}:${FRONTEND_PORT}",
    "service": "regobase.service",
    "update_cmd": ["bash", "-c", "curl -fsSL ${REGOINSTALL_RAW_BASE}/install-regobase.sh | bash -s -- --update"],
    "backup_db": "${INSTALL_ROOT}/REGObase/backend/data/regobase.db",
    "backup_extra_files": ["/etc/regobase.env"]
  }
]
EOF
  fi

  # Immer überschreiben, nicht nur wenn fehlend -- bootstrap-port80.sh
  # legt diese Datei schon VOR der eigentlichen Installation mit
  # "auth_db": null an (Installations-Ansicht statt Login), hier muss
  # der echte Pfad rein, sonst bliebe die Seite dauerhaft im "nicht
  # installiert"-Zustand hängen, obwohl REGObase längst läuft.
  cat > /etc/regoinstall/config.json <<EOF
{
  "auth_db": "${INSTALL_ROOT}/REGObase/backend/data/regobase.db",
  "connect_github_cmd": ["bash", "-c", "curl -fsSL ${REGOINSTALL_RAW_BASE}/install-regobase.sh | bash -s -- --connect-github"],
  "install_cmd": ["bash", "-c", "curl -fsSL ${REGOINSTALL_RAW_BASE}/install-regobase.sh | bash"]
}
EOF
  chmod 600 /etc/regoinstall/config.json

  # Nutzt REGObase's eigenes venv statt Systempython -- das hat
  # argon2-cffi schon installiert (exakt dieselbe Version, mit der die
  # Passwort-Hashes in der users-Tabelle erzeugt wurden), keine separate
  # Installation nötig. Bleibt trotzdem resilient gegenüber einem
  # abgestürzten regobase.service, da nur die Dateien des venvs
  # gebraucht werden, kein laufender REGObase-Prozess.
  #
  # install_systemd_unit() hat regobase.service zwar schon gestartet,
  # aber "systemctl enable --now" wartet nicht, bis run.sh's eigener
  # Erst-Start-Bootstrap (venv anlegen, pip install, das dauert) fertig
  # ist -- ohne diese Wartezeile würde hier auf ein venv gezeigt, das
  # noch gar nicht existiert.
  log "Warte auf REGObase's venv (Erst-Start-Bootstrap läuft evtl. noch)..."
  for _ in $(seq 1 60); do
    [ -x "${INSTALL_ROOT}/REGObase/backend/.venv/bin/python3" ] && break
    sleep 5
  done

  cat > /etc/systemd/system/regoinstall.service <<EOF
[Unit]
Description=REGOinstall (Update-/Backup-Oberfläche, Port 80)
After=network-online.target

[Service]
Type=simple
User=root
ExecStart=${INSTALL_ROOT}/REGObase/backend/.venv/bin/python3 /opt/regoinstall/regoinstall_updater.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now regoinstall.service
}

print_summary() {
  local ip
  ip="$(hostname -I | awk '{print $1}')"
  echo ""
  echo "================================================================"
  echo " REGObase installiert."
  echo "   Backend:  http://${ip}:${BACKEND_PORT}"
  echo "   Frontend: http://${ip}:${FRONTEND_PORT}"
  echo "   REGOinstall (Update/Backup/Restore): http://${ip}/"
  echo ""
  echo " Backup einspielen: siehe Kommentarblock am Skriptanfang, oder"
  echo " über die Weboberfläche (Restore-Knopf)."
  echo "================================================================"
}

do_update() {
  log "Update: git pull auf allen drei Repos..."
  for repo in "${REPOS[@]}"; do
    (cd "$INSTALL_ROOT/$repo" && git pull --ff-only)
  done

  log "Aktualisiere Backend-Abhängigkeiten..."
  (cd "$INSTALL_ROOT/REGObase/backend" && source .venv/bin/activate && \
    pip install -e "$INSTALL_ROOT/REGOcore/backend" && \
    pip install -e "$INSTALL_ROOT/regoeldat-core/backend" && \
    pip install -e ".[dev]" && \
    alembic upgrade head)

  log "Aktualisiere Frontend..."
  (cd "$INSTALL_ROOT/REGObase/frontend" && npm install && npm run build)

  log "Starte regobase.service neu..."
  systemctl restart regobase.service

  build_matterbridge_plugin
  docker restart matterbridge || true

  echo "Update fertig."
}

main() {
  require_root

  if [ "${1:-}" = "--update" ]; then
    do_update
    exit 0
  fi

  if [ "${1:-}" = "--connect-github" ]; then
    # Eigener, schneller erster Schritt für die Port-80-Oberfläche
    # (direkter Nutzerwunsch: "Der Github Login als erstes - bevor man
    # irgendwas startet") -- installiert nur die gh-CLI und meldet an,
    # macht sonst nichts. install-regobase.sh selbst (ohne Flag) prüft
    # danach über ensure_gh_auth() ohnehin zuerst, ob schon angemeldet
    # ist, und überspringt den Login dann automatisch.
    install_gh_cli
    ensure_gh_auth
    log "GitHub verbunden."
    exit 0
  fi

  install_system_packages
  install_node
  install_docker
  install_gh_cli
  ensure_gh_auth
  clone_repos
  write_env_file
  install_systemd_unit
  install_matterbridge
  install_update_service
  print_summary
}

main "$@"
