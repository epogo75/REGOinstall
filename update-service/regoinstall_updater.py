#!/usr/bin/env python3
"""REGOinstall :: kleine, absichtlich abhängigkeitsfreie (nur Python-
Standardbibliothek) Weboberfläche auf Port 80 -- Startseite für
installierte REGO-Projekte auf dieser Maschine, mit Update- und
Backup-Knopf pro Projekt.

Bewusst UNABHÄNGIG von REGObase selbst (eigener Prozess, eigener
systemd-Dienst, keine gemeinsamen Python-Abhängigkeiten/venvs) --
Grundidee: wenn ein Update REGObase kaputt macht, muss dieser Dienst
trotzdem noch laufen, um das Update-Log zu zeigen oder ein Backup
zurückzuspielen. Deshalb auch keine Fremdpakete (kein Flask/FastAPI),
nur http.server aus der Standardbibliothek.

Konfiguration: /etc/regoinstall/apps.json, eine Liste von Projekten
(aktuell nur REGObase, aber strukturiert für "später mehrere REGO-
Projekte" -- neue Einträge hinzufügen reicht, kein Code ändern nötig):

[
  {
    "name": "REGObase",
    "url": "http://192.168.1.230:5175",
    "service": "regobase.service",
    "update_cmd": ["bash", "-c", "curl -fsSL https://raw.githubusercontent.com/epogo75/REGOinstall/main/install-regobase.sh | bash -s -- --update"],
    "backup_db": "/opt/regobase-stack/REGObase/backend/data/regobase.db",
    "backup_extra_files": ["/etc/regobase.env"]
  }
]

Auth: HTTP Basic gegen die ECHTEN REGObase-Benutzerkonten (argon2-Hash
in der users-Tabelle von REGObase's eigener SQLite-DB, Rolle muss
ADMIN sein, Konto muss aktiv sein) -- NICHT gegen REGOBASE_ADMIN_PASSWORD
aus /etc/regobase.env. Dieser Wert wird von REGObase selbst nur EIN
EINZIGES MAL beim allerersten Start verwendet, um das initiale
"admin"-Konto anzulegen (siehe regobase/auth/bootstrap.py: "No-op once
any user exists") -- er verändert sich nie wieder und hat mit dem
Passwort, das echte Nutzer (z.B. ein zweites angelegtes Konto) tatsächlich
verwenden, nichts mehr zu tun. Ein erster Entwurf dieses Skripts prüfte
fälschlich gegen diesen Wert -- echte Admins mit einem anderen Konto als
dem allerersten Bootstrap-"admin" kamen dadurch gar nicht rein.

Die Benutzer-DB liegt in AUTH_DB (siehe /etc/regoinstall/config.json,
Feld "auth_db" -- zeigt auf die DB des "Haupt"-REGO-Projekts auf dieser
Maschine, das die gemeinsame Admin-Anmeldung stellt). Deshalb muss
dieser Dienst mit einem Python laufen, das das "argon2-cffi"-Paket hat
-- am einfachsten das venv des zugehörigen REGObase-Checkouts
wiederverwenden (schon vorhanden, keine zusätzliche Installation nötig),
siehe install-regobase.sh's regoinstall.service-ExecStart.

Zwei Zustände, per is_installed() unterschieden (config.json's "auth_db"
zeigt auf eine existierende Datei oder nicht):

- NICHT installiert (frische LXC, bootstrap-port80.sh lief, aber noch
  kein REGObase): "auth_db" ist null. Die Seite ist absichtlich OHNE
  Basic-Auth erreichbar -- der echte Türsteher ist `gh auth login`'s
  Device-Code-Flow, aber als eigener, ERSTER Schritt (direkter
  Nutzerwunsch: "Der Github Login als erstes - bevor man irgendwas
  startet", nachdem die ursprüngliche Version den Code mitten in einem
  langen Installations-Log versteckte). "Mit GitHub anmelden"-Knopf
  POSTet auf /connect-github (config.json's "connect_github_cmd" --
  installiert nur die gh-CLI + `gh auth login`, sonst nichts). Erst
  wenn gh_auth_status() danach True liefert, zeigt die Seite den
  "Installation starten"-Knopf (POST /install, config.json's
  "install_cmd"). Eine echte GitHub-OAuth-App mit fester Callback-URL
  wurde bewusst NICHT gebaut -- das würde pro LXC eine andere IP/
  Callback-Registrierung brauchen, passt nicht zu "soll auf jedem
  frisch erzeugten Container funktionieren". Beide Knöpfe laufen über
  denselben Job-Runner (run_job_async) wie Update/Backup, mit einem
  live per JS pollendem Log-Fenster direkt auf der Seite (kein
  manuelles Neuladen mehr, direkter Nutzerwunsch) statt nur einem Link
  auf eine separate /log-Seite.
- Installiert: "auth_db" zeigt auf die echte regobase.db. Ab hier normale
  Basic-Auth gegen die users-Tabelle (siehe oben), Update/Backup/Restore-
  Karten statt der Installations-Seite.

/etc/regoinstall/config.json (von bootstrap-port80.sh angelegt, von
install-regobase.sh am Ende aktualisiert):

{
  "auth_db": null,
  "connect_github_cmd": ["bash", "-c", "curl -fsSL https://raw.githubusercontent.com/epogo75/REGOinstall/main/install-regobase.sh | bash -s -- --connect-github"],
  "install_cmd": ["bash", "-c", "curl -fsSL https://raw.githubusercontent.com/epogo75/REGOinstall/main/install-regobase.sh | bash"]
}
"""

