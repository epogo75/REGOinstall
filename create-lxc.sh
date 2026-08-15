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

REGOINSTALL_VERSION="0.1"
REGOINSTALL_BUILD="2026-08-15.1"
echo "REGOinstall v${REGOINSTALL_VERSION} (build ${REGOINSTALL_BUILD}) -- create-lxc.sh"

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
  # Klartext am Bildschirm) und zweimal abgefragt, damit ein Tippfehler
  # nicht erst beim ersten Login in der neuen LXC aufällt -- da käme man
  # sonst gar nicht mehr rein.
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

setup_usb_passthrough() {
  # ELDAT-USB-Stick (Easywave-Funk-Dongle) in die LXC durchreichen --
  # REGObase/regoeldat_core spricht ihn über pyserial an
  # (serial.tools.list_ports.comports(), sucht/probiert alle /dev/tty*
  # der Reihe nach durch, siehe regoeldat_core/eldat/discovery.py), es
  # kommt also nur darauf an, dass IRGENDEIN passender /dev/tty*-Knoten
  # in der LXC existiert -- kein bestimmter Pfad/Name nötig.
  #
  # NICHT gegen echtes Proxmox getestet (wie das ganze Skript). Nutzt
  # `pct set -devX`, das moderne, native Geräte-Passthrough für
  # unprivilegierte Container (Proxmox VE 8+) -- auf älteren Proxmox-
  # Versionen ggf. stattdessen von Hand über
  # /etc/pve/lxc/<vmid>.conf's lxc.cgroup2.devices.allow +
  # lxc.mount.entry nötig.
  local vmid="$1"

  ask "USB-Passthrough für den ELDAT-Stick einrichten? (j/ja/y/yes)" "nein"
  if ! is_yes "$REPLY"; then
    return 0
  fi

  echo ""
  echo "Verfügbare USB-Seriell-Geräte auf diesem Proxmox-Host:"
  local by_id_dir="/dev/serial/by-id"
  local -a devices=()
  if [ -d "$by_id_dir" ]; then
    while IFS= read -r -d '' dev; do
      devices+=("$dev")
    done < <(find "$by_id_dir" -maxdepth 1 -type l -print0 2>/dev/null)
  fi
  if [ "${#devices[@]}" -eq 0 ]; then
    # Fallback ohne stabile by-id-Symlinks (z.B. sehr generischer
    # USB-Seriell-Chip ohne eigene Seriennummer) -- direkt die
    # ttyUSB*/ttyACM*-Knoten anbieten. Weniger robust über einen
    # Host-Neustart/Neu-Einstecken hinweg, aber besser als gar keine
    # Option zu haben.
    while IFS= read -r -d '' dev; do
      devices+=("$dev")
    done < <(find /dev -maxdepth 1 \( -name 'ttyUSB*' -o -name 'ttyACM*' \) -print0 2>/dev/null)
  fi

  if [ "${#devices[@]}" -eq 0 ]; then
    echo "Kein USB-Seriell-Gerät gefunden. Stick eingesteckt? Später manuell:"
    echo "  pct set $vmid -dev0 /dev/ttyUSBx"
    return 0
  fi

  local i=1
  for dev in "${devices[@]}"; do
    echo "  $i) $dev"
    i=$((i + 1))
  done
  ask "Welches Gerät? (Nummer)" "1"
  local choice="$REPLY"
  if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "${#devices[@]}" ]; then
    echo "Ungültige Auswahl, überspringe USB-Passthrough." >&2
    return 0
  fi
  local chosen="${devices[$((choice - 1))]}"

  # by-id-Symlink auf den echten Geräteknoten auflösen (z.B.
  # /dev/ttyUSB0) -- pct set trägt den Pfad 1:1 in die Container-
  # Konfiguration ein, ohne dass in der LXC selbst zwingend udev läuft,
  # das den by-id-Symlink dort neu erzeugen würde.
  local resolved
  resolved="$(readlink -f "$chosen")"
  echo "Reiche $resolved ($chosen) an LXC $vmid durch..."
  pct set "$vmid" -dev0 "$resolved"
  echo "Fertig -- in der LXC sollte $resolved jetzt existieren (Setup > ELDAT in REGObase zum Prüfen/Auswählen)."
  echo "Falls nicht sofort sichtbar: pct exec $vmid -- ls -la $resolved -- notfalls hilft ein 'pct reboot $vmid'."
}

main() {
  require_pve

  echo "=== REGObase-LXC anlegen ==="
  echo ""

  ask "VMID" "300"
  local vmid="$REPLY"

  ask "Hostname" "REGObase"
  local hostname="$REPLY"

  ask "IP-Adresse (CIDR, z.B. 192.168.1.230/24) -- leer für DHCP" ""
  local ip_cidr="$REPLY"

  local gateway=""
  if [ -n "$ip_cidr" ]; then
    # Echter Bug, live gefunden: der Gateway-Prompt hatte früher keinen
    # Default, und ein leer gelassenes Feld führte dazu, dass "gw=" im
    # net0-String komplett fehlte -- der Container bekam eine feste IP
    # OHNE Standardroute, also kein Internetzugang, obwohl "pct create"
    # anstandslos durchlief. Jetzt ein sinnvoller Vorschlag aus der
    # eingegebenen IP (x.x.x.1, die in den allermeisten Heim-/Büro-
    # Netzen übliche Gateway-Adresse) statt eines leeren Defaults --
    # Enter drücken reicht jetzt, außer die echte Gateway-IP weicht ab.
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

  ask_password "Root-Passwort für die LXC"
  local root_password="$REPLY"

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
    net0="${net0},gw=${gateway}"
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
  echo ""
  ask "Anlegen? (j/ja/y/yes zum Bestätigen)" "ja"
  if ! is_yes "$REPLY"; then
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

  # "pct exec -- true" prüft nur, ob der Container selbst läuft, NICHT
  # ob das Netzwerk (Gateway/DNS) tatsächlich funktioniert -- genau der
  # Fall, der live zum "kann nichts laden"-Bug geführt hat, wäre hier
  # anstandslos durchgelaufen. Echten Konnektivitätstest nachschalten,
  # mit klarer Fehlermeldung statt eines stillen apt-get-Timeouts weiter
  # unten.
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
    echo "Skript 2 (install-regobase.sh) wird trotzdem fehlschlagen, bis das behoben ist." >&2
    echo "" >&2
  fi

  echo "Aktualisiere Paketlisten und installierte Pakete..."
  pct exec "$vmid" -- bash -c "apt-get update -qq && apt-get -y -qq upgrade && apt-get install -y -qq curl"

  setup_usb_passthrough "$vmid"

  echo "Richte die Port-80-Weboberfläche ein (nur die Oberfläche, REGObase"
  echo "selbst installierst du danach per Klick im Browser)..."
  pct exec "$vmid" -- bash -c "curl -fsSL https://raw.githubusercontent.com/epogo75/REGOinstall/main/bootstrap-port80.sh | bash"

  local lxc_ip
  lxc_ip="$(pct exec "$vmid" -- hostname -I 2>/dev/null | awk '{print $1}')"

  echo ""
  echo "================================================================"
  echo " LXC $vmid ($hostname) läuft. IP: ${lxc_ip:-unbekannt}"
  echo ""
  echo " Nächster Schritt -- im Browser öffnen und auf 'Installieren' klicken:"
  echo "   http://${lxc_ip:-<ip>}/"
  echo " Der GitHub-Anmeldecode (privates Repo) erscheint danach im Log"
  echo " auf derselben Seite -- auf github.com/login/device bestätigen."
  echo "================================================================"
}

main "$@"
