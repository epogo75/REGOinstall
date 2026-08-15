#!/usr/bin/env bash
# REGOinstall :: Skript 1 -- erzeugt eine neue Ubuntu-LXC auf einem
# Proxmox-Host für REGObase. LÄUFT AUF DEM PROXMOX-HOST SELBST, nicht in
# einem Container (ein LXC kann sich nicht selbst erzeugen).
#
# NICHT live gegen ein echtes Proxmox getestet (diese Session lief selbst
# in einem LXC ohne Zugriff auf den Proxmox-Host) -- vor dem produktiven
# Einsatz einmal durchgehen und die Proxmox-spezifischen Annahmen prüfen
# (Storage-Pool-Name, Bridge-Name, Docker-in-LXC-Feature-Flags je nach
# Proxmox-/Kernel-Version).
#
# Usage (auf dem Proxmox-Host, als root):
#   curl -fsSL https://raw.githubusercontent.com/epogo75/REGOinstall/main/create-lxc.sh -o create-lxc.sh
#   bash create-lxc.sh
#
# Fragt interaktiv: VMID, IP/Gateway, Storage, Ressourcen, Root-Zugang.
# Danach: `pct create` + `pct start`, wartet auf Netzwerk, gibt den
# nächsten Schritt aus (Skript 2 innerhalb der LXC ausführen).

set -euo pipefail

require_pve() {
  if ! command -v pct >/dev/null 2>&1; then
    echo "Kein 'pct' gefunden -- dieses Skript muss auf dem Proxmox-Host laufen, nicht in einem Container." >&2
    exit 1
  fi
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

find_ubuntu_template() {
  # Sucht das neueste verfügbare Ubuntu-26.04-Template statt einen
  # konkreten Dateinamen zu hardcoden (die ändern sich mit jedem
  # Proxmox-Template-Update). Lädt die Liste vorher einmal neu.
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

  echo "=== REGObase-LXC anlegen ==="
  echo ""

  local next_id
  next_id="$(pvesh get /cluster/nextid 2>/dev/null || echo 200)"
  ask "VMID" "$next_id"
  local vmid="$REPLY"

  ask "Hostname" "REGObase"
  local hostname="$REPLY"

  ask "IP-Adresse (CIDR, z.B. 192.168.1.230/24) -- leer für DHCP" ""
  local ip_cidr="$REPLY"

  local gateway=""
  if [ -n "$ip_cidr" ]; then
    ask "Gateway (z.B. 192.168.1.1)" ""
    gateway="$REPLY"
  fi

  ask "Netzwerk-Bridge" "vmbr0"
  local bridge="$REPLY"

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

  ask "Root-Passwort für die LXC" ""
  local root_password="$REPLY"
  if [ -z "$root_password" ]; then
    echo "Root-Passwort ist erforderlich." >&2
    exit 1
  fi

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
    net0="${net0},ip=${ip_cidr}"
    [ -n "$gateway" ] && net0="${net0},gw=${gateway}"
  else
    net0="${net0},ip=dhcp"
  fi

  echo ""
  echo "=== Zusammenfassung ==="
  echo "  VMID:      $vmid"
  echo "  Hostname:  $hostname"
  echo "  Netzwerk:  $net0"
  echo "  Storage:   $storage (${disk_gb}GB)"
  echo "  RAM/CPU:   ${memory}MB / ${cores} Kerne"
  echo "  Template:  $template"
  echo ""
  ask "Anlegen? (ja/nein)" "ja"
  if [ "$REPLY" != "ja" ]; then
    echo "Abgebrochen."
    exit 0
  fi

  # unprivileged + nesting/keyctl: Voraussetzung für Docker innerhalb
  # der LXC auf aktuellen Proxmox-/Kernel-Versionen. Auf älteren Setups
  # kann stattdessen ein privilegierter Container (--unprivileged 0)
  # nötig sein -- falls Docker im zweiten Skript nicht startet, das hier
  # als Erstes prüfen.
  pct create "$vmid" "$template" \
    --hostname "$hostname" \
    --net0 "$net0" \
    --rootfs "${storage}:${disk_gb}" \
    --memory "$memory" \
    --cores "$cores" \
    --unprivileged 1 \
    --features nesting=1,keyctl=1 \
    --password "$root_password" \
    --onboot 1

  echo "Starte LXC..."
  pct start "$vmid"

  echo "Warte auf Netzwerk..."
  for _ in $(seq 1 30); do
    if pct exec "$vmid" -- true 2>/dev/null; then
      break
    fi
    sleep 2
  done

  # apt-Cache in einer frischen LXC ist oft leer/veraltet -- curl für
  # Skript 2 sicherstellen.
  pct exec "$vmid" -- bash -c "apt-get update -qq && apt-get install -y -qq curl" || true

  local lxc_ip
  lxc_ip="$(pct exec "$vmid" -- hostname -I 2>/dev/null | awk '{print $1}')"

  echo ""
  echo "================================================================"
  echo " LXC $vmid ($hostname) läuft. IP: ${lxc_ip:-unbekannt}"
  echo ""
  echo " Nächster Schritt -- in die LXC rein und Skript 2 starten:"
  echo "   pct enter $vmid"
  echo "   curl -fsSL https://raw.githubusercontent.com/epogo75/REGOinstall/main/install-regobase.sh | bash"
  echo "================================================================"
}

main "$@"