import base64
import html
import io
import json
import re
import secrets
import shutil
import sqlite3
import subprocess
import tarfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# Von Hand hochgezählt bei jeder inhaltlichen Änderung (direkter
# Nutzerwunsch, nach mehrfacher Verwirrung darüber, ob eine frische LXC
# wirklich schon den neuesten Stand hat) -- BUILD ist das Datum/die
# laufende Nummer des jeweiligen Edits, sichtbar im Seitenkopf jeder
# Seite, damit sich "läuft hier wirklich die Version, die ich denke"
# ohne journalctl-Grabbelei beantworten lässt.
VERSION = "0.1"
BUILD = "2026-08-15.1"

APPS_CONFIG = Path("/etc/regoinstall/apps.json")
CONFIG = Path("/etc/regoinstall/config.json")
LOG_DIR = Path("/var/log/regoinstall")
BACKUP_DIR = Path("/var/backups/regoinstall")
# _pending_first_login/_first_login_token (siehe unten) waren ursprünglich
# reine Prozess-Globals -- ein echter Vorfall live gefunden: ein Neustart
# des Dienstes genau in dem Fenster zwischen Installationsende und erstem
# Login (z.B. durch ein Deploy dieses Skripts selbst, wie hier passiert)
# löscht beide sofort, obwohl das Cookie im Browser des Nutzers noch gültig
# wäre. Deshalb hier auf Platte gespiegelt (0600, root-only -- gleiche
# Sensitivität wie das Klartext-Passwort im Install-Log).
FIRST_LOGIN_STATE = Path("/etc/regoinstall/first_login_state.json")
PORT = 80

LOG_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.chmod(0o700)  # enthält irgendwann Kopien von /etc/regobase.env

_job_lock = threading.Lock()
_job_running = False
_job_name: str | None = None
_hasher = None
_dummy_hash = None


def gh_auth_status() -> tuple[bool, str | None]:
    """(angemeldet?, GitHub-Login-Name oder None). `gh` existiert vor der
    Installation noch nicht (bootstrap-port80.sh installiert nur
    python3+curl) -- fehlender Befehl zählt als "nicht angemeldet",
    kein Fehler."""
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, None
    if result.returncode != 0:
        return False, None
    login = result.stdout.strip()
    return (True, login) if login else (False, None)


def _get_hasher():
    # "argon2-cffi" NICHT auf Modulebene importieren: vor der ersten
    # Installation läuft dieser Dienst unter dem nackten System-Python
    # (bootstrap-port80.sh installiert bewusst nur python3+curl, siehe
    # is_installed()) -- ein Import auf Modulebene würde den kompletten
    # Prozess schon beim Start crashen, lange bevor überhaupt die
    # Installations-Seite (die gar keine Passwort-Prüfung braucht)
    # angezeigt werden könnte. check_auth() wird ohnehin nur erreicht,
    # nachdem is_installed() bereits bestätigt hat, dass REGObase (und
    # damit dessen venv mit argon2-cffi) existiert.
    global _hasher, _dummy_hash
    if _hasher is None:
        from argon2 import PasswordHasher

        _hasher = PasswordHasher()
        # Fixed dummy hash to verify against when a username isn't found,
        # so a "no such user" response takes the same ~100ms as a real
        # wrong-password check instead of returning near-instantly --
        # otherwise response timing alone would let an attacker enumerate
        # valid usernames.
        _dummy_hash = _hasher.hash("not-a-real-password")
    return _hasher, _dummy_hash


def load_apps() -> list[dict]:
    if not APPS_CONFIG.exists():
        return []
    return json.loads(APPS_CONFIG.read_text())


def load_config() -> dict:
    if not CONFIG.exists():
        return {}
    return json.loads(CONFIG.read_text())


def auth_db_path() -> Path | None:
    value = load_config().get("auth_db")
    return Path(value) if value else None


def is_installed() -> bool:
    # Vor der ersten echten Installation gibt es noch keine REGObase-
    # Benutzer-DB, also auch keinen argon2-Login möglich -- in diesem
    # Zustand ist die Seite absichtlich UNGESCHÜTZT (kein Basic-Auth-
    # Gate), weil der echte Türsteher woanders sitzt: der Installations-
    # Job ruft `gh auth login` auf (Device-Code-Flow), und der braucht
    # eine ECHTE, manuelle Bestätigung auf github.com/login/device durch
    # den rechtmäßigen Besitzer, egal wer auf den Knopf gedrückt hat.
    # Funktioniert auf jeder frisch erzeugten LXC mit beliebiger IP,
    # ohne vorher irgendwo eine feste Callback-URL registrieren zu
    # müssen (das war der erste, verworfene Ansatz mit einer echten
    # GitHub-OAuth-App).
    db_path = auth_db_path()
    return db_path is not None and db_path.exists()


def check_auth(header: str | None) -> bool:
    db_path = auth_db_path()
    if db_path is None or not db_path.exists() or header is None or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[len("Basic "):]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except Exception:
        return False
    if not username or not password:
        return False

    try:
        # Nur lesend, eigene Verbindung pro Anfrage (kein shared state,
        # kein Konflikt mit REGObase's eigenen WAL-Schreibern).
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT password_hash, role, is_active FROM users WHERE username = ?", (username,)
        ).fetchone()
        conn.close()
    except sqlite3.Error:
        return False

    from argon2.exceptions import InvalidHash, VerifyMismatchError

    hasher, dummy_hash = _get_hasher()

    if row is None:
        # Feste Dummy-Prüfung statt sofort False -- sonst verrät die
        # Antwortzeit, ob der Benutzername existiert.
        try:
            hasher.verify(dummy_hash, password)
        except (VerifyMismatchError, InvalidHash):
            pass
        return False

    password_hash, role, is_active = row
    if not is_active or role != "ADMIN":
        return False
    try:
        ok = hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHash):
        return False
    if ok:
        global _pending_first_login, _first_login_token
        _pending_first_login = None  # einmal erfolgreich eingeloggt, Hinweis nicht mehr nötig
        _first_login_token = None
        _save_first_login_state()
    return ok


