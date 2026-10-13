#!/usr/bin/env python3
"""Local messaging between LLM agents.

``filo`` is a small addressed message bus for one machine and one user. A
scope (by default the current directory) holds the participants:

* listeners (``join``) own a Unix socket and receive messages directly;
* registered clients (``register``) have no listener but can be woken through
  an adapter, such as ``codex queue`` or ``agentapi send-message``;
* a recipient that is known but offline gets the message in a spool and
  receives it when it joins again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

APP = "filo"
DEFAULT_RUNTIME = "/tmp/filo-runtime"
SOCKET_TIMEOUT = 1.0
SERVER_TIMEOUT = 2.0
MAX_FRAME = 4 * 1024 * 1024
NAME_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
SESSION_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{5,127}")
MESSAGE_ID = re.compile(r"[0-9a-f]{10,20}-[0-9a-f]{6}")


class UserError(Exception):
    """A problem the user can fix; ``main`` prints it and exits with status 1."""


class ProtocolError(ValueError):
    """Data from a peer or a runtime file that is not what it should be."""


def env_seconds(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def spool_ttl() -> float:
    """How long an undelivered message is kept."""
    return env_seconds("FILO_SPOOL_TTL", 3600.0)


def known_ttl() -> float:
    """How long after it was last seen a name can still receive queued messages."""
    return env_seconds("FILO_KNOWN_TTL", 86400.0)


ACK_TTL = 86400.0

JsonObject = dict[str, Any]


# --- names, scopes and runtime files -----------------------------------------

def valid_name(value: str, label: str = "name") -> str:
    if not value or any(char not in NAME_ALPHABET for char in value):
        raise UserError(f"{label}: use only letters, digits, '.', '_' or '-'")
    return value


def canonical_scope(raw: str | None) -> str:
    value = raw or str(Path.cwd())
    path = Path(value).expanduser()
    if path.exists():
        return f"path:{path.resolve()}"
    return f"keyword:{value}"


def scope_path(scope: str) -> str | None:
    """The directory of a path scope; None for a keyword scope."""
    return scope[len("path:"):] if scope.startswith("path:") else None


def runtime_root() -> Path:
    return Path(os.environ.get("FILO_DIR", DEFAULT_RUNTIME)).expanduser()


def scope_directory(scope: str) -> Path:
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:32]
    directory = runtime_root() / digest
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def socket_path(scope: str, name: str) -> Path:
    return scope_directory(scope) / f"{valid_name(name)}.sock"


def metadata_path(scope: str, name: str) -> Path:
    return scope_directory(scope) / f"{valid_name(name)}.json"


def unlink_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def write_metadata(path: Path, data: JsonObject) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def as_object(value: Any, what: str) -> JsonObject:
    """``value`` as a JSON object, or ProtocolError."""
    if not isinstance(value, dict):
        raise ProtocolError(f"{what} is not a JSON object")
    return cast(JsonObject, value)


def read_object(path: Path) -> JsonObject:
    """Read a small JSON object owned by this user, or raise ProtocolError/OSError."""
    if path.stat().st_uid != os.getuid():
        raise ProtocolError("file is not owned by this user")
    if path.stat().st_size > MAX_FRAME:
        raise ProtocolError("file is too large")
    return as_object(json.loads(path.read_text(encoding="utf-8")), "file")


def int_field(data: JsonObject, key: str, default: int | None = None) -> int:
    """``data[key]`` as an int.

    A missing or null field gives ``default`` when there is one, otherwise
    KeyError; a field that is not a number raises ValueError or TypeError.
    """
    value = data.get(key)
    if value is None:
        if default is None:
            raise KeyError(key)
        return default
    return int(value)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- socket protocol ---------------------------------------------------------

def frame(payload: JsonObject) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def read_frame(connection: socket.socket) -> JsonObject:
    data = bytearray()
    while len(data) < MAX_FRAME:
        chunk = connection.recv(min(65536, MAX_FRAME - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        newline = data.find(b"\n")
        if newline >= 0:
            return as_object(json.loads(bytes(data[:newline])), "message")
    if len(data) >= MAX_FRAME:
        raise ProtocolError("message is too large")
    raise ProtocolError("connection closed before a complete message")


def request(path: Path, payload: JsonObject) -> JsonObject:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(SOCKET_TIMEOUT)
    try:
        connection.connect(str(path))
        connection.sendall(frame(payload))
        return read_frame(connection)
    finally:
        connection.close()


def peer_credentials(connection: socket.socket) -> tuple[int | None, int | None]:
    """(pid, uid) of the process on the other end of a Unix socket."""
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, _ = struct.unpack("3i", raw)
        return pid, uid
    except (OSError, AttributeError):
        return None, None


# --- listeners ---------------------------------------------------------------

def evict(scope: str, metadata: Path) -> None:
    unlink_if_present(metadata)
    try:
        unlink_if_present(socket_path(scope, metadata.stem))
    except UserError:
        pass


@dataclass(frozen=True)
class Participant:
    name: str
    pid: int


def listener_present(scope: str, name: str, pid: int) -> bool:
    """False only when the listener is provably gone.

    One that cannot be probed (a sandboxed caller may not connect to sockets) or
    that is merely slow still counts as present.
    """
    try:
        reply = request(socket_path(scope, name), {"op": "info"})
        return reply.get("name") == name and int_field(reply, "pid", -1) == pid
    except PermissionError:
        return True
    except TimeoutError:
        return pid_alive(pid)
    except (OSError, ValueError, TypeError):
        return False


def listener_is_live(scope: str, name: str) -> bool:
    """Whether a listener named ``name`` currently owns its socket."""
    try:
        pid = int_field(read_object(metadata_path(scope, name)), "pid")
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False
    return listener_present(scope, name, pid)


def participant_rows(scope: str, exclude: str | None = None) -> list[Participant]:
    """Listeners present in this scope; the ones that are provably gone are removed."""
    rows: list[Participant] = []
    for metadata in sorted(scope_directory(scope).glob("*.json")):
        try:
            info = read_object(metadata)
            name = valid_name(str(info["name"]), "metadata name")
            pid = int_field(info, "pid")
        except PermissionError:
            continue
        except (OSError, ValueError, TypeError, KeyError, UserError):
            evict(scope, metadata)
            continue
        if name == exclude:
            continue
        if listener_present(scope, name, pid):
            rows.append(Participant(name, pid))
        else:
            evict(scope, metadata)
    return rows


def print_rows(rows: list[Participant], heading: str) -> None:
    print(heading)
    if not rows:
        print("  (none)")
        return
    for row in rows:
        print(f"  {row.name}  pid={row.pid}")


def reply_hint(sender: str, me: str) -> str:
    """Command a recipient can run to answer ``sender``."""
    return f'filo send --to {sender} --from {me} --message "..."'


def compose_text(sender: str, recipient: str, body: str) -> str:
    """A message as an agent without a listener sees it: header, body, reply command."""
    return f"[filo] Message from {sender}: {body}\n\nTo reply: {reply_hint(sender, recipient)}"


def message_event(name: str, message: JsonObject, peer_pid: int | None = None) -> JsonObject:
    """The event a listener emits for one incoming message."""
    sender = str(message.get("from", "anonymous"))
    event: JsonObject = {
        "kind": "message", "id": message.get("id"), "from": sender, "to": name,
        "message": message.get("message", ""),
        "created_ns": message.get("created_ns"),
        "reply": reply_hint(sender, name),
    }
    if peer_pid is not None:
        event["peer_pid"] = peer_pid
    return event


def emit_event(payload: JsonObject, as_json: bool) -> None:
    """Print an event as one JSON line, or as text for a person."""
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), flush=True)
    elif payload.get("kind") == "message":
        print(f"\n[{payload['from']} -> {payload['to']}]\n{payload['message']}", flush=True)
    elif payload.get("kind") == "presence":
        print(f"\n[filo] {payload['name']} {payload['event']} this scope.", flush=True)


def notify(scope: str, sender: str, event: str) -> None:
    for row in participant_rows(scope, exclude=sender):
        try:
            request(socket_path(scope, row.name), {
                "op": "presence", "event": event, "name": sender,
            })
        except (OSError, ValueError, TimeoutError):
            continue


# --- known names, spool and acknowledgements ---------------------------------

def mark_known(scope: str, name: str, kind: str) -> None:
    """Remember that ``name`` exists here, so messages to it can wait while it is away."""
    write_metadata(scope_directory(scope) / "known" / f"{valid_name(name)}.json",
                   {"name": name, "kind": kind, "seen_ns": time.time_ns()})


def known_kind(scope: str, name: str) -> str:
    """What ``name`` was when last seen here ("listener", "registered"), or an empty string."""
    try:
        return str(read_object(scope_directory(scope) / "known" / f"{valid_name(name)}.json").get("kind", ""))
    except (OSError, ValueError, TypeError):
        return ""


def is_known(scope: str, name: str) -> bool:
    try:
        seen = int_field(read_object(scope_directory(scope) / "known" / f"{valid_name(name)}.json"), "seen_ns")
    except (OSError, ValueError, TypeError, KeyError):
        return False
    return time.time_ns() - seen <= known_ttl() * 1_000_000_000


def new_message_id() -> str:
    return f"{time.time_ns():x}-{secrets.token_hex(3)}"


def spool_message(scope: str, recipient: str, sender: str, body: str, message_id: str) -> None:
    """Leave a message as a file for ``recipient``'s listener to deliver."""
    directory = scope_directory(scope) / "spool" / valid_name(recipient)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_metadata(directory / f"{message_id}.msg", {
        "id": message_id, "from": sender, "message": body, "created_ns": time.time_ns(),
    })


