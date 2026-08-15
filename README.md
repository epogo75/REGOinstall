# REGOinstall

Öffentliches Installer-Repo für REGObase (die eigentliche App bleibt
privat). Ein Skript für den Proxmox-Host, danach läuft alles Weitere
über eine Weboberfläche auf Port 80 der neuen LXC.

## 1. Neue LXC anlegen (auf dem Proxmox-Host)

```bash
curl -fsSL https://raw.githubusercontent.com/epogo75/REGOinstall/main/create-lxc.sh -o create-lxc.sh
bash create-lxc.sh
```

Fragt interaktiv nach VMID (Default 300), IP, Gateway (Vorschlag aus der
IP abgeleitet, x.x.x.1), DNS, Storage etc., legt eine Ubuntu-26.04-LXC
an, aktualisiert sie (`apt update && apt upgrade`) und richtet am Ende
automatisch die Port-80-Weboberfläche ein (`bootstrap-port80.sh`,
läuft innerhalb der neuen LXC). **Noch nicht gegen ein echtes Proxmox
getestet** -- vor dem produktiven Einsatz einmal durchgehen.

Am Ende gibt das Skript die IP der neuen LXC aus -- im Browser öffnen.

## 2. REGObase installieren (im Browser, Port 80)

`http://<lxc-ip>/` zeigt vor der ersten Installation eine eigene
Installations-Seite (kein Login nötig -- der echte Türsteher ist unten
beschrieben). Ein Klick auf "Installation starten" führt
`install-regobase.sh` als Hintergrund-Job aus: OS-Pakete, Node.js,
Docker, klont REGObase+REGOcore+regoeldat-core, richtet
`regobase.service`, den Matterbridge-Container und die eigene
Weboberfläche final ein.

**GitHub-Anmeldung:** REGObase/REGOcore/regoeldat-core sind private
Repos, `gh auth login` läuft als Teil der Installation. Der
Anmeldecode (Device-Flow) erscheint im Log-Viewer derselben
Weboberfläche -- auf `github.com/login/device` eingeben und bestätigen,
dann läuft die Installation automatisch weiter. Bewusst KEINE
GitHub-OAuth-App mit fest registrierter Callback-URL -- jede frisch
erzeugte LXC hat eine andere IP, das hätte pro Box eine manuelle
GitHub-Konfiguration gebraucht.

Nach erfolgreicher Installation zeigt REGObase selbst am Ende ein
generiertes Admin-Passwort im Log (`admin` / `<generiert>`) -- damit
kann man sich danach auf derselben Port-80-Seite einloggen (jetzt
Basic Auth gegen REGObase's echte Benutzer-DB, siehe unten).

Backup einspielen: siehe Kommentarblock am Anfang von
`install-regobase.sh`, oder über die Weboberfläche (Restore-Knopf).

## 3. Update-/Backup-/Restore-/Install-Oberfläche (Port 80)

`update-service/regoinstall_updater.py` -- bewusst ohne Fremdabhängigkeiten
(nur Python-Standardbibliothek, `argon2-cffi` wird lazy importiert,
siehe unten), eigener systemd-Dienst, unabhängig von REGObase selbst
(überlebt auch, wenn ein Update REGObase kaputt macht).

Zwei Zustände (siehe `is_installed()` im Quelltext):
- **Nicht installiert**: `/etc/regoinstall/config.json`'s `auth_db` ist
  `null`. Seite ist absichtlich ohne Login erreichbar -- der Installations-
  Job selbst braucht eine echte, manuelle GitHub-Bestätigung, das ist
  der eigentliche Türsteher. Läuft unter dem nackten System-Python
  (kein `argon2-cffi` installiert, deshalb der lazy Import).
- **Installiert**: `auth_db` zeigt auf die echte `regobase.db`. Ab hier
  HTTP Basic Auth gegen REGObase's echte `users`-Tabelle (argon2-Hash,
  Rolle muss ADMIN sein, Konto aktiv) -- NICHT gegen
  `REGOBASE_ADMIN_PASSWORD` aus `/etc/regobase.env` (das wird von
  REGObase selbst nur beim allerersten Start verwendet, um das initiale
  "admin"-Konto anzulegen, und hat mit dem, was echte Nutzer später
  verwenden, nichts mehr zu tun -- ein erster Entwurf prüfte
  fälschlich dagegen, echte Admins mit einem anderen Konto kamen nicht
  rein). Läuft dann unter REGObase's eigenem venv-Python (hat
  `argon2-cffi` schon installiert).

Konfiguriert über `/etc/regoinstall/apps.json` -- eine Liste von
Projekten. Aktuell nur REGObase, aber strukturiert für "später mehrere
REGO-Projekte auf derselben Maschine": neuer Eintrag reicht, kein
Codeänderung nötig.

**Verifiziert** (2026-08-15, teils live auf 192.168.1.229 gegen die
echte REGObase-Installation, teils isoliert gegen Wegwerf-Daten ohne
echte Zugangsdaten zu lesen):
- Auth-Erzwingung (401 ohne/mit falschem Passwort, GET und HEAD)
- Kompletter Vorinstallations-Fluss end-to-end (echter HTTP-Server,
  Wegwerf-Config): `GET /` ohne Auth zeigt die Install-Seite, `POST
  /install` startet den konfigurierten `install_cmd` wirklich als
  Hintergrund-Job (Log enthält echte Subprozess-Ausgabe), sobald
  `auth_db` danach auf eine echte Nutzer-DB zeigt verlangt `GET /`
  Basic Auth, und das generierte Bootstrap-Admin-Konto kann sich
  erfolgreich anmelden.
- `argon2-cffi` läuft nachweislich NICHT beim Modul-Import (in einem
  eigens erzeugten venv OHNE das Paket getestet) -- ohne den Lazy-
  Import wäre der Dienst auf einer frischen LXC (bootstrap-port80.sh
  installiert bewusst kein argon2) sofort beim Start abgestürzt.
- Backup/Restore/Job-Runner-Logik inkl. zweier echter, per Test
  gefundener Bugs: `make_backup()` benannte Zielordner nur
  sekundengenau (zwei Backups in derselben Sekunde überschrieben sich
  gegenseitig, jetzt Mikrosekunden-Auflösung), und `backup_name` im
  Restore-Endpunkt kam ungeprüft aus der URL (Path Traversal, jetzt
  Allowlist gegen tatsächlich existierende Backup-Ordner).

**Noch nicht getestet:** die echten Update-/Backup-/Restore-Knöpfe im
Browser gegen eine echte, laufende Installation mit echtem Admin-Login
(die Zugangsdaten wurden absichtlich nie ausgelesen); `create-lxc.sh`
gegen ein echtes Proxmox (keine Proxmox-Host-Zugriffsmöglichkeit in
dieser Session).