_pending_first_login: str | None = None
_first_login_token: str | None = None
_FIRST_LOGIN_RE = re.compile(r"Erstinstallations-Admin-Login:\s*(\S+)\s*/\s*(\S+)")
FIRST_LOGIN_COOKIE = "regoinstall_first_login"


def _save_first_login_state() -> None:
    # Persistiert den aktuellen Stand von _pending_first_login/
    # _first_login_token auf Platte, damit ein Dienst-Neustart (z.B. durch
    # ein Deploy dieses Skripts selbst) das Cookie im Browser des Nutzers
    # nicht wertlos macht -- siehe FIRST_LOGIN_STATE's Kommentar oben.
    data = {}
    if _first_login_token is not None:
        data["token"] = _first_login_token
    if _pending_first_login is not None:
        data["credentials"] = _pending_first_login
    if data:
        FIRST_LOGIN_STATE.parent.mkdir(parents=True, exist_ok=True)
        FIRST_LOGIN_STATE.write_text(json.dumps(data))
        FIRST_LOGIN_STATE.chmod(0o600)
    else:
        FIRST_LOGIN_STATE.unlink(missing_ok=True)


def _load_first_login_state() -> None:
    global _pending_first_login, _first_login_token
    try:
        data = json.loads(FIRST_LOGIN_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return
    _first_login_token = data.get("token")
    _pending_first_login = data.get("credentials")


def run_job_async(name: str, cmd: list[str]) -> bool:
    global _job_running, _job_name
    with _job_lock:
        if _job_running:
            return False
        _job_running = True
        _job_name = name

    log_path = LOG_DIR / f"{name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}.log"

    def _run():
        global _job_running, _pending_first_login
        try:
            with log_path.open("wb") as f:
                subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, check=False)
            if name == "install":
                # Direkter Nutzerwunsch, nach einem echten Vorfall: das
                # generierte Admin-Passwort stand bisher NUR in diesem
                # Log -- ein abgebrochener/verpasster Log-Blick (genau
                # live passiert) bedeutete dauerhaften Ausschluss, ohne
                # SSH-Zugriff auf die Box nicht wiederherstellbar (argon2-
                # Hash in der DB ist nicht umkehrbar). Jetzt zusätzlich
                # in _pending_first_login gemerkt -- render_install_page()
                # zeigt es EINMALIG groß+kopierbar an, unauthentifiziert
                # (dieselbe Begründung wie is_installed()'s eigener
                # Auth-Bypass: das ist genau das Zeitfenster, bevor der
                # Nutzer das erste Mal etwas zum Einloggen hat). Auf Platte
                # gespiegelt (siehe _save_first_login_state()) -- ein
                # zweiter echter Vorfall live gefunden: ein Dienst-Neustart
                # genau in diesem Fenster (z.B. durch ein Deploy dieses
                # Skripts selbst) hätte sonst den In-Memory-Hinweis gelöscht,
                # obwohl das Cookie im Browser des Nutzers noch gültig war,
                # und wieder zum kompletten Ausschluss geführt. Gelöscht
                # beim ersten erfolgreichen echten Login (siehe check_auth()).
                match = _FIRST_LOGIN_RE.search(log_path.read_text(errors="replace"))
                if match:
                    _pending_first_login = f"{match.group(1)} / {match.group(2)}"
                    _save_first_login_state()
        finally:
            with _job_lock:
                _job_running = False

    threading.Thread(target=_run, daemon=True).start()
    return True


def latest_log() -> str:
    logs = sorted(LOG_DIR.glob("*.log"))
    if not logs:
        return "(noch kein Lauf)"
    return logs[-1].read_text(errors="replace")[-8000:]


def make_backup(app: dict) -> str:
    db_path = Path(app["backup_db"])
    # Mikrosekunden-Auflösung, nicht nur Sekunden -- sonst kollidieren
    # zwei Backups innerhalb derselben Sekunde (z.B. das automatische
    # Sicherheits-Backup direkt vor einem Restore) auf demselben
    # Zielordner und überschreiben sich gegenseitig (echter Bug, per
    # Test gefunden: restore_backup() nahm dadurch das FALSCHE, gerade
    # überschriebene Backup als Wiederherstellungsquelle). exist_ok=False
    # lässt es hart fehlschlagen statt still zu überschreiben, falls es
    # trotzdem je kollidiert.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    app_dir = BACKUP_DIR / app["name"]
    dest_dir = app_dir / stamp
    dest_dir.mkdir(parents=True, exist_ok=False)
    # backup_extra_files enthält typischerweise /etc/regobase.env (chmod
    # 600 im Original, u.a. REGOBASE_JWT_SECRET und das Admin-Passwort im
    # Klartext) -- ohne explizites chmod hier würde die Kopie mit den
    # Standard-umask-Rechten (meist 644, für jeden lesbar) landen. Jede
    # Verzeichnisebene, die wir selbst anlegen, ebenso auf 700 setzen,
    # nicht nur die Blatt-Datei.
    app_dir.chmod(0o700)
    dest_dir.chmod(0o700)

    # sqlite3 .backup ist auch bei einer laufenden, im WAL-Modus
    # geöffneten DB sicher (im Gegensatz zu einem simplen cp).
    subprocess.run(
        ["sqlite3", str(db_path), f".backup '{dest_dir / db_path.name}'"],
        check=True,
    )
    (dest_dir / db_path.name).chmod(0o600)
    for extra in app.get("backup_extra_files", []):
        extra_path = Path(extra)
        if extra_path.exists():
            target = dest_dir / extra_path.name
            target.write_bytes(extra_path.read_bytes())
            target.chmod(0o600)

    return str(dest_dir)