def write_ack(scope: str, message_id: str, recipient: str, how: str) -> None:
    write_metadata(scope_directory(scope) / "acks" / f"{message_id}.json", {
        "id": message_id, "to": recipient, "how": how, "at_ns": time.time_ns(),
    })


def drain_spool(scope: str, name: str, deliver: Callable[[JsonObject], None]) -> None:
    """Deliver and remove the files left for ``name``; expired ones are dropped."""
    directory = scope_directory(scope) / "spool" / name
    for path in sorted(directory.glob("*.msg")):
        try:
            message = read_object(path)
        except (OSError, ValueError):
            unlink_if_present(path)
            continue
        unlink_if_present(path)
        if time.time_ns() - int_field(message, "created_ns", 0) > spool_ttl() * 1_000_000_000:
            continue
        deliver(message)
        write_ack(scope, str(message.get("id") or path.stem), name, "delivered from the spool")
    acks = scope_directory(scope) / "acks"
    for old in acks.glob("*.json"):
        try:
            if time.time() - old.stat().st_mtime > ACK_TTL:
                unlink_if_present(old)
        except OSError:
            continue


def message_status(scope: str, message_id: str) -> str:
    if not MESSAGE_ID.fullmatch(message_id):
        raise UserError("status: not a message id")
    directory = scope_directory(scope)
    try:
        ack = read_object(directory / "acks" / f"{message_id}.json")
        return f"delivered to {ack['to']} ({ack['how']})"
    except (OSError, ValueError, KeyError):
        pass
    for pending in directory.glob(f"spool/*/{message_id}.msg"):
        return f"pending for {pending.parent.name}"
    return "unknown: expired, or not sent in this scope"


