#!/usr/bin/env bash
# REGOinstall :: create-vm.sh -- erzeugt eine echte Proxmox-VM (kein LXC)
# für Entwicklungs-/Testzwecke, z.B. REGObaseX1 gegen einen echten Gira X1
# zu entwickeln, ohne die produktive REGObase-LXC anzufassen. LÄUFT AUF DEM
# PROXMOX-HOST SELBST (braucht `qm`), nicht in einem Container.
#
# Anders als create-lxc.sh (pct create + Ubuntu-LXC-Template) nutzt dieses
# Skript ein Ubuntu-26.04-Server-Cloud-Image + Cloud-Init, das ist der
# Standardweg für unbeaufsichtigte Proxmox-VM-Erzeugung. Legt einen neuen
# Benutzer "rego" (sudo, Passwort-Login) UND ein Root-Passwort an, beide
# interaktiv abgefragt (maskiert, nicht im Skript hinterlegt -- dieses Repo
# ist öffentlich, ein hartkodiertes Passwort in einer committeten Datei wäre
# ein echtes Sicherheitsproblem).
#
# NICHT gegen ein echtes Proxmox getestet (diese Session lief selbst in
# einem LXC ohne Zugriff auf den Proxmox-Host, wie schon bei create-lxc.sh)
# -- vor dem produktiven Einsatz einmal durchgehen. Voraussetzung: die
# gewählte Template-Storage muss den Content-Typ "Snippets" aktiviert haben
# (Proxmox-Weboberfläche: Datacenter > Storage > <storage> > Bearbeiten >
# Inhalt > "Snippets" ankreuzen) -- Standardmäßig ist das bei "local" NICHT
# automatisch aktiv, das Skript prüft das und bricht mit einer klaren
# Fehlermeldung ab statt einem kryptischen qm-Fehler.
#
# Usage (auf dem Proxmox-Host, als root):
#   curl -fsSL https://raw.githubusercontent.com/epogo75/REGOinstall/main/create-vm.sh -o create-vm.sh
#   bash create-vm.sh
#
# Fragt interaktiv: VMID, IP/Gateway, Storage, Ressourcen, rego-Passwort,
# Root-Passwort. Danach: Cloud-Image laden (falls nicht schon lokal),
# `qm create` + Cloud-Init-Snippet + `qm start`, wartet auf den Guest-Agent,
# gibt am Ende die IP aus.

set -euo pipefail

REGOINSTALL_VERSION="0.1"
REGOINSTALL_BUILD="2026-08-16.1"
echo "REGOinstall v${REGOINSTALL_VERSION} (build ${REGOINSTALL_BUILD}) -- create-vm.sh"

UBUNTU_CLOUDIMG_URL="https://cloud-images.ubuntu.com/releases/26.04/release/ubuntu-26.04-server-cloudimg-amd64.img"

