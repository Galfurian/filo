"""filo ui: a viewer for a pool of agents, in a browser.

Standard library only. It reads what filo keeps (the roster that `filo who` shows and the log of each scope) and
shows it: it writes nothing and sends nothing. It runs in the foreground until Ctrl-C and listens on 127.0.0.1
only; every request needs the token in the address that it prints, and a request that names another host is
refused (a page on the web must not be able to read your agents' messages through your browser).

`run(core, ...)` takes the `filo` module itself, so that the viewer and the filo that started it agree.
"""

from __future__ import annotations

import http.server
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

HERE = Path(__file__).resolve().parent
POLL = 0.4              # how often the log of a scope is looked at for new entries
ROSTER_EVERY = 3.0      # how often the roster is looked at again, and the longest that it is not
KEEPALIVE = 15.0        # a comment on a quiet stream, so that nothing in between closes it
HISTORY = 300           # entries of the log that a page starts with
READ_LIMIT = 4_000_000  # the most that one look at the log reads


class Viewer:
    """What the pages ask for, from the filo that started this."""

    def __init__(self, core: Any, token: str) -> None:
        self.core = core
        self.token = token
        self.port = 0
        self._roster_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def scopes(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for scope in self.core.known_scopes():
            roster = self.roster(scope)
            found.append({"scope": self.core.display_scope(scope), "participants": len(roster["participants"]),
                          "registered": len(roster["registered"])})
        return found

    def find(self, shown: str) -> str | None:
        """The scope that is shown as ``shown``, among the ones filo knows: nothing else can be asked for."""
        for scope in self.core.known_scopes():
            if self.core.display_scope(scope) == shown:
                return str(scope)
        return None

    def roster(self, scope: str) -> dict[str, Any]:
        with self._lock:
            cached = self._roster_cache.get(scope)
            if cached is not None and time.monotonic() - cached[0] < 2.0:
                return cached[1]
        participants = self.core.participant_rows(scope)
        registered = self.core.live_registrations(scope)
        waits = self.core.registration_waits(registered)
        value = {"scope": self.core.display_scope(scope),
                 "participants": [asdict(row) for row in participants],
                 "registered": self.core.registered_rows(registered, waits)}
        with self._lock:
            self._roster_cache[scope] = (time.monotonic(), value)
        return value

    def log_path(self, scope: str) -> Path:
        return Path(self.core.scope_directory(scope)) / "events.jsonl"

    def read_since(self, scope: str, offset: int) -> tuple[list[dict[str, Any]], int]:
        """The entries of the log after ``offset`` (a byte position) and the position after them. The file is
        trimmed from time to time, and then it starts again from its beginning."""
        path = self.log_path(scope)
        try:
            size = path.stat().st_size
        except OSError:
            return [], 0
        if offset > size:
            offset = 0
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(min(size - offset, READ_LIMIT))
        except OSError:
            return [], offset
        complete = data.rfind(b"\n") + 1
        entries: list[dict[str, Any]] = []
        for line in data[:complete].splitlines():
            try:
                entry: Any = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(cast(dict[str, Any], entry))
        return entries, offset + complete

    def history(self, scope: str) -> tuple[list[dict[str, Any]], int]:
        entries, offset = self.read_since(scope, max(0, self._size(scope) - READ_LIMIT))
        return entries[-HISTORY:], offset

    def _size(self, scope: str) -> int:
        try:
            return self.log_path(scope).stat().st_size
        except OSError:
            return 0


def make_handler(viewer: Viewer) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "filo-ui"
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        # -- who may ask --------------------------------------------------------------------------------------

        def allowed(self) -> tuple[bool, dict[str, list[str]]]:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            host = self.headers.get("Host", "")
            if host not in (f"127.0.0.1:{viewer.port}", f"localhost:{viewer.port}"):
                self.reply(403, "text/plain; charset=utf-8", b"wrong host")
                return False, query
            supplied = (query.get("token") or [""])[0]
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "filo_ui":
                    supplied = supplied or value
            if not secrets.compare_digest(supplied.encode(), viewer.token.encode()):
                self.reply(403, "text/plain; charset=utf-8", b"open the address that filo ui printed")
                return False, query
            return True, query

        def reply(self, status: int, kind: str, body: bytes, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def json(self, value: Any) -> None:
            self.reply(200, "application/json; charset=utf-8", json.dumps(value, ensure_ascii=False).encode())

        # -- the routes ---------------------------------------------------------------------------------------

        def do_GET(self) -> None:
            ok, query = self.allowed()
            if not ok:
                return
            path = urllib.parse.urlsplit(self.path).path
            if path == "/":
                self.page()
            elif path == "/api/scopes":
                self.json({"scopes": viewer.scopes()})
            elif path in ("/api/state", "/api/stream"):
                scope = viewer.find((query.get("scope") or [""])[0])
                if scope is None:
                    self.reply(404, "text/plain; charset=utf-8", b"no such scope")
                elif path == "/api/state":
                    entries, offset = viewer.history(scope)
                    self.json({**viewer.roster(scope), "events": entries, "offset": offset})
                else:
                    self.stream(scope, int((query.get("offset") or ["0"])[0] or 0))
            else:
                self.reply(404, "text/plain; charset=utf-8", b"not found")

        def page(self) -> None:
            nonce = secrets.token_urlsafe(12)
            html = (HERE / "index.html").read_text(encoding="utf-8").replace("__NONCE__", nonce).encode()
            policy = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                      f"connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'")
            self.reply(200, "text/html; charset=utf-8", html, {
                "Content-Security-Policy": policy,
                "Set-Cookie": f"filo_ui={viewer.token}; HttpOnly; SameSite=Strict; Path=/"})

        def stream(self, scope: str, offset: int) -> None:
            """Server-sent events: new entries of the log as they are written, and the roster when it changes."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            last_roster = ""
            next_roster = 0.0
            quiet_since = time.monotonic()

            def send(event: str, data: Any) -> None:
                self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()

            try:
                while True:
                    entries, offset = viewer.read_since(scope, offset)
                    if entries:
                        send("events", {"events": entries, "offset": offset})
                        quiet_since = time.monotonic()
                    if time.monotonic() >= next_roster:
                        roster = json.dumps(viewer.roster(scope), ensure_ascii=False)
                        if roster != last_roster:
                            send("roster", json.loads(roster))
                            last_roster = roster
                            quiet_since = time.monotonic()
                        next_roster = time.monotonic() + ROSTER_EVERY
                    if time.monotonic() - quiet_since > KEEPALIVE:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        quiet_since = time.monotonic()
                    time.sleep(POLL)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return Handler


def run(core: Any, port: int = 0, open_browser: bool = False) -> int:
    viewer = Viewer(core, secrets.token_urlsafe(18))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(viewer))
    server.daemon_threads = True
    viewer.port = int(server.server_address[1])
    url = f"http://127.0.0.1:{viewer.port}/?token={viewer.token}"
    print(f"filo ui: {url}", flush=True)
    print("It only reads, and it listens on this computer only. Ctrl-C stops it.", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
