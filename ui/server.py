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
HISTORY = 500           # entries that a page starts with
LEGACY_EVERY = 1.0      # how often the inbox records and acknowledgements are looked at for new ones
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
        shown = self.core.registered_rows(registered, waits)
        for row in shown:    # to the minute: the page shows minutes, and a number that changes every second is a roster event
            if "oldest_seconds" in row:
                row["oldest_seconds"] = int(row["oldest_seconds"]) // 60 * 60
        value = {"scope": self.core.display_scope(scope),
                 "participants": [asdict(row) for row in participants],
                 "registered": shown}
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

    def legacy_paths(self, scope: str) -> list[tuple[Path, str]]:
        """What a filo without the log kept: what its listeners received (``inbox/NAME/ID.json``, for a day) and the
        acknowledgements of what was handed to a client's queue (``acks/ID.json``, which hold no text)."""
        directory = Path(self.core.scope_directory(scope))
        found: list[tuple[Path, str]] = [(path, "inbox") for path in directory.glob("inbox/*/*.json")
                                         if not path.name.startswith(".")]
        found += [(path, "ack") for path in directory.glob("acks/*.json")]
        return found

    @staticmethod
    def legacy_event(path: Path, kind: str) -> dict[str, Any] | None:
        """One of those files as an entry of the log: a received message was delivered, and a queued one is shown
        without its text and its sender, which were not kept."""
        try:
            record: Any = json.loads(path.read_text(encoding="utf-8"))
            mtime = path.stat().st_mtime_ns
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict):
            return None
        data = cast(dict[str, Any], record)
        if kind == "inbox":
            return {"kind": "message", "id": data.get("id") or path.stem, "from": data.get("from"),
                    "to": data.get("to") or path.parent.name, "state": "delivered", "body": data.get("message", ""),
                    "t": int(data.get("received_ns") or mtime), "kept": "inbox"}
        if not str(data.get("how", "")).startswith("queued to "):
            return None    # delivered directly: the inbox of its listener has the message
        return {"kind": "message", "id": data.get("id") or path.stem, "from": None, "to": data.get("to"),
                "state": "queued", "body": None, "t": int(data.get("at_ns") or mtime), "kept": "ack"}

    def log_start(self, scope: str) -> int | None:
        """When the log of the scope begins: what is older than that was not kept by a filo that writes it."""
        try:
            with open(self.log_path(scope), "rb") as handle:
                first: Any = json.loads(handle.readline())
        except (OSError, ValueError):
            return None
        if not isinstance(first, dict):
            return None
        stamp: Any = cast(dict[str, Any], first).get("t")
        return int(stamp) if isinstance(stamp, int) else None

    def mark(self, scope: str, event: dict[str, Any]) -> dict[str, Any]:
        """Say of an entry that came from an older file whether it was sent before the log began or after: after, and
        without text, means that the filo of its sender did not write it."""
        start = self.log_start(scope)
        event["before_log"] = start is None or int(event.get("t", 0)) < start
        return event

    def new_legacy(self, scope: str, since_ns: int, seen: set[str]) -> list[dict[str, Any]]:
        """The entries from those files that came after ``since_ns`` and that were not looked at before."""
        fresh: list[dict[str, Any]] = []
        for path, kind in self.legacy_paths(scope):
            key = f"{kind}:{path.stem}"
            if key in seen:
                continue
            seen.add(key)
            try:
                if path.stat().st_mtime_ns < since_ns - 2_000_000_000:
                    continue
            except OSError:
                continue
            event = self.legacy_event(path, kind)
            if event is not None:
                fresh.append(self.mark(scope, event))
        return sorted(fresh, key=lambda item: int(item.get("t", 0)))

    def history(self, scope: str) -> tuple[list[dict[str, Any]], int, int]:
        """The latest entries (the log's, and what older filo kept for a message that the log does not have), the
        position in the log after them, and the time from which new things are looked for in the older files."""
        since = time.time_ns() - 5 * 10**9
        entries, offset = self.read_since(scope, max(0, self._size(scope) - READ_LIMIT))
        known = {str(item.get("id")) for item in entries if item.get("kind") == "message"}
        for path, kind in self.legacy_paths(scope):
            if path.stem in known:
                continue
            event = self.legacy_event(path, kind)
            if event is not None and str(event["id"]) not in known:
                known.add(str(event["id"]))
                entries.append(self.mark(scope, event))
        entries.sort(key=lambda item: int(item.get("t", 0)))
        return entries[-HISTORY:], offset, since

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
                    entries, offset, since = viewer.history(scope)
                    self.json({**viewer.roster(scope), "events": entries, "offset": offset, "since_ns": since})
                else:
                    self.stream(scope, int((query.get("offset") or ["0"])[0] or 0),
                                int((query.get("since") or [str(time.time_ns())])[0] or 0))
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

        def stream(self, scope: str, offset: int, since: int) -> None:
            """Server-sent events: new entries of the log as they are written, what older filo writes (from
            ``since`` on), and the roster when it changes."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            last_roster = ""
            next_roster = 0.0
            next_legacy = 0.0
            quiet_since = time.monotonic()
            seen: set[str] = set()

            def send(event: str, data: Any) -> None:
                self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()

            try:
                while True:
                    entries, offset = viewer.read_since(scope, offset)
                    if time.monotonic() >= next_legacy:
                        entries += viewer.new_legacy(scope, since, seen)
                        next_legacy = time.monotonic() + LEGACY_EVERY
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