# --- adapters: clients that are woken from outside ---------------------------

@dataclass(frozen=True)
class Adapter:
    """How to reach a client that has no listener.

    ``deliver(session, text, scope_path, env)`` hands ``text`` to the session and
    returns a short description of where it went; it exits with a message on
    failure. ``process`` is the executable name of the client, used to notice
    sessions that are gone. ``capture_env`` names environment variables that
    ``register`` copies from the session; ``deliver`` receives them as ``env``.
    ``alive(env)``, when given, says whether the session still runs. It is exact
    where counting processes is not, and answers True when it cannot tell.
    """
    client: str
    deliver: Callable[[str, str, str, dict[str, str]], str]
    id_env: str | None = None
    process: str | None = None
    discover: Callable[[str], str] | None = None
    capture_env: tuple[str, ...] = ()
    alive: Callable[[dict[str, str]], bool] | None = None


def codex_thread(scope: str) -> str:
    """Newest interactive Codex thread whose working directory is this scope.

    Codex keeps its threads in ``$CODEX_HOME/state_N.sqlite``. The schema is
    internal, so every failure is reported as a normal error rather than a trace.
    """
    directory = scope_path(scope)
    if directory is None:
        raise UserError("send: Codex threads are tied to a directory; use a path scope")
    home = Path(os.environ.get("FILO_CODEX_HOME", "~/.codex")).expanduser()
    versions = {int(m.group(1)): p for p in home.glob("state_*.sqlite")
                if (m := re.fullmatch(r"state_(\d+)\.sqlite", p.name))}
    if not versions:
        raise UserError(f"send: no Codex state database found in {home}")
    database = versions[max(versions)]
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
        try:
            row = connection.execute(
                "select id from threads where cwd = ? and archived = 0 "
                "and thread_source = 'user' and source != 'exec' "
                "order by updated_at desc limit 1", (directory,),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise UserError(f"send: cannot read the Codex thread list ({exc})") from exc
    if row is None:
        raise UserError(f"send: no Codex thread for {directory}; start codex there and send it a first message")
    return str(row[0])


def run_delivery(argv: list[str], label: str, env: dict[str, str] | None = None) -> None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False,
                                env={**os.environ, **env} if env else None)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UserError(f"send: {label} failed: {exc}") from exc
    if result.returncode != 0:
        raise UserError(f"send: {label} failed: {(result.stderr or result.stdout).strip()}")


def codex_deliver(session: str, text: str, _scope_path: str, _env: dict[str, str]) -> str:
    run_delivery(["codex", "queue", "--thread", session, "--message", text], "codex queue")
    return f"codex thread {session}"


AGY_ENV = ("ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN",
           "ANTIGRAVITY_PROJECT_ID", "ANTIGRAVITY_AGENTAPI_EXE")


def agy_deliver(session: str, text: str, _scope_path: str, env: dict[str, str]) -> str:
    """Hand ``text`` to a live agy conversation. agentapi needs the session's own sidecar address and token."""
    tool = env.get("ANTIGRAVITY_AGENTAPI_EXE")
    # The variable names agy itself, which needs the "agentapi" subcommand; the bare tool does not.
    command = [tool, "agentapi"] if tool else ["agentapi"]
    run_delivery([*command, "send-message", "--title=filo", session, text], "agentapi send-message", env)
    return f"agy conversation {session}"


