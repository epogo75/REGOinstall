#!/usr/bin/env bash
# REGOinstall :: create-lxc-regobasex1-dev.sh -- erzeugt eine Ubuntu-LXC
# für REGObaseX1-Entwicklung/-Tests gegen einen echten Gira X1, ohne die
# produktive REGObase-LXC anzufassen. LÄUFT AUF DEM PROXMOX-HOST SELBST,
# nicht in einem Container.
#
# Ersetzt den ursprünglich für diesen Zweck gebauten create-vm.sh-Ansatz
# (echte VM + Cloud-Init) -- direkter Nutzerwunsch: "ich will ein LXC -
# keine VM!!". Eine LXC braucht dafür keinen Snippets-Storage/Cloud-Init:
# der zweite Benutzer (rego) wird nach dem Start einfach per `pct exec`
# angelegt, kein Umweg über eine user-data.yml nötig.
#
# NICHT gegen ein echtes Proxmox getestet (wie create-lxc.sh/create-vm.sh
# -- keine Proxmox-Host-Zugriffsmöglichkeit in dieser Session).
#
# Usage (auf dem Proxmox-Host, als root):
#   curl -fsSL https://raw.githubusercontent.com/epogo75/REGOinstall/main/create-lxc-regobasex1-dev.sh -o create-lxc-regobasex1-dev.sh
#   bash create-lxc-regobasex1-dev.sh
#
# Fragt interaktiv: VMID, IP/Gateway, Storage, Ressourcen, Root-Passwort,
# rego-Passwort. Danach: `pct create` + `pct start`, wartet auf Netzwerk,
# legt den rego-Benutzer (sudo) an. Installiert selbst NICHTS weiter --
# REGObaseX1 kommt in einem späteren, eigenen Schritt (Task 17 des
# Umsetzungsplans, port80-Installer). Port 5190 ist nur reserviert/
# dokumentiert, hier läuft noch nichts.

set -euo pipefail

REGOINSTALL_VERSION="0.1"
REGOINSTALL_BUILD="2026-08-16.2"
echo "REGOinstall v${REGOINSTALL_VERSION} (build ${REGOINSTALL_BUILD}) -- create-lxc-regobasex1-dev.sh"

require_pve() {
  if ! command -v pct >/dev/null 2>&1; then
    echo "Kein 'pct' gefunden -- dieses Skript muss auf dem Proxmox-Host laufen, nicht in einem Container." >&2
    exit 1
  fi
}

is_yes() {
  case "${1,,}" in
    j | ja | y | yes) return 0 ;;
    *) return 1 ;;
  esac
}

ask() {
  # ask "Frage" "Default" -> setzt REPLY
  local prompt="$1" default="${2:-}"
  local answer
  if [ -n "$default" ]; then
    read -r -p "$prompt [$default]: " answer
    REPLY="${answer:-$default}"
  else
    read -r -p "$prompt: " answer
    REPLY="$answer"
  fi
}

ask_password() {
  # ask_password "Frage" -> setzt REPLY. Maskierte Eingabe (read -s, kein
  # Klartext am Bildschirm) und zweimal abgefragt, gleiches Muster wie in
  # create-lxc.sh -- ein Tippfehler fällt sonst erst beim ersten Login auf.
  local prompt="$1"
  local pw1 pw2
  while true; do
    read -r -s -p "$prompt: " pw1
    echo
    if [ -z "$pw1" ]; then
      echo "Passwort darf nicht leer sein." >&2
      continue
    fi
    read -r -s -p "$prompt (Wiederholung): " pw2
    echo
    if [ "$pw1" != "$pw2" ]; then
      echo "Passwörter stimmen nicht überein -- nochmal." >&2
      continue
    fi
    REPLY="$pw1"
    return
  done
}

find_ubuntu_template() {
  # Wie create-lxc.sh: sucht das neueste verfügbare Ubuntu-26.04-Template
  # statt einen Dateinamen zu hardcoden.
  pveam update >/dev/null 2>&1 || true
  local tmpl
  tmpl="$(pveam available --section system 2>/dev/null | awk '{print $2}' | grep '^ubuntu-26.04-standard' | sort -V | tail -1)"
  if [ -z "$tmpl" ]; then
    echo ""
    return 1
  fi
  echo "$tmpl"
}

