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

Auth: HTTP Basic gegen REGOBASE_ADMIN_PASSWORD aus /etc/regobase.env
(bei jeder Anfrage frisch gelesen, damit ein geändertes Admin-Passwort
sofort greift, kein eigenes zweites Passwort zu pflegen).
"""

import base64
import hmac
import json
import subprocess
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

APPS_CONFIG = Path("/etc/regoinstall/apps.json")
REGOBASE_ENV = Path("/etc/regobase.env")
LOG_DIR = Path("/var/log/regoinstall")
BACKUP_DIR = Path("/var/backups/regoinstall")
PORT = 80

LOG_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

_job_lock = threading.Lock()
_job_running = False


def load_apps() -> list[dict]:
    if not APPS_CONFIG.exists():
        return []
    return json.loads(APPS_CONFIG.read_text())


def admin_password() -> str | None:
    if not REGOBASE_ENV.exists():
        return None
    for line in REGOBASE_ENV.read_text().splitlines():
        if line.startswith("REGOBASE_ADMIN_PASSWORD="):
            return line.split("=", 1)[1].strip()
    return None


def check_auth(header: str | None) -> bool:
    expected = admin_password()
    if expected is None or header is None or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[len("Basic "):]).decode("utf-8")
        _, _, password = decoded.partition(":")
    except Exception:
        return False
    return hmac.compare_digest(password, expected)


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
    dest_dir = BACKUP_DIR / app["name"] / stamp
    dest_dir.mkdir(parents=True, exist_ok=False)

    # sqlite3 .backup ist auch bei einer laufenden, im WAL-Modus
    # geöffneten DB sicher (im Gegensatz zu einem simplen cp).
    subprocess.run(
        ["sqlite3", str(db_path), f".backup '{dest_dir / db_path.name}'"],
        check=True,
    )
    for extra in app.get("backup_extra_files", []):
        extra_path = Path(extra)
        if extra_path.exists():
            (dest_dir / extra_path.name).write_bytes(extra_path.read_bytes())

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


def render_index() -> str:
    apps = load_apps()
    running = "Ja" if _job_running else "Nein"
    rows = []
    for i, app in enumerate(apps):
        backups = list_backups(app["name"])
        backup_list = "".join(f"<li>{b}</li>" for b in backups[:5]) or "<li>(noch keins)</li>"
        rows.append(f"""
        <div class="card">
          <h2>{app['name']}</h2>
          <p><a href="{app['url']}" target="_blank">{app['url']} öffnen</a></p>
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
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>REGOinstall</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #f4f5f7; }}
.card {{ background: white; border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.5rem; margin-bottom: 1rem; max-width: 500px; }}
button {{ margin-right: 0.5rem; padding: 0.4rem 0.8rem; }}
</style>
</head><body>
<h1>REGOinstall</h1>
<p>Läuft gerade ein Job? {running} -- <a href="/log">Log ansehen</a></p>
{''.join(rows)}
</body></html>"""


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
            self._send_html(f"<pre>{latest_log()}</pre><p><a href='/'>Zurück</a></p>")
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

    def do_POST(self):
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        apps = load_apps()

        if path.startswith("/update/"):
            idx = int(path.rsplit("/", 1)[-1])
            if 0 <= idx < len(apps):
                started = run_job_async(f"update-{apps[idx]['name']}", apps[idx]["update_cmd"])
                if not started:
                    self._send_html("<p>Es läuft schon ein Job. <a href='/'>Zurück</a></p>")
                    return
            self._redirect("/log")
            return

        if path.startswith("/backup/"):
            idx = int(path.rsplit("/", 1)[-1])
            if 0 <= idx < len(apps):
                try:
                    make_backup(apps[idx])
                except Exception as exc:
                    self._send_html(f"<p>Backup fehlgeschlagen: {exc}</p><p><a href='/'>Zurück</a></p>", status=500)
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