def agy_alive(env: dict[str, str]) -> bool:
    """Whether agy's own sidecar still accepts connections; True when it cannot be probed."""
    host, _, port = env.get("ANTIGRAVITY_LS_ADDRESS", "").rpartition(":")
    if host not in {"localhost", "127.0.0.1"} or not port.isdigit():
        return True
    try:
        socket.create_connection((host, int(port)), timeout=1).close()
    except ConnectionRefusedError:
        return False
    except OSError:
        return True
    return True


def string_list(value: Any) -> list[str]:
    """``value`` as a non-empty list of strings, or ValueError."""
    items = cast(list[object], value) if isinstance(value, list) else []
    if not items or not all(isinstance(item, str) for item in items):
        raise ValueError("not a non-empty list of strings")
    return [str(item) for item in items]


def command_adapter(client: str, spec: Any) -> Adapter:
    """An adapter described in the config file, so no code change is needed."""
    if not isinstance(spec, dict):
        raise UserError(f"adapters: {client!r} must be an object")
    fields = cast(JsonObject, spec)
    try:
        template = string_list(fields.get("deliver"))
    except ValueError:
        raise UserError(f"adapters: {client!r} needs 'deliver' as a non-empty list of strings") from None
    id_env, process = fields.get("id_env"), fields.get("process")
    if any(value is not None and not isinstance(value, str) for value in (id_env, process)):
        raise UserError(f"adapters: {client!r}: 'id_env' and 'process' must be strings")
    try:
        capture = string_list(fields["capture_env"]) if "capture_env" in fields else []
    except ValueError:
        raise UserError(f"adapters: {client!r}: 'capture_env' must be a non-empty list of strings") from None

    def deliver(session: str, text: str, directory: str, env: dict[str, str]) -> str:
        run_delivery([part.replace("{session}", session).replace("{message}", text).replace("{scope}", directory)
                      for part in template], client, env)
        return f"{client} session {session}"

    return Adapter(client=client, deliver=deliver, id_env=id_env, process=process, capture_env=tuple(capture))


def adapters() -> dict[str, Adapter]:
    """Built-in adapters plus the ones in ``$FILO_ADAPTERS`` or ``~/.config/filo/adapters.json``."""
    found = {"codex": Adapter("codex", codex_deliver, id_env="CODEX_THREAD_ID",
                              process="codex", discover=codex_thread),
             "agy": Adapter("agy", agy_deliver, id_env="ANTIGRAVITY_CONVERSATION_ID",
                            process="agy", capture_env=AGY_ENV, alive=agy_alive)}
    path = Path(os.environ.get("FILO_ADAPTERS", "~/.config/filo/adapters.json")).expanduser()
    try:
        configured = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return found
    except (OSError, ValueError) as exc:
        raise UserError(f"adapters: cannot read {path}: {exc}") from exc
    if not isinstance(configured, dict):
        raise UserError(f"adapters: {path} must hold a JSON object")
    for client, spec in cast(JsonObject, configured).items():
        if client in found:
            raise UserError(f"adapters: {client!r} is built in and cannot be redefined")
        found[valid_name(client, "adapters")] = command_adapter(client, spec)
    return found


# --- registered clients ------------------------------------------------------