def list_backups(app_name: str) -> list[str]:
    app_dir = BACKUP_DIR / app_name
    if not app_dir.exists():
        return []
    return sorted((p.name for p in app_dir.iterdir() if p.is_dir()), reverse=True)


MAX_BACKUP_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB, generous headroom over a real ~85MB regobase.db


def make_backup_archive(app: dict, backup_name: str) -> bytes:
    """Packt ein bestehendes lokales Backup als .tar.gz -- zum
    Herunterladen und auf einer anderen Maschine wieder Hochladen
    (direkter Nutzerwunsch: "kann ich die auch runterladen und auf der
    nächsten Maschine wieder hochladen?"). Gleiche Allowlist-Prüfung
    wie restore_backup() -- backup_name muss ein echter, von
    make_backup() angelegter Ordnername sein."""
    if backup_name not in list_backups(app["name"]):
        raise FileNotFoundError(f"Backup {backup_name} nicht gefunden.")
    backup_dir = BACKUP_DIR / app["name"] / backup_name

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for file in sorted(backup_dir.iterdir()):
            if file.is_file():
                tar.add(file, arcname=file.name)
    return buf.getvalue()


def import_backup_archive(app: dict, data: bytes) -> str:
    """Kehrseite von make_backup_archive() -- ein hochgeladenes .tar.gz
    wird in einen NEUEN lokalen Backup-Ordner entpackt, taucht danach
    ganz normal in list_backups() auf und kann über den bereits
    bestehenden (getesteten) restore_backup()-Weg eingespielt werden --
    keine zweite Restore-Logik nötig.

    tarfile.extractall() ohne Schutz ist ein klassischer Path-Traversal-
    Weg (Mitglieder mit "../" oder absoluten Pfaden können außerhalb des
    Zielordners landen) -- hier zusätzlich zu filter="data" (PEP 706)
    noch eine eigene, explizite Prüfung: jeder Eintrag muss ein
    einfacher Dateiname ohne "/" sein, das Archiv besteht nur aus
    flachen Dateien (siehe make_backup_archive())."""
    if len(data) > MAX_BACKUP_UPLOAD_BYTES:
        raise ValueError(f"Datei zu groß (>{MAX_BACKUP_UPLOAD_BYTES // (1024*1024)} MB).")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    app_dir = BACKUP_DIR / app["name"]
    dest_dir = app_dir / f"{stamp}-hochgeladen"
    app_dir.mkdir(parents=True, exist_ok=True)
    app_dir.chmod(0o700)
    dest_dir.mkdir(exist_ok=False)
    dest_dir.chmod(0o700)

    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                if not member.isfile() or "/" in member.name or member.name in ("..", "."):
                    raise ValueError(f"Ungültiger Eintrag im Archiv: {member.name!r}")
            tar.extractall(dest_dir, members=members, filter="data")
        for extracted in dest_dir.iterdir():
            extracted.chmod(0o600)
    except Exception:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise

    db_name = Path(app["backup_db"]).name
    if not (dest_dir / db_name).exists():
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise ValueError(f"Archiv enthält keine {db_name} -- kein gültiges Backup für {app['name']}.")

    return dest_dir.name


def restore_backup(app: dict, backup_name: str) -> None:
    """Spielt ein Backup ein -- überschreibt echte Live-Daten, deshalb
    erst ein eigenes Sicherheits-Backup des aktuellen Stands (falls man
    sich beim Auswählen vertan hat), dann Dienst stoppen, Dateien
    ersetzen, Dienst wieder starten.

    backup_name kommt roh aus der URL (POST /restore/<idx>/<backup_name>)
    -- ungeprüft wäre das ein echter Path-Traversal-Weg (z.B.
    "../../../../tmp"), über den ein Angreifer mit gültigen Zugangsdaten
    (oder ein erfolgreicher CSRF trotz Origin-Check) beliebige Dateien
    von der Platte über die echte regobase.db/regobase.env kopieren
    lassen könnte, solange sie passend benannt sind. Deshalb: nur exakt
    ein Name aus list_backups() (also ein Ordner, den make_backup()
    selbst angelegt hat) wird akzeptiert -- Allowlist statt Blocklist."""
    if backup_name not in list_backups(app["name"]):
        raise FileNotFoundError(f"Backup {backup_name} nicht gefunden.")
    backup_dir = BACKUP_DIR / app["name"] / backup_name

    make_backup(app)  # Sicherheitsnetz vor dem Überschreiben

    service = app.get("service")
    if service:
        subprocess.run(["systemctl", "stop", service], check=True)

    try:
        db_path = Path(app["backup_db"])
        backup_db = backup_dir / db_path.name
        if not backup_db.exists():
            raise FileNotFoundError(f"{backup_db} fehlt im Backup.")
        db_path.write_bytes(backup_db.read_bytes())
        # WAL/SHM der bisherigen Live-DB sind jetzt gegenüber der frisch
        # eingespielten Datei ungültig -- die eingespielte Datei ist ein
        # sauberer Einzeldatei-Snapshot (sqlite3 .backup), kein WAL-Replay
        # nötig/gewollt.
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix)
            sidecar.unlink(missing_ok=True)

        for extra in app.get("backup_extra_files", []):
            extra_path = Path(extra)
            backup_extra = backup_dir / extra_path.name
            if backup_extra.exists():
                extra_path.write_bytes(backup_extra.read_bytes())
    finally:
        if service:
            subprocess.run(["systemctl", "start", service], check=True)