require_pve() {
  if ! command -v qm >/dev/null 2>&1; then
    echo "Kein 'qm' gefunden -- dieses Skript muss auf dem Proxmox-Host laufen, nicht in einem Container." >&2
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

require_snippets_support() {
  # Cloud-Init mit zwei eigenen Benutzern (root + rego) braucht ein
  # eigenes user-data-Snippet (--cicustom). Proxmox erlaubt das nur auf
  # Storages, die den Content-Typ "snippets" aktiviert haben -- ohne
  # diese Prüfung würde `qm set --cicustom` erst beim eigentlichen
  # Anlegen mit einer wenig hilfreichen Fehlermeldung abbrechen.
  local storage="$1"
  if ! pvesm status -content snippets 2>/dev/null | awk '{print $1}' | grep -qx "$storage"; then
    echo "" >&2
    echo "FEHLER: Storage '$storage' hat den Content-Typ 'Snippets' nicht aktiviert." >&2
    echo "Proxmox-Weboberfläche: Datacenter > Storage > $storage > Bearbeiten > Inhalt > 'Snippets' ankreuzen." >&2
    echo "Danach dieses Skript erneut ausführen." >&2
    exit 1
  fi
}

fetch_cloud_image() {
  # Lädt das Ubuntu-26.04-Server-Cloud-Image einmalig herunter und cached
  # es unter /var/lib/vz/template/iso -- spätere Läufe des Skripts (z.B.
  # eine zweite Test-VM) brauchen dann keinen erneuten Download.
  local cache_dir="/var/lib/vz/template/iso"
  local cache_file="${cache_dir}/ubuntu-26.04-server-cloudimg-amd64.img"
  mkdir -p "$cache_dir"
  if [ ! -f "$cache_file" ]; then
    echo "Lade Ubuntu-26.04-Server-Cloud-Image herunter (einmalig, ca. 600MB)..." >&2
    curl -fsSL "$UBUNTU_CLOUDIMG_URL" -o "$cache_file.partial"
    mv "$cache_file.partial" "$cache_file"
  fi
  echo "$cache_file"
}

write_cloudinit_snippet() {
  # Eigenes user-data-Snippet statt der eingebauten --ciuser/--cipassword-
  # Flags: Proxmox unterstützt darüber nur EINEN Benutzer, hier sollen
  # aber zwei eigene Passwörter (root + rego) gesetzt werden. ssh_pwauth
  # + disable_root:false sind nötig, weil Ubuntus Cloud-Init-Vorlage
  # Root-Login und Passwort-SSH-Login standardmäßig abschaltet.
  local storage="$1" hostname="$2" root_password="$3" rego_password="$4"
  local snippet_dir="/var/lib/vz/snippets"
  local snippet_file="${hostname}-user-data.yml"
  mkdir -p "$snippet_dir"
  cat > "${snippet_dir}/${snippet_file}" <<EOF
#cloud-config
hostname: ${hostname}
disable_root: false
ssh_pwauth: true
chpasswd:
  expire: false
  users:
    - {name: root, password: '${root_password}', type: text}
    - {name: rego, password: '${rego_password}', type: text}
users:
  - name: rego
    groups: [sudo]
    shell: /bin/bash
    lock_passwd: false
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
  - name: root
    lock_passwd: false
package_update: true
packages:
  - qemu-guest-agent
runcmd:
  - systemctl enable --now qemu-guest-agent
EOF
  echo "${storage}:snippets/${snippet_file}"
}

main() {
  require_pve

  echo "=== Test-VM für REGObaseX1 anlegen ==="
  echo ""

  ask "VMID" "301"
  local vmid="$REPLY"

  ask "Hostname" "REGObaseX1-dev"
  local hostname="$REPLY"

  ask "IP-Adresse (CIDR, z.B. 192.168.1.235/24) -- leer für DHCP" ""
  local ip_cidr="$REPLY"

  local gateway=""
  if [ -n "$ip_cidr" ]; then
    # Gleicher Fix wie in create-lxc.sh: ohne Default hier landet man
    # sonst bei einer festen IP ohne Standardroute -> kein Internet.
    local suggested_gw
    suggested_gw="$(echo "$ip_cidr" | sed -E 's#^([0-9]+\.[0-9]+\.[0-9]+)\.[0-9]+/.*#\1.1#')"
    ask "Gateway" "$suggested_gw"
    gateway="$REPLY"
    if [ -z "$gateway" ]; then
      echo "Gateway darf bei einer festen IP nicht leer sein (sonst kein Internetzugang in der VM)." >&2
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

  ask "Storage-Pool (für Disk + Cloud-Init-Snippet)" "local-lvm"
  local storage="$REPLY"

  ask "Storage für das Cloud-Image/Snippets (braucht Content-Typ 'Snippets')" "local"
  local snippet_storage="$REPLY"

  ask "Festplattengröße in GB" "20"
  local disk_gb="$REPLY"

  ask "RAM in MB" "4096"
  local memory="$REPLY"

  ask "CPU-Kerne" "4"
  local cores="$REPLY"

  echo ""
  echo "Root-Passwort für die VM (Fallback-Zugang):"
  ask_password "Root-Passwort"
  local root_password="$REPLY"

  echo ""
  echo "Passwort für den neuen Benutzer 'rego' (sudo, für die tägliche Arbeit):"
  ask_password "rego-Passwort"
  local rego_password="$REPLY"

  require_snippets_support "$snippet_storage"

  echo ""
  echo "=== Zusammenfassung ==="
  echo "  VMID:      $vmid"
  echo "  Hostname:  $hostname"
  echo "  Netzwerk:  ${ip_cidr:-DHCP}${gateway:+, Gateway $gateway}"
  echo "  DNS:       $nameserver"
  echo "  Storage:   $storage (${disk_gb}GB Disk)"
  echo "  RAM/CPU:   ${memory}MB / ${cores} Kerne"
  echo "  Benutzer:  root + rego (Passwörter wie eingegeben, nicht angezeigt)"
  echo "  Port:      5190 reserviert für den späteren REGObaseX1-Dev-Server"
  echo ""
  ask "Anlegen? (j/ja/y/yes zum Bestätigen)" "ja"
  if ! is_yes "$REPLY"; then
    echo "Abgebrochen."
    exit 0
  fi

  local cloud_image
  cloud_image="$(fetch_cloud_image)"

  local cicustom_ref
  cicustom_ref="$(write_cloudinit_snippet "$snippet_storage" "$hostname" "$root_password" "$rego_password")"

  echo "Lege VM an..."
  qm create "$vmid" \
    --name "$hostname" \
    --memory "$memory" \
    --cores "$cores" \
    --cpu host \
    --net0 "virtio,bridge=${bridge}" \
    --scsihw virtio-scsi-pci \
    --ostype l26 \
    --agent enabled=1 \
    --onboot 1

  echo "Importiere Cloud-Image als Disk..."
  qm importdisk "$vmid" "$cloud_image" "$storage" >/dev/null
  qm set "$vmid" --scsi0 "${storage}:vm-${vmid}-disk-0"
  qm set "$vmid" --boot c --bootdisk scsi0
  qm set "$vmid" --ide2 "${storage}:cloudinit"
  qm set "$vmid" --serial0 socket --vga serial0

  echo "Vergrößere Disk auf ${disk_gb}GB..."
  qm resize "$vmid" scsi0 "${disk_gb}G"

  echo "Richte Netzwerk + Cloud-Init ein..."
  if [ -n "$ip_cidr" ]; then
    qm set "$vmid" --ipconfig0 "ip=${ip_cidr},gw=${gateway}"
  else
    qm set "$vmid" --ipconfig0 "ip=dhcp"
  fi
  qm set "$vmid" --nameserver "$nameserver"
  qm set "$vmid" --cicustom "user=${cicustom_ref}"

  echo "Starte VM..."
  qm start "$vmid"

  echo "Warte auf den QEMU-Guest-Agent (Cloud-Init braucht beim ersten Boot etwas länger)..."
  local agent_ok=0
  for _ in $(seq 1 60); do
    if qm agent "$vmid" ping >/dev/null 2>&1; then
      agent_ok=1
      break
    fi
    sleep 5
  done

  if [ "$agent_ok" -ne 1 ]; then
    echo "" >&2
    echo "WARNUNG: Guest-Agent antwortet nach 5 Minuten immer noch nicht." >&2
    echo "VM läuft eventuell trotzdem -- Konsole prüfen: qm terminal $vmid" >&2
    echo "================================================================"
    exit 0
  fi

  local vm_ip
  vm_ip="$(qm guest cmd "$vmid" network-get-interfaces 2>/dev/null \
    | grep -A3 '"ip-address"' \
    | grep '"ip-address"' \
    | grep -v '127\.0\.0\.1' \
    | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' \
    | head -1 || true)"

  echo ""
  echo "================================================================"
  echo " VM $vmid ($hostname) läuft. IP: ${vm_ip:-unbekannt, siehe 'qm guest cmd $vmid network-get-interfaces'}"
  echo ""
  echo " Login per SSH:"
  echo "   ssh rego@${vm_ip:-<ip>}   (sudo-Benutzer, für die tägliche Arbeit)"
  echo "   ssh root@${vm_ip:-<ip>}  (Fallback)"
  echo ""
  echo " Port 5190 ist für den späteren REGObaseX1-Dev-Server vorgesehen --"
  echo " noch nichts installiert, das kommt in einem eigenen nächsten Schritt."
  echo "================================================================"
}

main "$@"