def sandboxed() -> bool:
    """True where the process table cannot be trusted (Codex sandbox, or forced)."""
    return bool(os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") or os.environ.get("FILO_NO_PRUNE"))


def process_cwds(command: str) -> list[str]:
    """Working directories of the running processes called ``command``."""
    found: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text(encoding="utf-8").strip() == command:
                found.append(os.readlink(entry / "cwd"))
        except OSError:
            continue
    return found


@dataclass(frozen=True)
class Registration:
    name: str
    client: str
    session: str
    registered_ns: int
    path: Path
    env: dict[str, str]


def read_registrations(scope: str) -> list[Registration]:
    rows: list[Registration] = []
    for path in sorted(scope_directory(scope).glob("*.reg")):
        try:
            info = read_object(path)
            name = valid_name(str(info["name"]), "registration name")
            client, session = str(info["client"]), str(info["session"])
            registered = int_field(info, "registered_ns", 0)
            env = {str(key): str(value) for key, value in as_object(info.get("env", {}), "env").items()}
        except (OSError, ValueError, TypeError, KeyError, UserError):
            continue
        if SESSION_ID.fullmatch(session):
            rows.append(Registration(name, client, session, registered, path, env))
    return rows


def live_registrations(scope: str) -> list[Registration]:
    """Registrations whose session still seems to exist.

    A client with a known process name is checked against the process table: if
    N of its processes run in this directory only the N newest registrations are
    kept, and the rest are removed. Inside a sandbox the table is not trusted
    and nothing is removed.
    """
    rows = read_registrations(scope)
    directory = scope_path(scope)
    if directory is None or sandboxed():
        return rows
    known = adapters()
    kept: list[Registration] = []
    for client in sorted({row.client for row in rows}):
        group = sorted((row for row in rows if row.client == client),
                       key=lambda row: row.registered_ns, reverse=True)
        adapter = known.get(client)
        if adapter is not None and adapter.alive is not None:
            for row in group:
                if adapter.alive(row.env):
                    kept.append(row)
                else:
                    unlink_if_present(row.path)
            continue
        if adapter is None or not adapter.process:
            kept += group
            continue
        alive = sum(1 for cwd in process_cwds(adapter.process) if cwd == directory)
        kept += group[:alive]
        for stale in group[alive:]:
            unlink_if_present(stale.path)
    return sorted(kept, key=lambda row: row.name)


def taken_names(scope: str) -> set[str]:
    directory = scope_directory(scope)
    return {path.stem for pattern in ("*.json", "*.reg") for path in directory.glob(pattern)}


def free_name(scope: str, base: str) -> str:
    """``base``, or ``base-2``, ``base-3``... when earlier ones are taken."""
    taken = taken_names(scope)
    for number in range(1, 100):
        name = base if number == 1 else f"{base}-{number}"
        if name not in taken:
            return name
    raise UserError(f"no free name left for {base!r}")


def register(args: argparse.Namespace) -> int:
    """Announce a client that has no listener, so others can reach it."""
    scope = canonical_scope(args.scope)
    client = valid_name(args.name, "--as")
    known = adapters()
    if client not in known:
        raise UserError(f"register: unknown client {client!r}; known: {', '.join(sorted(known))}")
    adapter = known[client]
    session = args.session or (os.environ.get(adapter.id_env, "") if adapter.id_env else "")
    guessed = False
    if not session and adapter.discover is not None:
        session, guessed = adapter.discover(scope), True
    if not session:
        hint = f" (set ${adapter.id_env} or pass --session)" if adapter.id_env else " (pass --session)"
        raise UserError(f"register: no session id for {client}{hint}")
    if not SESSION_ID.fullmatch(session):
        raise UserError(f"register: {session!r} is not a valid session id")
    same = [row for row in read_registrations(scope) if row.client == client and row.session == session]
    if args.chosen:
        name = valid_name(args.chosen, "--name")
        if name in taken_names(scope) and not (same and same[0].name == name):
            raise UserError(f"register: the name {name!r} is taken here")
    else:
        name = same[0].name if same else free_name(scope, client)
    if same and same[0].name != name:
        unlink_if_present(same[0].path)
    captured = {key: os.environ[key] for key in adapter.capture_env if key in os.environ}
    write_metadata(scope_directory(scope) / f"{name}.reg", {
        "name": name, "client": client, "session": session, "registered_ns": time.time_ns(), "env": captured,
    })
    mark_known(scope, name, "registered")
    if args.json:
        print(json.dumps({"kind": "registered", "name": name, "client": client, "session": session}), flush=True)
    else:
        print(f"registered as {name} for {client} session {session}"
              + (" (newest of this directory)" if guessed else "")
              + (f"; use --from {name} when you send" if name != client else ""))
    return 0


def rename(args: argparse.Namespace) -> int:
    """Give a registered session another name. A listener changes its name by joining again."""
    scope = canonical_scope(args.scope)
    old, new = valid_name(args.old, "rename"), valid_name(args.new, "rename")
    found = [row for row in read_registrations(scope) if row.name == old]
    if not found:
        raise UserError(f"rename: {old!r} is not a registered session here"
                        + ("; a listener changes its name by running 'filo join --as NEW'"
                           if metadata_path(scope, old).exists() else ""))
    if new in taken_names(scope):
        raise UserError(f"rename: the name {new!r} is taken here")
    row = found[0]
    write_metadata(scope_directory(scope) / f"{new}.reg", {
        "name": new, "client": row.client, "session": row.session, "registered_ns": row.registered_ns, "env": row.env,
    })
    forget_registration(scope, row)
    mark_known(scope, new, "registered")
    print(f"renamed {old} to {new}; use --from {new} when you send")
    return 0


# --- profiles ----------------------------------------------------------------

def profile_dirs() -> list[tuple[str, Path]]:
    """Where profiles are read from, the user's own first so that they win."""
    mine = Path(os.environ.get("FILO_PROFILES", "~/.config/filo/profiles")).expanduser()
    return [("yours", mine), ("built in", Path(__file__).resolve().parent / "skills" / "filo" / "profiles")]


def find_profiles() -> dict[str, tuple[str, Path]]:
    """Every profile by name, with where it comes from; a user's profile hides a built-in one."""
    found: dict[str, tuple[str, Path]] = {}
    for source, directory in reversed(profile_dirs()):
        for path in sorted(directory.glob("*.md")):
            if all(char in NAME_ALPHABET for char in path.stem):
                found[path.stem] = (source, path)
    return found


def profile_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UserError(f"profile: cannot read {path}: {exc}") from exc


def profile_command(args: argparse.Namespace) -> int:
    """Print a profile: the instructions an agent follows when it joins with that name."""
    name = valid_name(args.profile, "profile")
    found = find_profiles()
    if name not in found:
        known = ", ".join(sorted(found)) or "none"
        raise UserError(f"profile: there is no profile named {name!r}; known: {known}")
    print(profile_text(found[name][1]).rstrip("\n"))
    return 0


def profiles_command(_args: argparse.Namespace) -> int:
    """List the profiles, one per line, with the first line of each."""
    found = find_profiles()
    if not found:
        print("no profiles")
        return 0
    width = max(len(name) for name in found)
    for name in sorted(found):
        source, path = found[name]
        lines = profile_text(path).strip().splitlines()
        print(f"{name:<{width}}  [{source}]  {lines[0] if lines else ''}")
    return 0


def forget_registration(scope: str, row: Registration) -> None:
    """Remove a registration and the record that its name was ever seen."""
    unlink_if_present(row.path)
    unlink_if_present(scope_directory(scope) / "known" / f"{valid_name(row.name)}.json")


def leave(args: argparse.Namespace) -> int:
    """Take a registered session out of the pool. A listener leaves by stopping."""
    scope = canonical_scope(args.scope)
    rows = read_registrations(scope)
    if args.chosen:
        name = valid_name(args.chosen, "--name")
        found = [row for row in rows if row.name == name]
        if not found:
            raise UserError(f"leave: {name!r} is not a registered session here"
                            + ("; a listener leaves by stopping 'filo join' (stop its Monitor)"
                               if metadata_path(scope, name).exists() else "; see 'filo who'"))
    else:
        mine = {(adapter.client, os.environ[adapter.id_env]) for adapter in adapters().values()
                if adapter.id_env and os.environ.get(adapter.id_env)}
        found = [row for row in rows if (row.client, row.session) in mine]
        if not found:
            raise UserError("leave: this session is not registered here; pass --name NAME (see 'filo who')")
    for row in found:
        forget_registration(scope, row)
        print(f"{row.name} left the pool; its name is free again and messages to it are refused")
    return 0


# --- sending -----------------------------------------------------------------

def adapter_target(scope: str, recipient: str) -> tuple[Adapter, str, dict[str, str]] | None:
    known = adapters()
    for row in live_registrations(scope):
        if row.name == recipient and row.client in known:
            return known[row.client], row.session, row.env
    adapter = known.get(recipient)
    if adapter is not None and adapter.discover is not None:
        return adapter, adapter.discover(scope), {}
    return None


def deliver_offline(scope: str, recipient: str, sender: str, body: str, message_id: str) -> str:
    """Reach a recipient with no reachable listener: adapter, else spool, else fail."""
    target = adapter_target(scope, recipient)
    if target is not None:
        adapter, session, env = target
        where = adapter.deliver(session, compose_text(sender, recipient, body), scope_path(scope) or "", env)
        write_ack(scope, message_id, recipient, f"queued to {where}")
        return f"queued for {where} ({message_id})"
    if metadata_path(scope, recipient).exists():
        if listener_is_live(scope, recipient):
            spool_message(scope, recipient, sender, body, message_id)
            return f"left for {recipient} ({message_id}); it will deliver it shortly"
        evict(scope, metadata_path(scope, recipient))
    if is_known(scope, recipient) and known_kind(scope, recipient) == "registered":
        raise UserError(f"{recipient!r} was a registered session and has ended; nothing was queued. "
                        f"It has to run 'filo register' again")
    if is_known(scope, recipient):
        spool_message(scope, recipient, sender, body, message_id)
        return (f"{recipient} is offline: queued ({message_id}) for up to "
                f"{int(spool_ttl() // 60)} minutes, delivered when it joins again")
    raise UserError(f"recipient {recipient!r} is not connected and has not been seen here; "
                     f"run 'filo who' to see who is")


def deliver_message(scope: str, recipient: str, sender: str, body: str, message_id: str) -> str:
    payload: JsonObject = {
        "op": "message", "id": message_id, "from": sender, "message": body, "created_ns": time.time_ns(),
    }
    try:
        reply = request(socket_path(scope, recipient), payload)
    except PermissionError as exc:
        # Sandboxed sender: it cannot connect, but a listener exists (its
        # metadata is there), so leave the message for it to pick up.
        if not metadata_path(scope, recipient).exists():
            raise UserError(f"recipient {recipient!r} is not connected: {exc}") from exc
        spool_message(scope, recipient, sender, body, message_id)
        return f"left for {recipient} ({message_id}); delivery cannot confirm from this sandbox"
    except (OSError, ValueError):
        return deliver_offline(scope, recipient, sender, body, message_id)
    if not reply.get("ok"):
        raise UserError(str(reply.get("error", "recipient rejected the message")))
    return f"sent directly to {recipient} ({message_id})"


# --- the listener ------------------------------------------------------------

def claim_listener_name(scope: str, base: str, exact: bool) -> tuple[str, socket.socket]:
    """Bind a socket under ``base``, or ``base-2``... if that name is in use."""
    for number in range(1, 100):
        name = base if number == 1 else f"{base}-{number}"
        socket_file = socket_path(scope, name)
        metadata = metadata_path(scope, name)
        in_use = (scope_directory(scope) / f"{name}.reg").exists() or (
            metadata.exists() and listener_is_live(scope, name))
        if in_use:
            if exact:
                raise UserError(f"participant {name!r} is already connected")
            continue
        unlink_if_present(metadata)
        unlink_if_present(socket_file)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(socket_file))
        except OSError:
            server.close()
            continue
        return name, server
    raise UserError(f"no free name left for {base!r}")