PAGE_STYLE = """
body { font-family: system-ui, sans-serif; margin: 2rem; background: #f4f5f7; }
.card { background: white; border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.5rem; margin-bottom: 1rem; max-width: 500px; }
button { margin-right: 0.5rem; padding: 0.4rem 0.8rem; }
button.danger { background: #b3261e; color: white; border: none; }
"""


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>{PAGE_STYLE}</style>
</head><body>
{body}
<p style="margin-top:2rem;color:#999;font-size:0.75rem;">REGOinstall v{html.escape(VERSION)} (build {html.escape(BUILD)})</p>
</body></html>"""


# Direct request: "würde ich immer gerne in einem Log Fenster sehen, was
# gerade läuft" -- kein manuelles Neuladen mehr, ein eingebettetes <pre>
# pollt /log-content per fetch() alle 1.5s und scrollt automatisch mit.
# Bewusst reines Vanilla-JS (kein Framework), das <pre> ist beim ersten
# Laden schon server-seitig mit dem aktuellen Log gefüllt, funktioniert
# also auch ganz ohne JS (nur eben ohne Live-Aktualisierung).
def _live_log_block() -> str:
    # Direkter Nutzerwunsch: den gh-Anmeldecode nicht nur irgendwo im
    # rohen Log suchen lassen, sondern client-seitig herausfiltern, groß
    # + kopierbar anzeigen. `gh auth login`'s echtes Ausgabeformat
    # (live geprüft): "! First copy your one-time code: XXXX-XXXX".
    log_text = html.escape(latest_log())
    # Direkter Nutzerwunsch: "man weiß nie, wenn du fertig bist -- zurück
    # ist immer eingeblendet" -- vorher gab es außer dem rohen Log-Text
    # kein sichtbares Signal, ob ein Job noch läuft oder schon fertig ist.
    status_text = "⏳ Läuft…" if _job_running else "✓ Fertig"
    status_color = "#c80" if _job_running else "#2a7"
    return f"""<p id="jobStatus" style="font-weight:700;color:{status_color};">{status_text}</p>
<div id="deviceCodeBox" style="display:none;background:#eef6ff;border:1px solid #9cf;border-radius:8px;padding:0.8rem 1rem;margin-bottom:0.8rem;">
  <p style="margin:0 0 0.4rem;">GitHub-Anmeldecode:</p>
  <code id="deviceCode" style="font-size:1.4rem;font-weight:700;letter-spacing:0.05em;"></code>
  <button type="button" onclick="copyDeviceCode()">Kopieren</button>
  <span id="copyStatus" class="muted"></span>
  <p style="margin:0.5rem 0 0;"><a href="https://github.com/login/device" target="_blank">github.com/login/device öffnen</a></p>
</div>
<pre id="log" style="background:#111;color:#ddd;padding:0.8rem;max-height:400px;overflow:auto;border-radius:8px;">{log_text}</pre>
<script>
function copyDeviceCode() {{
  const code = document.getElementById('deviceCode').textContent;
  navigator.clipboard.writeText(code).then(() => {{
    const s = document.getElementById('copyStatus');
    s.textContent = 'kopiert!';
    setTimeout(() => {{ s.textContent = ''; }}, 2000);
  }});
}}
function extractDeviceCode(text) {{
  const m = text.match(/one-time code:\\s*([A-Z0-9-]+)/);
  return m ? m[1] : null;
}}
async function pollLog() {{
  try {{
    const res = await fetch('/log-content');
    if (res.ok) {{
      const text = await res.text();
      const el = document.getElementById('log');
      const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
      el.textContent = text;
      if (atBottom) el.scrollTop = el.scrollHeight;
      const code = extractDeviceCode(text);
      if (code) {{
        document.getElementById('deviceCode').textContent = code;
        document.getElementById('deviceCodeBox').style.display = 'block';
      }}
      const running = res.headers.get('X-Job-Running') === 'true';
      const statusEl = document.getElementById('jobStatus');
      statusEl.textContent = running ? '⏳ Läuft…' : '✓ Fertig';
      statusEl.style.color = running ? '#c80' : '#2a7';
    }}
  }} catch (e) {{}}
  setTimeout(pollLog, 1500);
}}
pollLog();
</script>"""


def render_install_page() -> str:
    if _job_running:
        job_label = {"connect-github": "GitHub-Anmeldung", "install": "Installation"}.get(_job_name, _job_name or "Job")
        body = f"""<h1>{html.escape(job_label)} läuft…</h1>
<p>Bei der GitHub-Anmeldung erscheint hier ein Anmeldecode -- den auf
<a href="https://github.com/login/device" target="_blank">github.com/login/device</a>
bestätigen, dann läuft es automatisch weiter.</p>
{_live_log_block()}"""
        return _page("REGOinstall", body)

    gh_ok, gh_login = gh_auth_status()
    if not gh_ok:
        body = """<h1>REGObase installieren</h1>
<p>Erster Schritt: mit GitHub anmelden (REGObase/REGOcore/regoeldat-core
sind private Repos). Ein Klick zeigt einen Code + einen Link zu
<a href="https://github.com/login/device" target="_blank">github.com/login/device</a> --
dort bestätigen.</p>
<form method="post" action="/connect-github">
  <button type="submit">Mit GitHub anmelden</button>
</form>"""
    else:
        body = f"""<h1>REGObase installieren</h1>
<p>✓ Mit GitHub angemeldet als <strong>{html.escape(gh_login or '')}</strong>.</p>
<p>Ein Klick startet die komplette Installation (Docker, Matterbridge,
REGObase selbst) im Hintergrund.</p>
<form method="post" action="/install">
  <button type="submit">Installation starten</button>
</form>"""
    return _page("REGOinstall", body)


def render_first_login_page() -> str:
    # Direkter Nutzerwunsch nach einem echten Vorfall: das generierte
    # Admin-Passwort stand bisher nur im Installations-Log -- ein
    # verpasster/abgebrochener Blick dorthin bedeutete dauerhaften
    # Ausschluss (argon2-Hash in der DB ist nicht umkehrbar, nur per
    # SSH+direktem DB-Zugriff zu umgehen). Diese Seite ist absichtlich
    # ungeschützt erreichbar, aber nur solange das Passwort noch nie
    # erfolgreich benutzt wurde (siehe _require_auth()/check_auth()).
    creds = html.escape(_pending_first_login or "")
    body = f"""<h1>Installation abgeschlossen</h1>
