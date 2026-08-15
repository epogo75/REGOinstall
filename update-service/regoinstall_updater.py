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
"""

import base64
import html
import json
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError

APPS_CONFIG = Path("/etc/regoinstall/apps.json")
CONFIG = Path("/etc/regoinstall/config.json")
LOG_DIR = Path("/var/log/regoinstall")
BACKUP_DIR = Path("/var/backups/regoinstall")
PORT = 80

LOG_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.chmod(0o700)  # enthält irgendwann Kopien von /etc/regobase.env

_hasher = PasswordHasher()
# Fixed dummy hash to verify against when a username isn't found, so a
# "no such user" response takes the same ~100ms as a real wrong-password
# check instead of returning near-instantly -- otherwise response timing
# alone would let an attacker enumerate valid usernames.
_DUMMY_HASH = _hasher.hash("not-a-real-password")
_job_lock = threading.Lock()
_job_running = False


def load_apps() -> list[dict]:
    if not APPS_CONFIG.exists():
        return []
    return json.loads(APPS_CONFIG.read_text())


def auth_db_path() -> Path | None:
    if not CONFIG.exists():
        return None
    value = json.loads(CONFIG.read_text()).get("auth_db")
    return Path(value) if value else None


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

    if row is None:
        # Feste Dummy-Prüfung statt sofort False -- sonst verrät die
        # Antwortzeit, ob der Benutzername existiert.
        try:
            _hasher.verify(_DUMMY_HASH, password)
        except (VerifyMismatchError, InvalidHash):
            pass
        return False

    password_hash, role, is_active = row
    if not is_active or role != "ADMIN":
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHash):
        return False


def run_job_async(name: str, cmd: list[str]) -> bool:
    global _job_running
    with _job_lock:
        if _job_running:
            return False
        _job_running = True

    log_path = LOG_DIR / f"{name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}.log"

    def _run():
        global _job_running
        try:
            with log_path.open("wb") as f:
                subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, check=False)
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


def restore_backup(app: dict, backup_name: str) -> None:
    """Spielt ein Backup ein -- überschreibt echte Live-Daten, deshalb
    erst ein eigenes Sicherheits-Backup des aktuellen Stands (falls man
    sich beim Auswählen vertan hat), dann Dienst stoppen, Dateien
    ersetzen, Dienst wieder starten."""
    backup_dir = BACKUP_DIR / app["name"] / backup_name
    if not backup_dir.is_dir():
        raise FileNotFoundError(f"Backup {backup_name} nicht gefunden.")

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
</body></html>"""


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
                f"<li>{html.escape(b)} -- <a href='/restore/{i}'>wiederherstellen</a></li>" for b in backups[:5]
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
          <p>Letzte Backups:</p>
          <ul>{backup_list}</ul>
        </div>
        """)
    body = f"""<h1>REGOinstall</h1>
<p>Läuft gerade ein Job? {running} -- <a href="/log">Log ansehen</a></p>
{''.join(rows)}"""
    return _page("REGOinstall", body)


def render_restore_confirm(idx: int, app: dict) -> str | None:
    backups = list_backups(app["name"])
    if not backups:
        return None
    rows = "".join(f"""
      <div class="card">
        <p>{html.escape(b)}</p>
        <form method="post" action="/restore/{idx}/{html.escape(b)}">
          <button type="submit" class="danger">Dieses Backup einspielen (überschreibt Live-Daten)</button>
        </form>
      </div>
    """ for b in backups)
    body = f"""<h1>Restore -- {html.escape(app['name'])}</h1>
<p>Vor dem Einspielen wird automatisch ein Sicherheits-Backup des aktuellen Stands erstellt.
Der Dienst ({html.escape(app.get('service', ''))}) wird kurz gestoppt und neu gestartet.</p>
{rows}
<p><a href="/">Zurück</a></p>"""
    return _page(f"Restore -- {app['name']}", body)


class Handler(BaseHTTPRequestHandler):
    def _unauthorized(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="REGOinstall"')
        self.end_headers()

    def _require_auth(self) -> bool:
        if check_auth(self.headers.get("Authorization")):
            return True
        self._unauthorized()
        return False

    def _send_html(self, body: str, status: int = 200):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(render_index())
        elif path == "/log":
            self._send_html(_page("Log", f"<pre>{html.escape(latest_log())}</pre><p><a href='/'>Zurück</a></p>"))
        elif path.startswith("/restore/"):
            idx = self._parse_index(path, "/restore/")
            if idx is None:
                self.send_response(404)
                self.end_headers()
                return
            apps = load_apps()
            page = render_restore_confirm(idx, apps[idx])
            if page is None:
                self._send_html(_page("Restore", "<p>Keine Backups vorhanden.</p><p><a href='/'>Zurück</a></p>"))
                return
            self._send_html(page)
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        # BaseHTTPRequestHandler only implements do_GET/do_POST by
        # default -- without this, any HEAD request (health checks,
        # `curl -I`) gets a bare 501 instead of the real status/headers.
        if not self._require_auth():
            return
        path = urlparse(self.path).path
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
        if not self._require_auth():
            return
        if not self._same_origin():
            self.send_response(403)
            self.end_headers()
            return

        path = urlparse(self.path).path
        apps = load_apps()

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
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"REGOinstall-Oberfläche läuft auf Port {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