class Listener:
    """A participant that receives messages on its Unix socket and prints them."""

    def __init__(self, scope: str, name: str, server: socket.socket, as_json: bool,
                 parent: int | None = None) -> None:
        self.scope = scope
        self.parent = parent
        self.name = name
        self.server = server
        self.as_json = as_json
        self.socket_file = socket_path(scope, name)
        self.metadata = metadata_path(scope, name)
        self.closed = False

    def open(self, shown_scope: str, existing: list[Participant]) -> None:
        """Start accepting, publish this listener and tell the others and the user."""
        os.chmod(self.socket_file, 0o600)
        self.server.listen(32)
        self.server.settimeout(0.5)
        write_metadata(self.metadata, {"name": self.name, "pid": os.getpid()})
        mark_known(self.scope, self.name, "listener")
        notify(self.scope, self.name, "joined")
        if self.as_json:
            print(json.dumps({"kind": "joined", "scope": shown_scope, "name": self.name,
                              "pid": os.getpid()}), flush=True)
            print(json.dumps({"kind": "participants", "participants": [asdict(row) for row in existing]}),
                  flush=True)
        else:
            print(f"joined {shown_scope} as {self.name} (pid={os.getpid()})", flush=True)
            print_rows(existing, "already connected:")
            print("listening; press Ctrl-C to leave", flush=True)

    def serve(self) -> None:
        """Deliver spooled messages and answer connections until the parent, if any, is gone."""
        while self.parent is None or pid_alive(self.parent):
            drain_spool(self.scope, self.name, self.print_spooled)
            try:
                connection, _ = self.server.accept()
            except TimeoutError:
                continue
            with connection:
                self.handle(connection)

    def print_spooled(self, message: JsonObject) -> None:
        self.emit(message_event(self.name, message))

    def emit(self, payload: JsonObject) -> None:
        emit_event(payload, self.as_json)

    def handle(self, connection: socket.socket) -> None:
        connection.settimeout(SERVER_TIMEOUT)
        peer_pid, peer_uid = peer_credentials(connection)
        try:
            if peer_uid is not None and peer_uid != os.getuid():
                raise ProtocolError("peer belongs to another user")
            response = self.respond(read_frame(connection), peer_pid)
        except (OSError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)}
        try:
            connection.sendall(frame(response))
        except OSError:
            pass

    def respond(self, message: JsonObject, peer_pid: int | None) -> JsonObject:
        operation = message.get("op")
        if operation == "info":
            return {"ok": True, "name": self.name, "pid": os.getpid()}
        if operation == "message":
            self.emit(message_event(self.name, message, peer_pid))
            if message.get("id"):
                write_ack(self.scope, str(message["id"]), self.name, "delivered directly")
            return {"ok": True}
        if operation == "presence":
            self.emit({"kind": "presence", "event": str(message.get("event", "changed")),
                       "name": str(message.get("name", "anonymous")), "to": self.name})
            return {"ok": True}
        return {"ok": False, "error": "unknown operation"}

    def close(self) -> None:
        """Leave the scope. Safe to call more than once."""
        if self.closed:
            return
        self.closed = True
        mark_known(self.scope, self.name, "listener")
        notify(self.scope, self.name, "left")
        self.server.close()
        unlink_if_present(self.socket_file)
        unlink_if_present(self.metadata)