<div class="card" style="background:#eef6ff;border-color:#9cf;">
  <p>Dein Admin-Login (nur dieses eine Mal hier sichtbar -- notieren!):</p>
  <code id="creds" style="font-size:1.3rem;font-weight:700;">{creds}</code>
  <button type="button" onclick="copyCreds()">Kopieren</button>
  <span id="copyStatus" class="muted"></span>
</div>
<p><a href="/">Weiter zum Login</a></p>
<script>
function copyCreds() {{
  navigator.clipboard.writeText(document.getElementById('creds').textContent).then(() => {{
    const s = document.getElementById('copyStatus');
    s.textContent = 'kopiert!';
    setTimeout(() => {{ s.textContent = ''; }}, 2000);
  }});
}}
</script>"""
    return _page("Installation abgeschlossen", body)


def render_index() -> str:
    apps = load_apps()
    running = "Ja" if _job_running else "Nein"
    rows = []
    for i, app in enumerate(apps):
        name = html.escape(app["name"])
        url = html.escape(app["url"])
        backups = list_backups(app["name"])
        if backups:
            backup_list = "".join(
                f"<li>{html.escape(b)} -- "
                f"<a href='/restore/{i}'>wiederherstellen</a> -- "
                f"<a href='/backup/{i}/{html.escape(b)}/download'>herunterladen</a></li>"
                for b in backups[:5]
            )
        else:
            backup_list = "<li>(noch keins)</li>"
        rows.append(f"""
        <div class="card">
          <h2>{name}</h2>
          <p><a href="{url}" target="_blank">{url} öffnen</a></p>
          <form method="post" action="/update/{i}" style="display:inline">
            <button type="submit">Update starten</button>
          </form>
          <form method="post" action="/backup/{i}" style="display:inline">
            <button type="submit">Backup jetzt erstellen</button>
          </form>
          <p>Letzte Backups: (<a href="/restore/{i}">Backup hochladen</a>)</p>
          <ul>{backup_list}</ul>
        </div>
        """)
    gh_ok, gh_login = gh_auth_status()
    github_status = (
        f"✓ Verbunden als <strong>{html.escape(gh_login or '')}</strong>" if gh_ok else "✗ Nicht verbunden"
    )
    github_card = f"""
    <div class="card">
      <h2>GitHub</h2>
      <p>{github_status}</p>
      <form method="post" action="/connect-github">
        <button type="submit">{"Neu verbinden" if gh_ok else "Mit GitHub verbinden"}</button>
      </form>
    </div>"""

    body = f"""<h1>REGOinstall</h1>
<p>Läuft gerade ein Job? {running} -- <a href="/log">Log ansehen</a></p>
{github_card}
{''.join(rows)}"""
    return _page("REGOinstall", body)


def render_restore_confirm(idx: int, app: dict) -> str:
    backups = list_backups(app["name"])
    upload_block = f"""
    <div class="card">
      <p>Backup von einer anderen Maschine hochladen (.tar.gz von "herunterladen"):</p>
      <input type="file" id="backupFile" accept=".tar.gz,.gz">
      <button type="button" onclick="uploadBackup()">Hochladen</button>
      <p id="uploadStatus" class="muted"></p>
    </div>
    <script>
    async function uploadBackup() {{
      const input = document.getElementById('backupFile');
      const status = document.getElementById('uploadStatus');
      if (!input.files.length) {{ status.textContent = 'Bitte zuerst eine Datei auswählen.'; return; }}
      status.textContent = 'Lade hoch…';
      try {{
        const res = await fetch('/backup/{idx}/upload', {{ method: 'POST', body: input.files[0] }});
        if (res.redirected) {{ window.location = res.url; return; }}
        if (!res.ok) {{ status.textContent = 'Fehler: ' + await res.text(); return; }}
        window.location.reload();
      }} catch (e) {{ status.textContent = 'Fehler: ' + e; }}
    }}
    </script>"""
    if not backups:
        body = f"""<h1>Restore -- {html.escape(app['name'])}</h1>
<p>Noch keine lokalen Backups.</p>
{upload_block}
<p><a href="/">Zurück</a></p>"""
        return _page(f"Restore -- {app['name']}", body)
    rows = "".join(f"""
      <div class="card">
        <p>{html.escape(b)} -- <a href='/backup/{idx}/{html.escape(b)}/download'>herunterladen</a></p>
        <form method="post" action="/restore/{idx}/{html.escape(b)}">
          <button type="submit" class="danger">Dieses Backup einspielen (überschreibt Live-Daten)</button>
        </form>
      </div>
    """ for b in backups)
    body = f"""<h1>Restore -- {html.escape(app['name'])}</h1>