main() {
  require_pve

  echo "=== REGObaseX1-Entwicklungs-LXC anlegen ==="
  echo ""

  ask "VMID" "310"
  local vmid="$REPLY"

  ask "Hostname" "REGObaseX1-dev"
  local hostname="$REPLY"

  ask "IP-Adresse (CIDR, z.B. 192.168.1.235/24) -- leer für DHCP" ""
  local ip_cidr="$REPLY"

  local gateway=""
  if [ -n "$ip_cidr" ]; then
    local suggested_gw
    suggested_gw="$(echo "$ip_cidr" | sed -E 's#^([0-9]+\.[0-9]+\.[0-9]+)\.[0-9]+/.*#\1.1#')"
    ask "Gateway" "$suggested_gw"
    gateway="$REPLY"
    if [ -z "$gateway" ]; then
      echo "Gateway darf bei einer festen IP nicht leer sein (sonst kein Internetzugang im Container)." >&2
      exit 1
    fi
  fi

  ask "Netzwerk-Bridge" "vmbr0"
  local bridge="$REPLY"

  local default_dns="8.8.8.8"
  if [ -f /etc/resolv.conf ]; then
    default_dns="$(awk '/^nameserver/{print $2; exit}' /etc/resolv.conf 2>/dev/null || echo "8.8.8.8")"
  fi
  ask "DNS-Server" "$default_dns"
  local nameserver="$REPLY"

  ask "Storage-Pool (für Rootfs)" "local-lvm"
  local storage="$REPLY"

  ask "Template-Storage (für das Ubuntu-Image)" "local"
  local tmpl_storage="$REPLY"

  ask "Festplattengröße in GB" "16"
  local disk_gb="$REPLY"

  ask "RAM in MB" "4096"
  local memory="$REPLY"

  ask "CPU-Kerne" "2"
  local cores="$REPLY"

  echo ""
  echo "Root-Passwort für die LXC (Fallback-Zugang):"
  ask_password "Root-Passwort"
  local root_password="$REPLY"

  echo ""
  echo "Passwort für den neuen Benutzer 'rego' (sudo, für die tägliche Arbeit):"
  ask_password "rego-Passwort"
  local rego_password="$REPLY"

  echo ""
  echo "Suche verfügbares Ubuntu-26.04-Template..."
  local template_name template
  template_name="$(find_ubuntu_template)"
  if [ -z "$template_name" ]; then
    echo "Kein Ubuntu-26.04-Template gefunden. 'pveam available --section system | grep ubuntu' prüfen und Namen unten von Hand eintragen."
    ask "Template-Dateiname (unter $tmpl_storage:vztmpl/)" ""
    template_name="$REPLY"
  fi
  template="${tmpl_storage}:vztmpl/${template_name}"

  if ! pveam list "$tmpl_storage" 2>/dev/null | grep -q "$template_name"; then
    echo "Lade Template $template_name herunter..."
    pveam download "$tmpl_storage" "$template_name"
  fi

  local net0="name=eth0,bridge=${bridge}"
  if [ -n "$ip_cidr" ]; then
    net0="${net0},ip=${ip_cidr},gw=${gateway}"
  else
    net0="${net0},ip=dhcp"
  fi

  echo ""
  echo "=== Zusammenfassung ==="
  echo "  VMID:      $vmid"
  echo "  Hostname:  $hostname"
  echo "  Netzwerk:  $net0"
  echo "  DNS:       $nameserver"
  echo "  Storage:   $storage (${disk_gb}GB)"
  echo "  RAM/CPU:   ${memory}MB / ${cores} Kerne"
  echo "  Template:  $template"
  echo "  Benutzer:  root + rego (sudo)"
  echo "  Port:      5190 reserviert für den späteren REGObaseX1-Dev-Server"
  echo ""
  ask "Anlegen? (j/ja/y/yes zum Bestätigen)" "ja"
  if ! is_yes "$REPLY"; then
    echo "Abgebrochen."
    exit 0
  fi

  pct create "$vmid" "$template" \
    --hostname "$hostname" \
    --net0 "$net0" \
    --nameserver "$nameserver" \
    --rootfs "${storage}:${disk_gb}" \
    --memory "$memory" \
    --cores "$cores" \
    --unprivileged 1 \
    --features nesting=1,keyctl=1 \
    --password "$root_password" \
    --onboot 1

  echo "Starte LXC..."
  pct start "$vmid"

  echo "Warte, bis die LXC reagiert..."
  for _ in $(seq 1 30); do
    if pct exec "$vmid" -- true 2>/dev/null; then
      break
    fi
    sleep 2
  done

  echo "Prüfe Internetzugang im Container..."
  local network_ok=0
  for _ in $(seq 1 15); do
    if pct exec "$vmid" -- getent hosts deb.debian.org >/dev/null 2>&1; then
      network_ok=1
      break
    fi
    sleep 2
  done
  if [ "$network_ok" -ne 1 ]; then
    echo "" >&2
    echo "WARNUNG: Der Container hat nach 30s immer noch keinen Internetzugang." >&2
    echo "Prüfen: pct exec $vmid -- ping -c1 ${gateway:-<gateway>}" >&2
    echo "        pct exec $vmid -- cat /etc/resolv.conf" >&2
    echo "" >&2
  fi

  echo "Aktualisiere Paketlisten und installierte Pakete..."
  pct exec "$vmid" -- bash -c "apt-get update -qq && apt-get -y -qq upgrade"

  echo "Lege Benutzer 'rego' an..."
  # useradd/adduser --disabled-password statt --password, damit das
  # Passwort nicht als Klartext-Argument an einen inneren Prozess
  # durchgereicht wird (Review-Fund von create-vm.sh, hier von Anfang an
  # vermieden) -- printf ist ein Bash-Builtin, erscheint nie in `ps`.
  pct exec "$vmid" -- useradd -m -s /bin/bash -G sudo rego
  printf 'rego:%s\n' "$rego_password" | pct exec "$vmid" -- chpasswd

  local lxc_ip
  lxc_ip="$(pct exec "$vmid" -- hostname -I 2>/dev/null | awk '{print $1}')"

  echo ""
  echo "================================================================"
  echo " LXC $vmid ($hostname) läuft. IP: ${lxc_ip:-unbekannt}"
  echo ""
  echo " Login per SSH:"
  echo "   ssh rego@${lxc_ip:-<ip>}   (sudo-Benutzer, für die tägliche Arbeit)"
  echo "   ssh root@${lxc_ip:-<ip>}  (Fallback)"
  echo ""
  echo " Port 5190 ist für den späteren REGObaseX1-Dev-Server vorgesehen --"
  echo " noch nichts installiert, das kommt in einem eigenen nächsten Schritt."
  echo "================================================================"
}

main "$@"
