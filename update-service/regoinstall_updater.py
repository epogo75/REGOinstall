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
import json
import sqlite3
import subprocess
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
        return hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHash):
        return False


def run_job_async(name: str, cmd: list[str]) -> bool:
    global _job_running, _job_name
    with _job_lock:
        if _job_running:
            return False
        _job_running = True
        _job_name = name

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
    log_text = html.escape(latest_log())
    return f"""<pre id="log" style="background:#111;color:#ddd;padding:0.8rem;max-height:400px;overflow:auto;border-radius:8px;">{log_text}</pre>
<script>
async function pollLog() {{
  try {{
    const res = await fetch('/log-content');
    if (res.ok) {{
      const text = await res.text();
      const el = document.getElementById('log');
      const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
      el.textContent = text;
      if (atBottom) el.scrollTop = el.scrollHeight;
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
<p>Bei der GitHub-Anmeldung erscheint hier ein Code + ein Link zu
<a href="https://github.com/login/device" target="_blank">github.com/login/device</a>
-- dort bestätigen, dann läuft es automatisch weiter.</p>
{_live_log_block()}"""
        return _page("REGOinstall", body)

    gh_ok, gh_login = gh_auth_status()
    if not gh_ok:
        body = """<h1>REGObase installieren</h1>
<p>Erster Schritt: mit GitHub anmelden (REGObase/REGOcore/regoeldat-core
sind private Repos). Ein Klick zeigt einen Code + Link -- der muss auf
github.com/login/device bestätigt werden.</p>
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
        if not is_installed():
            # Vor der ersten Installation gibt es noch keine REGObase-
            # Benutzer-DB gegen die geprüft werden könnte -- der echte
            # Türsteher ist hier `gh auth login`'s Device-Code-Flow
            # innerhalb des Installations-Jobs selbst (siehe is_installed()'s
            # Docstring-Kommentar).
            return True
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
            self.end_headers()
            self.wfile.write(encoded)
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

        if path == "/connect-github":
            if is_installed():
                self._redirect("/")
                return
            gh_ok, _ = gh_auth_status()
            if gh_ok:
                self._redirect("/")
                return
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
            self._redirect("/log")
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
    print(f"REGOinstall v{VERSION} (build {BUILD}) läuft auf Port {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