<p>Vor dem Einspielen wird automatisch ein Sicherheits-Backup des aktuellen Stands erstellt.
Der Dienst ({html.escape(app.get('service', ''))}) wird kurz gestoppt und neu gestartet.</p>
{rows}
{upload_block}
<p><a href="/">Zurück</a></p>"""
    return _page(f"Restore -- {app['name']}", body)


class Handler(BaseHTTPRequestHandler):
    def _unauthorized(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="REGOinstall"')
        self.end_headers()

    def _require_auth(self, path: str) -> bool:
        if not is_installed():
            # Vor der ersten Installation gibt es noch keine REGObase-
            # Benutzer-DB gegen die geprüft werden könnte -- der echte
            # Türsteher ist hier `gh auth login`'s Device-Code-Flow
            # innerhalb des Installations-Jobs selbst (siehe is_installed()'s
            # Docstring-Kommentar).
            return True
        if check_auth(self.headers.get("Authorization")):
            # Echte Anmeldung geht immer vor -- auch auf "/" und
            # "/first-login" selbst, sonst würde ein Login-Versuch über "/"
            # nie bei check_auth() ankommen und _pending_first_login (das
            # dort bei Erfolg gelöscht wird) bliebe für immer gesetzt.
            return True
        if (
            path in ("/first-login", "/")
            and _pending_first_login is not None
            and _first_login_token is not None
            and secrets.compare_digest(self._get_cookie(FIRST_LOGIN_COOKIE) or "", _first_login_token)
        ):
            # Direkter Nutzerwunsch, nach einem echten Vorfall (Passwort
            # nur im Log, Log verpasst, dauerhaft ausgesperrt): diese beiden
            # Routen bleiben ohne gültige Anmeldung erreichbar, aber NUR für
            # den Browser, der die Installation selbst gestartet hat (das
            # eigene, bei "POST /install" gesetzte Cookie muss passen) --
            # NICHT für jeden im Netz. Security-Review hat zurecht
            # angemerkt, dass eine reine "_pending_first_login is not None"-
            # Prüfung das generierte Passwort für JEDEN mit Netzzugriff auf
            # Port 80 unauthentifiziert abrufbar gemacht hätte (schlimmer
            # als vorher, wo dafür Shell-/SSH-Zugriff nötig war). "/" muss
            # mit rein, sonst bekäme der berechtigte Nutzer beim ersten
            # Aufruf nach der Installation sofort einen Basic-Auth-Dialog
            # (kein gecachtes Passwort!) statt der Weiterleitung zu
            # /first-login -- genau der Fall, den diese Seite lösen soll.
            return True
        self._unauthorized()
        return False

    def _get_cookie(self, name: str) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return None

    def _send_html(self, body: str, status: int = 200):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _redirect(self, location: str, extra_headers: dict[str, str] | None = None):
        self.send_response(303)
        self.send_header("Location", location)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._require_auth(path):
            return
        if path == "/first-login":
            if _pending_first_login is None:
                self._redirect("/")
                return
            self._send_html(render_first_login_page())
        elif path == "/":
            if is_installed() and _pending_first_login is not None:
                self._redirect("/first-login")
                return
            self._send_html(render_index() if is_installed() else render_install_page())
        elif path == "/log":
            self._send_html(_page("Log", f"{_live_log_block()}<p><a href='/'>Zurück</a></p>"))
        elif path == "/log-content":
            # Reines Text-Endpunkt für das JS-Polling in _live_log_block()
            # -- kein HTML drumherum, damit fetch() den Log-Inhalt direkt
            # als Text bekommt.
            encoded = latest_log().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            # Eigener Header statt eigenem JSON-Endpunkt fürs Job-Ende --
            # das Polling-JS in _live_log_block() liest ihn bei jedem
            # ohnehin schon laufenden Log-Poll mit, kein zweiter Request
            # nötig. Direkter Nutzerwunsch: "man weiß nie, wenn du fertig
            # bist -- zurück ist immer eingeblendet" -- vorher gab es gar
            # kein sichtbares Fertig-Signal, nur den Log-Text selbst.
            self.send_header("X-Job-Running", "true" if _job_running else "false")
            self.end_headers()
            self.wfile.write(encoded)
        elif path.startswith("/restore/"):
            idx = self._parse_index(path, "/restore/")
            if idx is None:
                self.send_response(404)
                self.end_headers()
                return
            apps = load_apps()
            self._send_html(render_restore_confirm(idx, apps[idx]))
        elif path.startswith("/backup/") and path.endswith("/download"):
            # /backup/<idx>/<backup_name>/download -- Backup als .tar.gz
            # herunterladen, um es auf einer anderen Maschine wieder
            # hochzuladen (direkter Nutzerwunsch).
            rest = path[len("/backup/"):-len("/download")]
            idx_str, _, backup_name = rest.partition("/")
            idx = self._parse_index(f"/backup/{idx_str}", "/backup/") if idx_str.isdigit() else None
            if idx is None or not backup_name:
                self.send_response(404)
                self.end_headers()
                return
            apps = load_apps()
            try:
                archive = make_backup_archive(apps[idx], backup_name)
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                return
            filename = f"{apps[idx]['name']}-{backup_name}.tar.gz"
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(archive)))
            self.end_headers()
            self.wfile.write(archive)
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        # BaseHTTPRequestHandler only implements do_GET/do_POST by
        # default -- without this, any HEAD request (health checks,
        # `curl -I`) gets a bare 501 instead of the real status/headers.
        path = urlparse(self.path).path
        if not self._require_auth(path):
            return
        if path in ("/", "/log"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def _same_origin(self) -> bool:
        # Basic-Auth-Zugangsdaten werden vom Browser für den ganzen
        # Origin gecacht und automatisch bei JEDER Anfrage mitgeschickt,
        # auch von einer fremden Seite ausgelöste (klassisches CSRF-
        # Muster: eine bösartige Seite lässt ein Formular auf /update/0
        # automatisch abschicken, der Browser liefert die gecachten
        # Credentials gleich mit). Origin/Referer müssen zum eigenen Host
        # passen, sonst wird die schreibende Anfrage abgelehnt.
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        referer = self.headers.get("Referer")
        candidate = origin or referer
        if not candidate:
            # Ein echter Browser, der das eigene Formular abschickt,
            # setzt beides -- kein Origin/Referer ist eher ein
            # Kommandozeilen-Tool (curl) als ein Angriff über den
            # Browser, aber sicherheitshalber trotzdem ablehnen.
            return False
        return urlparse(candidate).netloc == host

    def _parse_index(self, path: str, prefix: str) -> int | None:
        rest = path[len(prefix):]
        try:
            idx = int(rest.split("/", 1)[0])
        except ValueError:
            return None
        apps = load_apps()
        return idx if 0 <= idx < len(apps) else None

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._require_auth(path):
            return
        if not self._same_origin():
            self.send_response(403)
            self.end_headers()
            return

        apps = load_apps()

        if path == "/connect-github":
            # Auch nach der Installation erreichbar (direkter
            # Nutzerwunsch: "müsste man immer noch die Möglichkeit haben
            # sich ins Git einzuloggen") -- z.B. um eine kaputte
            # Credential-Helper-Verdrahtung zu reparieren (echter Fall:
            # `gh auth status` meldete "angemeldet", aber rohes
            # "git fetch" scheiterte trotzdem, siehe ensure_gh_auth()'s
            # gh-auth-setup-git-Fix). Kein früher Ausstieg mehr, auch
            # wenn gh_auth_status() schon "verbunden" meldet -- der
            # Knopf soll ein "neu verbinden" bleiben können.
            connect_cmd = load_config().get("connect_github_cmd")
            if not connect_cmd:
                self._send_html(
                    _page("Install", "<p>connect_github_cmd fehlt in /etc/regoinstall/config.json.</p>"), status=500
                )
                return
            started = run_job_async("connect-github", connect_cmd)
            if not started:
                self._send_html("<p>Läuft schon. <a href='/log'>Log ansehen</a></p>")
                return
            self._redirect("/log")
            return

        if path == "/install":
            if is_installed():
                self._redirect("/")
                return
            gh_ok, _ = gh_auth_status()
            if not gh_ok:
                # Direkter Zugriff auf /install ohne vorherige GitHub-
                # Anmeldung (z.B. Lesezeichen, doppelter Tab) -- zurück
                # zur Startseite statt einer Installation, die sowieso
                # gleich am fehlenden gh-Login hängen bliebe.
                self._redirect("/")
                return
            install_cmd = load_config().get("install_cmd")
            if not install_cmd:
                self._send_html(
                    _page("Install", "<p>install_cmd fehlt in /etc/regoinstall/config.json.</p>"), status=500
                )
                return
            started = run_job_async("install", install_cmd)
            if not started:
                self._send_html("<p>Installation läuft bereits. <a href='/log'>Log ansehen</a></p>")
                return
            # Token für den späteren /first-login-Zugriff: nur DIESER
            # Browser (der die Installation gerade ausgelöst hat) bekommt
            # das Cookie, siehe _require_auth()'s Kommentar dazu. Sofort
            # gespeichert (nicht erst am Job-Ende), damit ein Neustart des
            # Dienstes auch WÄHREND einer laufenden Installation das
            # Cookie nicht entwertet.
            global _first_login_token
            _first_login_token = secrets.token_urlsafe(32)
            _save_first_login_state()
            self._redirect(
                "/log",
                extra_headers={
                    "Set-Cookie": f"{FIRST_LOGIN_COOKIE}={_first_login_token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=3600"
                },
            )
            return

        if path.startswith("/update/"):
            idx = self._parse_index(path, "/update/")
            if idx is None:
                self.send_response(400)
                self.end_headers()
                return
            started = run_job_async(f"update-{apps[idx]['name']}", apps[idx]["update_cmd"])
            if not started:
                self._send_html("<p>Es läuft schon ein Job. <a href='/'>Zurück</a></p>")
                return
            self._redirect("/log")
            return

        if path.startswith("/backup/") and path.endswith("/upload"):
            # /backup/<idx>/upload -- Backup-Archiv von einer anderen
            # Maschine wieder hochladen (direkter Nutzerwunsch). Kein
            # multipart/form-data-Parsing nötig: das <input type=file>
            # wird per JS als roher Request-Body gePOSTet (siehe
            # render_restore_confirm()), hier einfach Content-Length
            # Bytes direkt vom Socket lesen.
            idx_str = path[len("/backup/"):-len("/upload")]
            idx = self._parse_index(f"/backup/{idx_str}", "/backup/") if idx_str.isdigit() else None
            if idx is None:
                self.send_response(400)
                self.end_headers()
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BACKUP_UPLOAD_BYTES:
                self.send_response(400)
                self.end_headers()
                return
            data = self.rfile.read(length)
            try:
                import_backup_archive(apps[idx], data)
            except Exception as exc:
                self._send_html(f"<p>Hochladen fehlgeschlagen: {exc}</p><p><a href='/'>Zurück</a></p>", status=400)
                return
            self._redirect(f"/restore/{idx}")
            return

        if path.startswith("/backup/"):
            idx = self._parse_index(path, "/backup/")
            if idx is None:
                self.send_response(400)
                self.end_headers()
                return
            try:
                make_backup(apps[idx])
            except Exception as exc:
                self._send_html(f"<p>Backup fehlgeschlagen: {exc}</p><p><a href='/'>Zurück</a></p>", status=500)
                return
            self._redirect("/")
            return

        if path.startswith("/restore/"):
            rest = path[len("/restore/"):]
            idx_str, _, backup_name = rest.partition("/")
            idx = self._parse_index(f"/restore/{idx_str}", "/restore/") if idx_str.isdigit() else None
            if idx is None or not backup_name:
                self.send_response(400)
                self.end_headers()
                return
            try:
                restore_backup(apps[idx], backup_name)
            except Exception as exc:
                self._send_html(f"<p>Restore fehlgeschlagen: {exc}</p><p><a href='/'>Zurück</a></p>", status=500)
                return
            self._redirect("/")
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    _load_first_login_state()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"REGOinstall v{VERSION} (build {BUILD}) läuft auf Port {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
