# REGOinstall

Öffentliches Installer-Repo für REGObase (die eigentliche App bleibt
privat). Zwei Skripte + eine kleine Weboberfläche.

## 1. Neue LXC anlegen (auf dem Proxmox-Host)

```bash
curl -fsSL https://raw.githubusercontent.com/epogo75/REGOinstall/main/create-lxc.sh -o create-lxc.sh
bash create-lxc.sh
```

Fragt interaktiv nach VMID, IP, Storage etc., legt eine Ubuntu-26.04-LXC
an. **Noch nicht gegen ein echtes Proxmox getestet** -- vor dem
produktiven Einsatz einmal durchgehen (siehe Kommentare im Skript,
insbesondere Docker-in-LXC-Feature-Flags).

## 2. REGObase installieren (in der neuen LXC)

```bash
curl -fsSL https://raw.githubusercontent.com/epogo75/REGOinstall/main/install-regobase.sh | bash
```

Private Repos -> `gh auth login` wird währenddessen interaktiv
aufgerufen. Installiert OS-Pakete, klont REGObase+REGOcore+
regoeldat-core, richtet `regobase.service`, den Matterbridge-Docker-
Container und die REGOinstall-Weboberfläche (Port 80) ein.

Backup einspielen: siehe Kommentarblock am Anfang von
`install-regobase.sh`, oder über die Weboberfläche (Restore-Knopf).

## 3. Update-/Backup-/Restore-Oberfläche (Port 80)

`update-service/regoinstall_updater.py` -- bewusst ohne Fremdabhängigkeiten
(nur Python-Standardbibliothek), eigener systemd-Dienst, unabhängig von
REGObase selbst (überlebt auch, wenn ein Update REGObase kaputt macht).

Konfiguriert über `/etc/regoinstall/apps.json` -- eine Liste von
Projekten. Aktuell nur REGObase, aber strukturiert für "später mehrere
REGO-Projekte auf derselben Maschine": neuer Eintrag reicht, kein
Codeänderung nötig.

Login: HTTP Basic Auth mit `REGOBASE_ADMIN_PASSWORD` aus
`/etc/regobase.env` (wird bei jeder Anfrage frisch gelesen).

Live-verifiziert (2026-08-15, auf 192.168.1.229 gegen die echte
REGObase-Installation dieses Servers, mit an das reale Verzeichnis-
Layout angepasstem `apps.json`):
- Auth-Erzwingung (401 ohne/mit falschem Passwort, GET und HEAD)
- Backup/Restore/Job-Runner-Logik per isoliertem Test gegen eine
  Wegwerf-DB (kein Zugriff auf echte Zugangsdaten nötig) -- dabei einen
  echten Bug gefunden und gefixt: `make_backup()` benannte Zielordner
  nur sekundengenau, zwei Backups in derselben Sekunde (z.B. das
  automatische Sicherheits-Backup direkt vor einem Restore) haben sich
  gegenseitig überschrieben. Jetzt Mikrosekunden-Auflösung.

**Noch nicht getestet:** die echten Update-/Backup-/Restore-Knöpfe im
Browser gegen die echte REGObase-Installation (braucht das echte
Admin-Passwort, das absichtlich nicht ausgelesen wurde).