def join(args: argparse.Namespace) -> int:
    scope = canonical_scope(args.scope)
    existing = participant_rows(scope)
    name, server = claim_listener_name(scope, valid_name(args.name, "--as"), args.exact)
    listener = Listener(scope, name, server, args.json, args.parent)

    def stop(_signum: int, _frame: object) -> None:
        listener.close()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    listener.open(args.scope or str(Path.cwd()), existing)
    try:
        listener.serve()
    finally:
        listener.close()
    return 0


# --- command line ------------------------------------------------------------

def scope_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scope", "--project", help="project path or shared keyword; defaults to the current directory")


def command_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP,
        description=(
            "Addressed messaging between LLM agents on this machine. A message goes straight to a "
            "listener; to a registered client such as codex or agy through its adapter; and to a "
            "recipient that is only away, into a spool that is delivered when it returns."
        ),
        epilog=(
            "Examples:\n"
            "  filo join --as claude --json      listen (a second 'claude' becomes claude-2)\n"
            "  filo register --as codex          announce this Codex session\n"
            "  filo register --as agy --name reviewer   pick your own name\n"
            "  filo rename agy-2 reviewer\n"
            "  filo leave                           take this registered session out of the pool\n"
            "  filo profiles                        the roles you can join as: /filo manager\n"
            "  filo profile manager                 print what a manager is told to do\n"
            "  filo who\n"
            "  filo send --to claude --from codex --message 'review this'\n"
            "  filo send --to codex --from claude --stdin < message.txt\n"
            "  filo status ID                    was a message delivered?\n"
            "\nThe default scope is the current directory. Set FILO_DIR to change the local runtime root."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    join_parser = sub.add_parser("join", help="join a scope and listen until interrupted")
    scope_argument(join_parser)
    join_parser.add_argument("--as", dest="name", required=True,
                             help="your participant name; if taken a numeric suffix is added")
    join_parser.add_argument("--exact", action="store_true", help="fail instead of adding a suffix")
    join_parser.add_argument("--parent", type=int, metavar="PID",
                             help="leave when this process ends (for a plugin that starts the listener)")
    join_parser.add_argument("--json", action="store_true", help="emit machine-readable output")

    register_parser = sub.add_parser(
        "register", help="announce a client that has no listener (codex, agy, or one from the adapters file)")
    scope_argument(register_parser)
    register_parser.add_argument("--as", dest="name", required=True, help="client name, for example codex or agy")
    register_parser.add_argument("--session", dest="session",
                                 help="session id; defaults to the client's environment variable")
    register_parser.add_argument("--name", dest="chosen",
                                 help="the name others use for this session; defaults to the client name")
    register_parser.add_argument("--json", action="store_true", help="emit machine-readable output")

    profile_parser = sub.add_parser("profile", help="print a profile: the instructions for a role")
    profile_parser.add_argument("profile", help="its name, for example manager")

    sub.add_parser("profiles", help="list the profiles you can join with")

    leave_parser = sub.add_parser("leave", help="take a registered session out of the pool")
    scope_argument(leave_parser)
    leave_parser.add_argument("--name", dest="chosen",
                              help="the registered name to remove; defaults to this session")

    rename_parser = sub.add_parser("rename", help="give a registered session another name")
    scope_argument(rename_parser)
    rename_parser.add_argument("old", help="its current name")
    rename_parser.add_argument("new", help="the new name")

    who_parser = sub.add_parser("who", help="list participants")
    scope_argument(who_parser)
    who_parser.add_argument("--json", action="store_true", help="emit machine-readable output")

    send_parser = sub.add_parser("send", help="send one message")
    scope_argument(send_parser)
    send_parser.add_argument("--to", required=True, help="recipient participant name")
    send_parser.add_argument("--from", dest="sender", default="anonymous", help="sender participant name")
    source = send_parser.add_mutually_exclusive_group()
    source.add_argument("--message", help="message text")
    source.add_argument("--stdin", action="store_true", help="read the entire message from standard input")
    send_parser.add_argument("text", nargs="?", help="message text, as an alternative to --message")

    status_parser = sub.add_parser("status", help="show whether a sent message was delivered")
    scope_argument(status_parser)
    status_parser.add_argument("id", help="the id printed by send")
    return parser


def who_command(args: argparse.Namespace, scope: str) -> int:
    participants = participant_rows(scope)
    registered = live_registrations(scope)
    if args.json:
        print(json.dumps({
            "scope": args.scope or str(Path.cwd()),
            "participants": [asdict(row) for row in participants],
            "registered": [{"name": row.name, "client": row.client, "session": row.session}
                           for row in registered],
        }), flush=True)
        return 0
    print_rows(participants, f"connected to {args.scope or Path.cwd()}:")
    for row in registered:
        print(f"  {row.name}  {row.client} session={row.session}  (registered)")
    return 0


def message_body(args: argparse.Namespace) -> str:
    """The text to send, from --stdin, --message or the positional argument."""
    if args.stdin + (args.message is not None) + (args.text is not None) > 1:
        raise UserError("send: choose one of --stdin, --message or a positional message")
    if args.stdin:
        body = sys.stdin.read()
    else:
        body = args.message if args.message is not None else (args.text or "")
    if not body:
        raise UserError("send: refusing an empty message")
    return str(body)


def send_command(args: argparse.Namespace, scope: str) -> int:
    body = message_body(args)
    recipient = valid_name(args.to, "--to")
    sender = valid_name(args.sender, "--from")
    print(deliver_message(scope, recipient, sender, body, new_message_id()))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = command_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "join":
            return join(args)
        if args.command == "register":
            return register(args)
        if args.command == "profile":
            return profile_command(args)
        if args.command == "profiles":
            return profiles_command(args)
        if args.command == "leave":
            return leave(args)
        if args.command == "rename":
            return rename(args)
        scope = canonical_scope(args.scope)
        if args.command == "who":
            return who_command(args, scope)
        if args.command == "status":
            print(message_status(scope, args.id))
            return 0
        return send_command(args, scope)
    except UserError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
