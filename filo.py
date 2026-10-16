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
import fcntl
import hashlib
import http.client
import json
import os
import re
import secrets
import select
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

APP = "filo"
__version__ = "0.4.4"
DEFAULT_RUNTIME = "/tmp/filo"
SOCKET_TIMEOUT = 1.0
# A Unix socket path must fit in sockaddr_un: 103 bytes and a NUL on macOS and the BSDs, 107 on Linux.
SOCKET_PATH_LIMIT = 103 if sys.platform != "linux" else 107
SERVER_TIMEOUT = 2.0
MAX_FRAME = 4 * 1024 * 1024
# A file is written with every non-ASCII character escaped, which takes up to six bytes for each byte
# of a frame, so a message that fit in a frame has to fit in the file that keeps it.
MAX_FILE = 6 * MAX_FRAME + 64 * 1024
MAX_BODY = MAX_FRAME - 64 * 1024    # the text of a message: the rest of the frame is for its other fields
NAME_LIMIT = 64    # a file name made of it, with its suffixes, must fit in a socket path and a file system
NAME_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
SESSION_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{5,127}")
MESSAGE_ID = re.compile(r"[0-9a-f]{10,20}-[0-9a-f]{6}")
PLACEHOLDER = re.compile(r"\{(session|message|scope)\}")
RELEASES_URL = "https://github.com/Galfurian/filo/releases"
RELEASE_TAG = re.compile(r"/releases/tag/v(\d+\.\d+\.\d+)$")
STAMPED_VERSION = re.compile(r'(?m)^version="(\d+\.\d+\.\d+)"$')
VERSION = re.compile(r"\d+\.\d+\.\d+")
RELEASE_MARKER = ".filo-release"  # written by get.sh into the directory it unpacks
UPDATE_CHECK_TTL = 24 * 3600.0


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
INBOX_TTL = 86400.0  # how long a listener keeps what it received, for `filo show`
MIN_PREVIEW = 100
HOME_LIMIT = 1000  # the longest scope a message may say it comes from

JsonObject = dict[str, Any]


# --- names, scopes and runtime files -----------------------------------------

def valid_name(value: str, label: str = "name") -> str:
    if not value or any(char not in NAME_ALPHABET for char in value) or value in (".", ".."):
        raise UserError(f"{label}: use only letters, digits, '.', '_' or '-', and not '.' or '..'")
    if len(value) > NAME_LIMIT:
        raise UserError(f"{label}: at most {NAME_LIMIT} characters")
    return value


def canonical_scope(raw: str | None) -> str:
    try:
        value = raw or str(Path.cwd())
    except OSError:
        raise UserError("the current directory no longer exists: change to another or pass --scope") from None
    try:
        path = Path(value).expanduser()
        if path.exists():
            return f"path:{path.resolve()}"
    except (RuntimeError, OSError):    # "~someone" who is not a user, or a name too long to look up: a keyword
        pass
    return f"keyword:{value}"


def scope_path(scope: str) -> str | None:
    """The directory of a path scope; None for a keyword scope."""
    return scope[len("path:"):] if scope.startswith("path:") else None


def display_scope(scope: str) -> str:
    """A scope as a person writes it: the directory, or the keyword."""
    return scope_path(scope) or scope.split(":", 1)[-1]


def current_origin() -> str | None:
    """The directory this process runs in."""
    try:
        return str(Path.cwd())
    except OSError:
        return None


def is_member(scope: str, name: str) -> bool:
    """Whether ``name`` has a listener or a registration in ``scope``. It only looks at what is on disk:
    it creates nothing and probes nothing, so a name that is gone but not yet cleaned up counts."""
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
    directory = runtime_root() / digest
    return (directory / f"{name}.json").is_file() or (directory / f"{name}.reg").is_file()


def my_sessions() -> set[tuple[str, str]]:
    """The (client, session id) of the registered client that this process runs in, from its environment."""
    try:
        known = adapters()
    except UserError:    # a broken adapters file must not stop a command that does not need it
        return set()
    return {(adapter.client, os.environ[adapter.id_env]) for adapter in known.values()
            if adapter.id_env and os.environ.get(adapter.id_env)}


def holds_session(scope: str, sessions: set[tuple[str, str]]) -> bool:
    """Whether one of ``sessions`` is registered in ``scope``. Like is_member, it only reads."""
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
    for path in (runtime_root() / digest).glob("*.reg"):
        try:
            info = read_object(path)
            if (str(info["client"]), str(info["session"])) in sessions:
                return True
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return False


def resolve_scope(raw: str | None, sender: str | None = None) -> str:
    """The scope a command works in. ``--scope`` (``raw``) wins. Otherwise it is the scope of the current
    directory, except that a session which is registered in another scope stays there, and so does a
    sender (``--from``) that is a member of one, when the shell of the session is in some other
    directory, for example in a git worktree of the project. It is never a guess: the one other scope
    that holds the session or the name, and not two."""
    here = canonical_scope(raw)
    if raw is not None:
        return here
    sessions = my_sessions()
    if sessions and holds_session(here, sessions):
        return here
    if sessions:
        found = [scope for scope in known_scopes() if scope != here and holds_session(scope, sessions)]
        if len(found) == 1:
            return _moved(here, found[0], "this session is registered there")
    if sender and not is_member(here, sender):
        found = [scope for scope in known_scopes() if scope != here and is_member(scope, sender)]
        if len(found) == 1:
            return _moved(here, found[0], f"{sender} is there")
    return here


def _moved(here: str, scope: str, why: str) -> str:
    print(f"filo: working in {display_scope(scope)}, not in {display_scope(here)}: {why} "
          f"(--scope chooses another)", file=sys.stderr)
    return scope


def current_home(sender: str, target: str) -> str | None:
    """The scope that the sender of a message belongs to, which is where it can be answered.

    A session is not always in the directory of its scope: an agent that registered in a project and
    then moved to a worktree is still in the project's scope. So the answer is the first of: the scope of
    the directory the sender runs in, if it is a member; the scope the message is for, if it is a member
    there; the one other scope in which it is a member; and, failing those, the directory it runs in."""
    try:
        here = canonical_scope(None)
    except OSError:
        return None
    for scope in (here, target):
        if is_member(scope, sender):
            return scope
    others = [scope for scope in known_scopes() if scope not in (here, target) and is_member(scope, sender)]
    return others[0] if len(others) == 1 else here


def sender_fields(sender: str, target: str) -> JsonObject:
    """What a message says about where it comes from: ``home`` is its sender's scope, ``origin``
    the directory it ran in (what filo before 0.3.2 looked at)."""
    fields: JsonObject = {}
    origin, home = current_origin(), current_home(sender, target)
    if origin is not None:
        fields["origin"] = origin
    if home is not None:
        fields["home"] = home
    return fields


def sent_from(scope: str, message: JsonObject) -> tuple[bool, str | None]:
    """Whether ``message`` comes from outside ``scope``, and the scope it says it comes from.

    A message that names its home scope is outside when that is another scope, and one whose home is
    not a scope at all is outside too. One from an older filo names only the directory it ran in.
    A message that says nothing is not taken for an outsider: there is nothing to go on."""
    if "home" in message:
        home = message["home"]
        valid = isinstance(home, str) and len(home) <= HOME_LIMIT and home.startswith(("path:", "keyword:"))
        if not valid or not isinstance(home, str):
            return True, None
        return home != scope, display_scope(home)
    origin = message.get("origin")
    if isinstance(origin, str) and origin and len(origin) <= HOME_LIMIT:
        return is_foreign(scope, origin), origin
    return False, None


def is_foreign(scope: str, origin: object) -> bool:
    """Whether a message that says it was sent from ``origin`` comes from outside this directory scope."""
    directory = scope_path(scope)
    if not isinstance(origin, str) or not origin or directory is None:
        return False
    try:
        return os.path.realpath(origin) != directory
    except (OSError, ValueError):    # a path that cannot be looked at is not this directory
        return True


def secure_directory(path: Path) -> Path:
    """Make ``path`` a directory that is private to this user, or refuse to use it.

    One that is a link, is not a directory, or belongs to someone else is refused: anyone
    could have made it before us, and what we put in it would be theirs. One that is ours
    but open to others is closed to ``0700``, unless it already holds a link or something
    of another user's, which could have been put there while it was open.
    """
    refusal = (f"{path} is not a plain directory of yours, so filo will not use it; "
               f"remove it or set FILO_DIR to another")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise UserError(refusal)
        if info.st_mode & 0o077:
            with os.scandir(path) as entries:
                for entry in entries:
                    inside = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(inside.st_mode) or inside.st_uid != os.getuid():
                        raise UserError(f"{refusal} (it is open to others and holds {entry.name!r}, "
                                        f"which is a link or not yours)")
            os.chmod(path, 0o700)
    except OSError as exc:
        raise UserError(f"directory {path}: {exc.strerror or exc}") from exc
    return path


def runtime_root() -> Path:
    """The directory that holds all state, private to this user.

    The default is ``/tmp/filo-<uid>``, so two users never share a name.
    """
    return secure_directory(Path(os.environ.get("FILO_DIR", f"{DEFAULT_RUNTIME}-{os.getuid()}")).expanduser())


def scope_directory(scope: str) -> Path:
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
    return secure_directory(runtime_root() / digest)


def remember_scope(scope: str) -> None:
    """Write down which scope this directory is, so that `who --all` can say. Best effort."""
    path = scope_directory(scope) / ".scope"
    try:
        if path.exists() and read_object(path).get("scope") == scope:
            return
        write_metadata(path, {"scope": scope})
    except (OSError, ValueError):
        pass


def known_scopes() -> list[str]:
    """The scopes whose directory says what it is, in the order of their names."""
    found: list[str] = []
    for directory in sorted(runtime_root().iterdir()):
        try:
            scope = str(read_object(directory / ".scope")["scope"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if directory.name == scope_directory(scope).name and scope not in found:
            found.append(scope)
    return sorted(found, key=display_scope)


def socket_path(scope: str, name: str) -> Path:
    return scope_directory(scope) / f"{valid_name(name)}.sock"


def metadata_path(scope: str, name: str) -> Path:
    return scope_directory(scope) / f"{valid_name(name)}.json"


def unlink_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def write_metadata(path: Path, data: JsonObject, exclusive: bool = False) -> bool:
    """Write ``data`` to ``path`` in one step. With ``exclusive`` it is written only if ``path``
    does not exist, and the answer says whether it was: that is how a name is claimed by
    one process and never by two. Without it an existing file is replaced, and the answer is True."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.chmod(temporary, 0o600)
    if not exclusive:
        os.replace(temporary, path)
        return True
    try:
        os.link(temporary, path)   # fails when the name exists, and the file appears already complete
        return True
    except FileExistsError:
        return False
    finally:
        unlink_if_present(temporary)


def parse_json(data: str | bytes) -> Any:
    """``json.loads`` that raises only ValueError: nesting too deep for the parser is a ProtocolError."""
    try:
        return json.loads(data)
    except RecursionError:
        raise ProtocolError("JSON nested too deeply") from None


def as_object(value: Any, what: str) -> JsonObject:
    """``value`` as a JSON object, or ProtocolError."""
    if not isinstance(value, dict):
        raise ProtocolError(f"{what} is not a JSON object")
    return cast(JsonObject, value)


def read_object(path: Path) -> JsonObject:
    """Read a small JSON object owned by this user, or raise ProtocolError/OSError.

    The file is opened without following a link and checked through the descriptor, so
    what is checked is what is read.
    """
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ProtocolError("not a regular file")
        if info.st_uid != os.getuid():
            raise ProtocolError("file is not owned by this user")
        if info.st_size > MAX_FILE:
            raise ProtocolError("file is too large")
        return as_object(parse_json(handle.read().decode("utf-8")), "file")


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
    try:
        return int(value)
    except OverflowError:    # infinity
        raise ValueError(f"{key} is not a finite number") from None


def pid_alive(pid: int) -> bool:
    if pid <= 0:    # kill() takes these for a group of processes, and finds one
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):    # OverflowError: a number that no process has
        return False
    except PermissionError:
        return True
    return True


# --- socket protocol ---------------------------------------------------------

def frame(payload: JsonObject) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def read_frame(connection: socket.socket, timeout: float | None = None) -> JsonObject:
    """One message from a connection. With ``timeout`` the whole message has to arrive within it: a timeout
    on each read starts again with every byte, and so lets a client that sends one now and then hold on."""
    deadline = None if timeout is None else time.monotonic() + timeout
    data = bytearray()
    while len(data) < MAX_FRAME:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("the message did not arrive in time")
            connection.settimeout(remaining)
        chunk = connection.recv(min(65536, MAX_FRAME - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        newline = data.find(b"\n")
        if newline >= 0:
            return as_object(parse_json(bytes(data[:newline])), "message")
    if len(data) >= MAX_FRAME:
        raise ProtocolError("message is too large")
    raise ProtocolError("connection closed before a complete message")


def request(path: Path, payload: JsonObject) -> JsonObject:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(SOCKET_TIMEOUT)
    try:
        connection.connect(str(path))
        connection.sendall(frame(payload))
        return read_frame(connection, SOCKET_TIMEOUT)
    finally:
        connection.close()


def peer_credentials(connection: socket.socket) -> tuple[int | None, int | None]:
    """(pid, uid) of the process on the other end of a Unix socket.

    Linux has ``SO_PEERCRED`` and macOS has ``LOCAL_PEERPID`` and ``LOCAL_PEERCRED``.
    Elsewhere, or when the system refuses, nothing is known and the private directory
    is what keeps other users out.
    """
    try:
        if sys.platform == "darwin":
            sol_local, local_peercred, local_peerpid = 0, 1, 2
            pid = int(struct.unpack("i", connection.getsockopt(sol_local, local_peerpid, struct.calcsize("i")))[0])
            _version, uid = struct.unpack_from("II", connection.getsockopt(sol_local, local_peercred, 128))
            return pid, int(uid)
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, _ = struct.unpack("3i", raw)
        return pid, uid
    except (OSError, AttributeError, struct.error):
        return None, None


# --- listeners ---------------------------------------------------------------

def name_is_held(scope: str, name: str) -> bool:
    """Whether a listener holds ``name``: it keeps the lock of its name for as long as it lives, a slow or
    busy one as well as one that is still starting, and the system takes the lock back when it ends, so
    that this is true of nobody that is gone. False too for a listener of a filo that took no lock."""
    try:
        descriptor = os.open(scope_directory(scope) / f"{valid_name(name)}.lock",
                             os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except (OSError, UserError):
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(descriptor)    # which gives back a lock that this took
    return False


def evict(scope: str, metadata: Path) -> None:
    """Remove the files of a listener that is gone, unless something holds its name: what is there then
    belongs to a listener that is slow, or that has only just taken the name."""
    if name_is_held(scope, metadata.stem):
        return
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
    except (TimeoutError, BlockingIOError):    # busy, or its queue of connections is full: not gone
        return pid_alive(pid)
    except (OSError, ValueError, TypeError):
        return name_is_held(scope, name)


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


def registered_rows(rows: list[Registration]) -> list[JsonObject]:
    return [{"name": row.name, "client": row.client, "session": row.session} for row in rows]


def print_rows(rows: list[Participant], heading: str, registered: Sequence[Registration] = ()) -> None:
    print(heading)
    if not rows and not registered:
        print("  (none)")
        return
    for row in rows:
        print(f"  {row.name}  pid={row.pid}")
    for entry in registered:
        print(f"  {entry.name}  {entry.client} session={entry.session}  (registered)")


def reply_hint(sender: str, me: str, origin: str | None = None) -> str:
    """Command a recipient can run to answer ``sender``; ``origin`` is where it has to be sent when
    the sender is in another directory."""
    where = f" --scope {shlex.quote(origin)}" if origin else ""
    return f'filo send --to {shlex.quote(sender)} --from {shlex.quote(me)}{where} --message "..."'


def compose_text(sender: str, recipient: str, body: str, home: str | None = None,
                 scope: str | None = None) -> str:
    """A message as an agent without a listener sees it: header, body, reply command. A message from
    another scope says so, and its reply command names that scope. ``home`` is the sender's scope."""
    outside, where = (False, None) if scope is None or home is None else sent_from(scope, {"home": home})
    sent = (f", sent from {where} (another scope)" if where else ", sent from another scope") if outside else ""
    return f"[filo] Message from {sender}{sent}: {body}\n\nTo reply: {reply_hint(sender, recipient, where if outside else None)}"


def message_event(name: str, message: JsonObject, peer_pid: int | None = None,
                  preview: int | None = None, scope: str | None = None) -> JsonObject:
    """The event a listener emits for one incoming message. The body comes last, so that a client
    that shows only the start of a line still shows who wrote, the id and how to reply.

    ``scope`` is the scope of the listener. A message that comes from another scope has ``foreign``
    true and ``origin``, the scope its sender belongs to, and its reply command names that scope;
    one from the listener's own scope has neither.

    With ``preview``, an event longer than that many characters carries only the start of the body
    and says so: ``truncated``, ``length`` (of the whole body) and ``show``, the command that prints it."""
    sender = str(message.get("from", "anonymous"))
    outside, origin = sent_from(scope, message) if scope is not None else (False, None)
    event: JsonObject = {"kind": "message", "id": message.get("id"), "from": sender, "to": name}
    if outside:
        event["foreign"] = True
        if origin is not None:
            event["origin"] = origin
    event["reply"] = reply_hint(sender, name, origin if outside else None)
    event["created_ns"] = message.get("created_ns")
    if peer_pid is not None:
        event["peer_pid"] = peer_pid
    event["message"] = message.get("message", "")
    if preview is None or not message.get("id"):
        return event
    return preview_event(event, preview)


def preview_event(event: JsonObject, limit: int) -> JsonObject:
    """``event`` as it is when its JSON line fits in ``limit`` characters, else with the body cut to fit."""
    def size(candidate: JsonObject) -> int:
        return len(json.dumps(candidate, ensure_ascii=False))

    body = str(event["message"])
    if size(event) <= limit or not body:    # nothing to cut from an empty body
        return event
    head = {key: value for key, value in event.items() if key != "message"}

    def cut(length: int) -> JsonObject:
        return {**head, "truncated": True, "length": len(body), "show": f"filo show {event['id']}",
                "message": body[:length]}

    low, high = 0, len(body)    # the longest start of the body that still fits; none at all is allowed
    while low < high:
        middle = (low + high + 1) // 2
        if size(cut(middle)) <= limit:
            low = middle
        else:
            high = middle - 1
    return cut(low)


def already_received(scope: str, name: str, message: JsonObject) -> bool:
    """True for a message that ``name`` kept already. A sender whose wait for the answer ran out sends it
    again through the spool, and the listener has then both: the same id comes twice."""
    message_id = message.get("id")
    if not isinstance(message_id, str) or not MESSAGE_ID.fullmatch(message_id):
        return False
    return (scope_directory(scope) / "inbox" / valid_name(name) / f"{message_id}.json").exists()


def keep_received(scope: str, name: str, message: JsonObject, peer_pid: int | None = None,
                  listener: tuple[int, int] | None = None) -> None:
    """Keep what ``name`` received, so that `filo show` can print it whole and `filo watch` can
    report it. Best effort. ``listener`` is the pid of the listener and the number that it gave this message:
    the order of arrival does not depend on a clock, which can be set back."""
    message_id = message.get("id")
    if not isinstance(message_id, str) or not MESSAGE_ID.fullmatch(message_id):
        return
    record: JsonObject = {
        "id": message_id, "from": message.get("from", "anonymous"), "to": name,
        "message": message.get("message", ""), "created_ns": message.get("created_ns"),
        "received_ns": time.time_ns(),
    }
    for key in ("origin", "home"):
        value = message.get(key)
        if isinstance(value, str) and len(value) <= HOME_LIMIT:
            record[key] = value
    if peer_pid is not None:
        record["peer_pid"] = peer_pid
    if listener is not None:
        record["listener"], record["seq"] = listener
    try:
        write_metadata(scope_directory(scope) / "inbox" / valid_name(name) / f"{message_id}.json", record)
    except OSError:
        pass


def prune_inbox(scope: str) -> None:
    for old in scope_directory(scope).glob("inbox/*/*.json"):
        try:
            if time.time() - old.stat().st_mtime > INBOX_TTL:
                unlink_if_present(old)
        except OSError:
            continue


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
    record: JsonObject = {"id": message_id, "from": sender, "message": body, "created_ns": time.time_ns(),
                          **sender_fields(sender, scope)}
    write_metadata(directory / f"{message_id}.msg", record)


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
        if time.time_ns() - int_field(message, "created_ns", 0) > spool_ttl() * 1_000_000_000:
            unlink_if_present(path)
            continue
        try:
            deliver(message)    # the file goes after the delivery: if that is cut short it is still here
        except ValueError:      # a message that cannot be shown will not be shown by trying again
            unlink_if_present(path)
            continue
        unlink_if_present(path)
        identifier = message.get("id") or path.stem
        if isinstance(identifier, str) and MESSAGE_ID.fullmatch(identifier):
            write_ack(scope, identifier, name, "delivered from the spool")


def prune_acks(scope: str) -> None:
    for old in (scope_directory(scope) / "acks").glob("*.json"):
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
    capture_env: tuple[str, ...] = ()
    alive: Callable[[dict[str, str]], bool] | None = None


def captured_env(adapter: Adapter, env: dict[str, str]) -> dict[str, str]:
    """The part of a registration's ``env`` that the adapter asked ``register`` to capture: nothing else
    in the file is given to a command, whatever the file says."""
    return {key: value for key, value in env.items() if key in adapter.capture_env}


def run_delivery(argv: list[str], label: str, env: dict[str, str] | None = None) -> None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=30, check=False,
                                env={**os.environ, **env} if env else None)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:    # ValueError: a NUL in an argument
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
        values = {"session": session, "message": text, "scope": directory}
        # In one pass, so that a message that contains "{scope}" reaches the command as written.
        run_delivery([PLACEHOLDER.sub(lambda found: values[found.group(1)], part) for part in template],
                     client, env)
        return f"{client} session {session}"

    return Adapter(client=client, deliver=deliver, id_env=id_env, process=process, capture_env=tuple(capture))


def adapters() -> dict[str, Adapter]:
    """Built-in adapters plus the ones in ``$FILO_ADAPTERS`` or ``~/.config/filo/adapters.json``."""
    found = {"codex": Adapter("codex", codex_deliver, id_env="CODEX_THREAD_ID",
                              process="codex"),
             "agy": Adapter("agy", agy_deliver, id_env="ANTIGRAVITY_CONVERSATION_ID",
                            process="agy", capture_env=AGY_ENV, alive=agy_alive)}
    path = Path(os.environ.get("FILO_ADAPTERS", "~/.config/filo/adapters.json")).expanduser()
    try:
        configured = parse_json(path.read_text(encoding="utf-8"))
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


COMM_LIMIT = 15    # characters of a process's name in /proc/PID/comm


def parse_lsof(output: str, command: str) -> list[str]:
    """The ``n`` (path) lines that belong to processes called ``command`` in ``lsof -Fcn`` output."""
    found: list[str] = []
    current: str | None = None
    for line in output.splitlines():
        if line.startswith("p"):
            current = None
        elif line.startswith("c"):
            current = line[1:]
        elif line.startswith("n") and current == command:
            found.append(line[1:])
    return found


def process_cwds(command: str) -> list[str] | None:
    """Working directories of the running processes called ``command``.

    ``/proc`` answers on Linux; ``lsof`` does elsewhere, for example on macOS. None
    means this system cannot tell, and nobody must be forgotten on that account.
    """
    proc = Path("/proc")
    if proc.is_dir():
        found: list[str] = []
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                # the kernel keeps 15 characters of a process's name, so a longer one is never equal to it
                if (entry / "comm").read_text(encoding="utf-8").strip() == command[:COMM_LIMIT]:
                    found.append(os.readlink(entry / "cwd"))
            except OSError:
                continue
        return found
    try:
        result = subprocess.run(["lsof", "-a", "-d", "cwd", "-c", command, "-Fcn"],
                                capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # lsof exits with 1 when it finds nothing, which is an answer; anything else is not.
    return parse_lsof(result.stdout, command) if result.returncode in (0, 1) else None


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

    A client with a known process name is checked against the process table: if none of its
    processes run in this directory, its registrations are removed. When some run but fewer
    than there are registrations, which ones ended cannot be told, so none is removed: an old
    registration is better than losing a session that is alive. Inside a sandbox the table
    is not trusted and nothing is removed.
    """
    rows = read_registrations(scope)
    directory = scope_path(scope)
    if directory is None or sandboxed():
        return rows
    try:
        known = adapters()
    except UserError as exc:    # what a listing needs is not worth stopping for: say so, and prune nothing
        print(exc, file=sys.stderr)
        return sorted(rows, key=lambda row: row.name)
    kept: list[Registration] = []
    for client in sorted({row.client for row in rows}):
        group = sorted((row for row in rows if row.client == client),
                       key=lambda row: row.registered_ns, reverse=True)
        adapter = known.get(client)
        if adapter is not None and adapter.alive is not None:
            for row in group:
                if adapter.alive(captured_env(adapter, row.env)):
                    kept.append(row)
                else:
                    unlink_if_present(row.path)
            continue
        if adapter is None or not adapter.process:
            kept += group
            continue
        cwds = process_cwds(adapter.process)
        if cwds is None:
            kept += group
            continue
        if any(cwd == directory for cwd in cwds):
            kept += group
            continue
        for stale in group:
            unlink_if_present(stale.path)
    return sorted(kept, key=lambda row: row.name)


def taken_names(scope: str) -> set[str]:
    directory = scope_directory(scope)
    return {path.stem for pattern in ("*.json", "*.reg") for path in directory.glob(pattern)}


def claim_free_name(scope: str, base: str, registration: Callable[[str], JsonObject]) -> str:
    """Register under ``base``, or ``base-2``, ``base-3``... when earlier ones are taken.

    The name is taken by creating its file, which only one process can do, so two sessions
    registering at the same moment never end up with the same name.
    """
    taken = taken_names(scope)
    for number in range(1, 100):
        name = base if number == 1 else f"{base}-{number}"
        if name in taken:
            continue
        if write_metadata(scope_directory(scope) / f"{name}.reg", registration(name), exclusive=True):
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
    if not session:
        hint = f" (set ${adapter.id_env} or pass --session)" if adapter.id_env else " (pass --session)"
        raise UserError(f"register: no session id for {client}{hint}")
    if not SESSION_ID.fullmatch(session):
        raise UserError(f"register: {session!r} is not a valid session id")
    same = [row for row in read_registrations(scope) if row.client == client and row.session == session]
    captured = {key: os.environ[key] for key in adapter.capture_env if key in os.environ}

    def registration(name: str) -> JsonObject:
        return {"name": name, "client": client, "session": session, "registered_ns": time.time_ns(), "env": captured}

    chosen = valid_name(args.chosen, "--name") if args.chosen else None
    if same and chosen in (None, same[0].name):
        name = same[0].name   # this session again: its own file is replaced
        write_metadata(scope_directory(scope) / f"{name}.reg", registration(name))
    elif chosen:
        name = chosen
        if name in taken_names(scope) or not write_metadata(
                scope_directory(scope) / f"{name}.reg", registration(name), exclusive=True):
            raise UserError(f"register: the name {name!r} is taken here")
        if same:
            unlink_if_present(same[0].path)
    else:
        name = claim_free_name(scope, client, registration)
    mark_known(scope, name, "registered")
    remember_scope(scope)
    if args.json:
        print(json.dumps({"kind": "registered", "name": name, "client": client, "session": session}), flush=True)
    else:
        print(f"registered as {name} for {client} session {session}"
              + (f"; use --from {name} when you send" if name != client else ""))
    return 0


def rename(args: argparse.Namespace) -> int:
    """Give a registered session another name. A listener changes its name by joining again."""
    scope = resolve_scope(args.scope)
    old, new = valid_name(args.old, "rename"), valid_name(args.new, "rename")
    found = [row for row in read_registrations(scope) if row.name == old]
    if not found:
        raise UserError(f"rename: {old!r} is not a registered session here"
                        + ("; a listener changes its name by running 'filo join --as NEW'"
                           if metadata_path(scope, old).exists() else ""))
    row = found[0]
    if new in taken_names(scope) or not write_metadata(scope_directory(scope) / f"{new}.reg", {
            "name": new, "client": row.client, "session": row.session, "registered_ns": row.registered_ns,
            "env": row.env}, exclusive=True):
        raise UserError(f"rename: the name {new!r} is taken here")
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
    """Take a registered session or a listener out of the pool."""
    scope = resolve_scope(args.scope)
    rows = read_registrations(scope)
    if args.chosen:
        name = valid_name(args.chosen, "--name")
        found = [row for row in rows if row.name == name]
        if not found:
            try:
                reply = request(socket_path(scope, name), {"op": "quit"})
            except (OSError, ValueError):
                reply = {}
            if reply.get("ok"):
                print(f"{name} is leaving")
                return 0
            raise UserError(f"leave: {name!r} is not a registered session or a listener here; see 'filo who'")
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
    for row in live_registrations(scope):
        if row.name == recipient:
            known = adapters()    # only a registered client needs its adapter, so only it needs the file
            if row.client in known:
                return known[row.client], row.session, captured_env(known[row.client], row.env)
    return None


def deliver_offline(scope: str, recipient: str, sender: str, body: str, message_id: str) -> str:
    """Reach a recipient with no reachable listener: adapter, else spool, else fail."""
    target = adapter_target(scope, recipient)
    if target is not None:
        adapter, session, env = target
        text = compose_text(sender, recipient, body, sender_fields(sender, scope).get("home"), scope)
        where = adapter.deliver(session, text, scope_path(scope) or "", env)
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
    if recipient in adapters():
        raise UserError(f"{recipient!r} has not registered here, and filo does not guess which {recipient} "
                        f"session you mean: run /filo in it, then 'filo who' to see its name")
    raise UserError(f"recipient {recipient!r} is not connected and has not been seen here; "
                     f"run 'filo who' to see who is")


def deliver_message(scope: str, recipient: str, sender: str, body: str, message_id: str) -> str:
    payload: JsonObject = {
        "op": "message", "id": message_id, "from": sender, "message": body, "created_ns": time.time_ns(),
    }
    payload.update(sender_fields(sender, scope))
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

def lock_name(scope: str, name: str) -> int | None:
    """Hold ``name`` for as long as the descriptor that this returns stays open, or None if another
    process holds it. The system takes the lock back when its process ends, however it ends, so it needs no
    probing and cannot be left behind; and only one process can take it, so two that ask for a name at the
    same moment do not both get it."""
    descriptor = os.open(scope_directory(scope) / f"{valid_name(name)}.lock",
                         os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        return None
    return descriptor


def claim_listener_name(scope: str, base: str, exact: bool) -> tuple[str, socket.socket, int]:
    """Bind a socket under ``base``, or ``base-2``... if that name is in use. The third thing returned is the
    descriptor that holds the name: the listener keeps it open for as long as it lives."""
    for number in range(1, 100):
        name = base if number == 1 else f"{base}-{number}"
        socket_file = socket_path(scope, name)
        if len(os.fsencode(str(socket_file))) > SOCKET_PATH_LIMIT:
            raise UserError(f"the socket path is too long for this system ({SOCKET_PATH_LIMIT} bytes at most): "
                            f"{socket_file}; set FILO_DIR to a shorter directory")
        held = lock_name(scope, name)
        metadata = metadata_path(scope, name)
        # What the lock cannot know: a registration, and a listener of a filo that did not take one.
        in_use = held is None or (scope_directory(scope) / f"{name}.reg").exists() or (
            metadata.exists() and listener_is_live(scope, name))
        if in_use:
            if held is not None:
                os.close(held)
            if exact:
                raise UserError(f"participant {name!r} is already connected")
            continue
        assert held is not None
        unlink_if_present(metadata)
        unlink_if_present(socket_file)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(socket_file))
        except OSError:
            server.close()
            os.close(held)
            continue
        return name, server, held
    raise UserError(f"no free name left for {base!r}")


class Listener:
    """A participant that receives messages on its Unix socket and prints them."""

    def __init__(self, scope: str, name: str, server: socket.socket, as_json: bool,
                 parent: int | None = None, preview: int | None = None, detached: bool = False,
                 lock: int | None = None) -> None:
        self.lock = lock             # holds the name; closed, and so given back, when the listener leaves
        self.detached = detached     # nobody reads its output: `filo watch` reads what it received
        self.stopping = False
        self.preview = preview
        self.next_prune = 0.0
        self.sequence = 0    # how many messages this listener has received: their order, which no clock gives
        self.scope = scope
        self.parent = parent
        self.name = name
        self.server = server
        self.as_json = as_json
        self.socket_file = socket_path(scope, name)
        self.metadata = metadata_path(scope, name)
        self.closed = False

    def open(self, shown_scope: str, existing: list[Participant], registered: list[Registration]) -> None:
        """Start accepting, publish this listener and tell the others and the user."""
        os.chmod(self.socket_file, 0o600)
        self.server.listen(32)
        self.server.settimeout(0.5)
        write_metadata(self.metadata, {"name": self.name, "pid": os.getpid(), "started_ns": time.time_ns()})
        mark_known(self.scope, self.name, "listener")
        remember_scope(self.scope)
        if self.detached:
            return    # `detach` tells the others once the command that started this has its answer
        notify(self.scope, self.name, "joined")
        if self.as_json:
            print(json.dumps({"kind": "joined", "scope": shown_scope, "name": self.name,
                              "pid": os.getpid()}), flush=True)
            print(json.dumps({"kind": "participants", "participants": [asdict(row) for row in existing],
                              "registered": registered_rows(registered)}), flush=True)
        else:
            print(f"joined {shown_scope} as {self.name} (pid={os.getpid()})", flush=True)
            print_rows(existing, "already connected:", registered)
            print("listening; press Ctrl-C to leave", flush=True)

    def serve(self) -> None:
        """Deliver spooled messages and answer connections until the parent, if any, is gone."""
        while not self.stopping and (self.parent is None or pid_alive(self.parent)):
            drain_spool(self.scope, self.name, self.print_spooled)
            if time.monotonic() >= self.next_prune:
                prune_inbox(self.scope)
                prune_acks(self.scope)
                self.next_prune = time.monotonic() + 60
            try:
                connection, _ = self.server.accept()
            except TimeoutError:
                continue
            with connection:
                self.handle(connection)

    def print_spooled(self, message: JsonObject) -> None:
        self.receive(message, None)

    def receive(self, message: JsonObject, peer_pid: int | None) -> None:
        if already_received(self.scope, self.name, message):
            return
        self.sequence += 1
        keep_received(self.scope, self.name, message, peer_pid, (os.getpid(), self.sequence))
        self.emit(message_event(self.name, message, peer_pid, self.preview, self.scope))

    def emit(self, payload: JsonObject) -> None:
        if not self.detached:
            emit_event(payload, self.as_json)

    def handle(self, connection: socket.socket) -> None:
        connection.settimeout(SERVER_TIMEOUT)
        peer_pid, peer_uid = peer_credentials(connection)
        try:
            if peer_uid is not None and peer_uid != os.getuid():
                raise ProtocolError("peer belongs to another user")
            response = self.respond(read_frame(connection, SERVER_TIMEOUT), peer_pid)
        except (OSError, ValueError) as exc:
            response = {"ok": False, "error": str(exc)}
        try:
            connection.settimeout(SERVER_TIMEOUT)    # what is left of the time for reading is not for this
            connection.sendall(frame(response))
        except OSError:
            pass

    def respond(self, message: JsonObject, peer_pid: int | None) -> JsonObject:
        operation = message.get("op")
        if operation == "info":
            return {"ok": True, "name": self.name, "pid": os.getpid()}
        if operation == "message":
            self.receive(message, peer_pid)
            identifier = message.get("id")    # a name of a file: only what filo makes is one
            if isinstance(identifier, str) and MESSAGE_ID.fullmatch(identifier):
                write_ack(self.scope, identifier, self.name, "delivered directly")
            return {"ok": True}
        if operation == "quit":
            self.stopping = True
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
        # Gone first, then said: the others that leave at the same moment find nobody here to wait for,
        # and so do not each wait out the time of a request that nobody answers.
        self.server.close()
        unlink_if_present(self.socket_file)
        unlink_if_present(self.metadata)
        notify(self.scope, self.name, "left")
        if self.lock is not None:
            os.close(self.lock)


def detach(listener: Listener, shown_scope: str, existing: list[Participant],
           registered: list[Registration], as_json: bool) -> int:
    """Let the listener go on in a process of its own, which ends with its parent, and say who it is.

    The name and the socket are claimed first, so a failure is reported here. The new process
    publishes the listener and tells this one, which prints what `join` would have printed."""
    sys.stdout.flush()
    sys.stderr.flush()
    ready_read, ready_write = os.pipe()
    child = os.fork()
    if child == 0:
        status = 1
        try:
            os.setsid()
            null = os.open(os.devnull, os.O_RDWR)
            for descriptor in (0, 1, 2):
                os.dup2(null, descriptor)
            keep = {listener.server.fileno(), ready_write}
            if listener.lock is not None:
                keep.add(listener.lock)
            for descriptor in range(3, 256):
                if descriptor not in keep:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            listener.open(shown_scope, existing, registered)
            os.write(ready_write, b"1")
            os.close(ready_write)
            notify(listener.scope, listener.name, "joined")
            listener.serve()
            status = 0
        finally:
            listener.close()
            os._exit(status)
    os.close(ready_write)
    listener.server.close()
    if listener.lock is not None:
        os.close(listener.lock)    # the new process has its own: the name stays held until it ends
    listener.closed = True    # the files belong to the new process now: this one must never remove them
    with os.fdopen(ready_read, "rb") as pipe:
        ready, _, _ = select.select([pipe], [], [], 10)
        started = bool(ready) and pipe.read(1) == b"1"
    if not started:
        raise UserError("join: the listener did not start")
    if as_json:
        print(json.dumps({"kind": "joined", "scope": shown_scope, "name": listener.name, "pid": child,
                          "detached": True}), flush=True)
        print(json.dumps({"kind": "participants", "participants": [asdict(row) for row in existing],
                          "registered": registered_rows(registered)}), flush=True)
    else:
        print(f"joined {shown_scope} as {listener.name} (pid={child}); it leaves when process "
              f"{listener.parent} ends, or with `filo leave --name {listener.name}`", flush=True)
        print_rows(existing, "already connected:", registered)
    return 0


def join(args: argparse.Namespace) -> int:
    if args.preview is not None and args.preview < MIN_PREVIEW:
        raise UserError(f"join: --preview must be at least {MIN_PREVIEW}")
    if args.parent is not None and not 0 < args.parent < 2**31:
        raise UserError("join: --parent must be the id of a process, above 0")
    if args.detach:
        if args.parent is None:
            raise UserError("join: --detach needs --parent PID, the process whose end ends the listener")
        if not pid_alive(args.parent):
            raise UserError(f"join: process {args.parent} is not running")
    scope = canonical_scope(args.scope)
    existing = participant_rows(scope)
    registered = live_registrations(scope)    # before the socket exists: nothing here may leave one behind
    name, server, lock = claim_listener_name(scope, valid_name(args.name, "--as"), args.exact)
    listener = Listener(scope, name, server, args.json, args.parent, args.preview, args.detach, lock)

    def stop(_signum: int, _frame: object) -> None:
        listener.close()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    if args.detach:
        return detach(listener, args.scope or str(Path.cwd()), existing, registered, args.json)
    try:
        listener.open(args.scope or str(Path.cwd()), existing, registered)
        listener.serve()
    finally:
        listener.close()
    return 0


# --- command line ------------------------------------------------------------

def releases_url() -> str:
    """Where releases are looked up. FILO_RELEASES_URL is for a mirror and for the tests."""
    url = os.environ.get("FILO_RELEASES_URL", RELEASES_URL).rstrip("/")
    if not secure_url(url):
        raise UserError(f"upgrade: FILO_RELEASES_URL must be an https:// URL, not {url}")
    return url


def secure_url(url: str) -> bool:
    """Whether ``url`` is https, or plain http to this machine on a port (for a mirror and the tests).
    It is the host that counts, not how the address begins: ``http://127.0.0.1:80@example.org/`` is
    example.org, and an address that names a user is never accepted."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:    # a port that is not a number, an unclosed bracket
        return False
    if parts.username is not None or parts.password is not None or not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost") and port is not None


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


class KeepMethod(urllib.request.HTTPRedirectHandler):
    """Follow a redirect with the method of the request. Before Python 3.13 a HEAD became a GET on the way,
    and the check for a new version then read the page of the release."""

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> urllib.request.Request | None:
        followed = super().redirect_request(req, fp, code, msg, headers, newurl)
        if followed is not None and req.get_method() == "HEAD":
            followed.method = "HEAD"
        return followed


def fetch_url(url: str, method: str = "GET", timeout: float = 10.0) -> tuple[str, bytes]:
    """Fetch a URL and return the address it ended at and up to a megabyte of its body."""
    try:
        with urllib.request.build_opener(KeepMethod).open(urllib.request.Request(url, method=method),
                                                          timeout=timeout) as response:
            final = cast(str, response.geturl())
            if not secure_url(final):
                raise UserError(f"upgrade: {url} led to {final}, which is not https")
            return final, cast(bytes, response.read(1_000_000))
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise UserError(f"upgrade: could not fetch {url}: {exc}") from exc


def latest_version(timeout: float = 10.0) -> str:
    """The version of the newest release: the address /releases/latest redirects to."""
    final, _ = fetch_url(f"{releases_url()}/latest", method="HEAD", timeout=timeout)
    found = RELEASE_TAG.search(final)
    if found is None:
        raise UserError(f"upgrade: cannot read a version from {final}")
    return found.group(1)


def install_directory() -> Path:
    """Where this filo lives, as the links made by the installer name it (links not resolved)."""
    script = Path(os.path.abspath(__file__))
    for _ in range(16):    # a link to a link, as when the installer's link is itself linked from a bin directory
        if not script.is_symlink():
            break
        script = Path(os.path.abspath(script.parent / os.readlink(script)))
    return script.parent


def upgrade_command(args: argparse.Namespace) -> int:
    """Say whether a newer release exists and, unless --check, install it with that release's get.sh."""
    latest = latest_version()
    if version_tuple(latest) <= version_tuple(__version__):
        print(f"filo {__version__} is the latest version.")
        return 0
    print(f"filo {__version__} is installed; {latest} is available: {releases_url()}/tag/v{latest}")
    if args.check:
        print("Run `filo upgrade` to install it.")
        return 0
    directory = install_directory()
    if not (directory / RELEASE_MARKER).is_file():
        if (directory / ".git").exists():
            print(f"This is a checkout. Update it with: git -C {directory} pull")
            return 0
        raise UserError("upgrade: this filo was not installed by get.sh, so it cannot upgrade itself")
    _, body = fetch_url(f"{releases_url()}/download/v{latest}/get.sh")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UserError("upgrade: the get.sh of that release is not text") from exc
    stamped = STAMPED_VERSION.search(text)
    if stamped is None or stamped.group(1) != latest:
        raise UserError(f"upgrade: the get.sh of that release is not stamped for {latest}; not running it")
    with tempfile.TemporaryDirectory(prefix="filo-upgrade.") as scratch:
        script = Path(scratch) / "get.sh"
        script.write_text(text, encoding="utf-8")
        env = {**os.environ, "FILO_HOME": str(directory)}
        print()
        planned = subprocess.run(["sh", str(script), "--dry-run"], env=env, check=False).returncode
        if planned != 0:
            return planned
        if not args.yes:
            if not sys.stdin.isatty():
                raise UserError("upgrade: this is not a terminal; run it again with --yes to go ahead")
            try:
                answer = input("\nUpgrade now? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("Nothing changed.")
                return 0
        return subprocess.run(["sh", str(script)], env=env, check=False).returncode


def update_notice() -> None:
    """With FILO_CHECK_UPDATES=1, say on stderr when a newer release exists. At most one look a day."""
    if os.environ.get("FILO_CHECK_UPDATES") != "1":
        return
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / APP / "latest-version"
    latest = ""
    try:
        if time.time() - cache.stat().st_mtime < UPDATE_CHECK_TTL:
            latest = cache.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if not VERSION.fullmatch(latest):
        try:
            latest = latest_version(timeout=2.0)
        except UserError:
            latest = __version__   # offline: ask again tomorrow, not at every command
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(latest + "\n", encoding="utf-8")
        except OSError:
            pass
    if version_tuple(latest) > version_tuple(__version__):
        print(f"filo {latest} is available (this is {__version__}): run `filo upgrade`", file=sys.stderr)


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
            "  filo join --as claude --detach --parent $CLAUDE_PID    a listener that ends with its parent\n"
            "  filo watch --as claude            its messages, until it ends; start it again at any time\n"
            "  filo leave                           take this registered session out of the pool\n"
            "  filo profiles                        the roles you can join as: /filo manager\n"
            "  filo profile manager                 print what a manager is told to do\n"
            "  filo who\n"
            "  filo who --all                    every scope that has someone in it\n"
            "  filo send --to claude --from codex --message 'review this'\n"
            "  filo send --to codex --from claude --stdin < message.txt\n"
            "  filo status ID                    was a message delivered?\n"
            "  filo show ID                      the whole text of a message that was shortened\n"
            "  filo upgrade --check              is there a newer release?\n"
            "\nThe default scope is the current directory. Set FILO_DIR to change the local runtime root."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{APP} {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    join_parser = sub.add_parser("join", help="join a scope and listen until interrupted")
    scope_argument(join_parser)
    join_parser.add_argument("--as", dest="name", required=True,
                             help="your participant name; if taken a numeric suffix is added")
    join_parser.add_argument("--exact", action="store_true", help="fail instead of adding a suffix")
    join_parser.add_argument("--parent", type=int, metavar="PID",
                             help="leave when this process ends (for a plugin that starts the listener)")
    join_parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    join_parser.add_argument("--preview", type=int, metavar="N",
                             help=f"with --json: cut an event longer than N characters (at least {MIN_PREVIEW}) "
                                  "and say so; `filo show ID` prints the whole message")
    join_parser.add_argument("--detach", action="store_true",
                             help="go on in a process of its own and return: for a session whose output is not "
                                  "its listener (needs --parent; read the messages with `filo watch`)")

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

    upgrade_parser = sub.add_parser("upgrade", help="install the newest release, if this filo came from get.sh")
    upgrade_parser.add_argument("--check", action="store_true", help="only say whether a newer release exists")
    upgrade_parser.add_argument("-y", "--yes", action="store_true", help="do not ask before upgrading")

    who_parser = sub.add_parser("who", help="list participants")
    scope_argument(who_parser)
    who_parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    who_parser.add_argument("--all", action="store_true",
                            help="every scope that has someone in it, not only this one")

    send_parser = sub.add_parser("send", help="send one message")
    scope_argument(send_parser)
    send_parser.add_argument("--to", required=True, help="recipient participant name")
    send_parser.add_argument("--from", dest="sender", default="anonymous", help="sender participant name")
    source = send_parser.add_mutually_exclusive_group()
    source.add_argument("--message", help="message text")
    source.add_argument("--stdin", action="store_true", help="read the entire message from standard input")
    send_parser.add_argument("text", nargs="?", help="message text, as an alternative to --message")

    watch_parser = sub.add_parser(
        "watch", help="print the messages that a detached listener receives, as JSON lines, until it ends")
    scope_argument(watch_parser)
    watch_parser.add_argument("--as", dest="name", required=True, help="the name of the detached listener")
    watch_parser.add_argument("--json", action="store_true",
                              help="accepted for symmetry with join: watch always prints JSON lines")
    watch_parser.add_argument("--preview", type=int, metavar="N",
                              help=f"cut an event longer than N characters (at least {MIN_PREVIEW}) and say so")

    show_parser = sub.add_parser("show", help="print the whole body of a message this scope received")
    scope_argument(show_parser)
    show_parser.add_argument("id", help="the id in the event, or in what `filo show` was told to print")
    show_parser.add_argument("--json", action="store_true", help="print the whole record as JSON")

    status_parser = sub.add_parser("status", help="show whether a sent message was delivered")
    scope_argument(status_parser)
    status_parser.add_argument("id", help="the id printed by send")
    return parser


def who_all(args: argparse.Namespace) -> int:
    """List every scope that has someone in it, with who they are."""
    found: list[tuple[str, list[Participant], list[Registration]]] = []
    for scope in known_scopes():
        participants, registered = participant_rows(scope), live_registrations(scope)
        if participants or registered:
            found.append((display_scope(scope), participants, registered))
    if args.json:
        print(json.dumps({"scopes": [
            {"scope": shown, "participants": [asdict(row) for row in participants],
             "registered": registered_rows(registered)} for shown, participants, registered in found]}), flush=True)
        return 0
    if not found:
        print("nobody is connected in any scope")
    for shown, participants, registered in found:
        print_rows(participants, f"{shown}:", registered)
    update_notice()
    return 0


def who_command(args: argparse.Namespace, scope: str | None) -> int:
    if args.all:
        return who_all(args)
    assert scope is not None
    participants = participant_rows(scope)
    registered = live_registrations(scope)
    shown = args.scope or str(Path.cwd())
    if scope != canonical_scope(args.scope):    # the session is in another scope than the directory
        shown = display_scope(scope)
    if args.json:
        print(json.dumps({
            "scope": shown,
            "participants": [asdict(row) for row in participants],
            "registered": registered_rows(registered),
        }), flush=True)
        return 0
    print_rows(participants, f"connected to {shown}:", registered)
    update_notice()
    return 0


def watch_command(args: argparse.Namespace, scope: str) -> int:
    """Print, one JSON line each, the messages that a detached listener receives, and the end of the
    listener. It reads what the listener keeps, so it can be stopped and started again at any time:
    nothing changes for the listener and the others, and what came in between is printed at once."""
    name = valid_name(args.name, "--as")
    if args.preview is not None and args.preview < MIN_PREVIEW:
        raise UserError(f"watch: --preview must be at least {MIN_PREVIEW}")
    try:
        info = read_object(metadata_path(scope, name))
        pid, started = int_field(info, "pid"), int_field(info, "started_ns", 0)
    except (OSError, ValueError, TypeError, KeyError):
        raise UserError(f"watch: nobody called {name!r} is listening here; "
                        f"start it with `filo join --as {name} --detach --parent PID`") from None
    inbox = scope_directory(scope) / "inbox" / name
    cursor = inbox / ".cursor"
    last, last_seq = watch_cursor(cursor, inbox, pid)
    looked_at: set[str] = set()    # what was read: a record is read once, and never again
    next_check = 0.0
    while True:
        waiting: list[tuple[int, str, JsonObject]] = []
        for path in inbox.glob("*.json"):
            if path.stem in looked_at:
                continue
            try:
                record = read_object(path)
                received = int_field(record, "received_ns", 0)
            except (OSError, ValueError, TypeError):
                continue
            looked_at.add(path.stem)
            if "seq" in record:
                # Numbered by the listener that received it, so the order and what is new need no clock.
                if record.get("listener") != pid or int_field(record, "seq", 0) <= last_seq:
                    continue
                waiting.append((int_field(record, "seq", 0), path.stem, record))
            elif received > last and received >= started:    # from a filo that numbered nothing: by the clock
                waiting.append((received, path.stem, record))
        for _, _, record in sorted(waiting, key=lambda item: item[:2]):
            peer = record.get("peer_pid")
            print(json.dumps(message_event(name, record, peer if isinstance(peer, int) else None,
                                           args.preview, scope), ensure_ascii=False), flush=True)
            last = max(last, int_field(record, "received_ns", 0))
            last_seq = max(last_seq, int_field(record, "seq", 0))
            write_metadata(cursor, {"pid": pid, "last_ns": last, "last_seq": last_seq})
        if time.monotonic() >= next_check:
            if not listener_present(scope, name, pid):
                print(json.dumps({"kind": "ended", "name": name, "reason": "the listener is gone"}), flush=True)
                return 0
            next_check = time.monotonic() + 2
        time.sleep(0.25)


def watch_cursor(cursor: Path, inbox: Path, pid: int) -> tuple[int, int]:
    """Where the last watch of this listener stopped: when the last message that it printed was received
    (for records that the listener did not number) and the number of the last one that it printed.

    The cursor says when and which number, not which id: an id is made by the sender before it delivers, so
    a message that was slow, or that came through the spool, can have an older id than one that arrived
    before it. A cursor written by an older filo holds that id, and when it was received is in the inbox."""
    try:
        saved = read_object(cursor)
        if saved.get("pid") != pid:
            return 0, 0
        if "last_ns" in saved and "last_seq" in saved:
            return int_field(saved, "last_ns", 0), int_field(saved, "last_seq", 0)
        if "last_ns" in saved:
            when = int_field(saved, "last_ns", 0)
        else:
            when = int_field(read_object(inbox / f"{saved.get('last', '')}.json"), "received_ns", 0)
        return when, last_numbered(inbox, pid, when)    # a cursor of a filo before the numbers: find where it was
    except (OSError, ValueError, TypeError):
        return 0, 0


def last_numbered(inbox: Path, pid: int, until_ns: int) -> int:
    """The number of the last message that the listener ``pid`` had received at ``until_ns``."""
    found = 0
    for path in inbox.glob("*.json"):
        try:
            record = read_object(path)
        except (OSError, ValueError):
            continue
        if record.get("listener") == pid and int_field(record, "received_ns", 0) <= until_ns:
            found = max(found, int_field(record, "seq", 0))
    return found


def show_command(args: argparse.Namespace, scope: str) -> int:
    """Print the whole body of a message that a listener of this scope received."""
    if not MESSAGE_ID.fullmatch(args.id):
        raise UserError("show: not a message id")
    for path in sorted(scope_directory(scope).glob(f"inbox/*/{args.id}.json")):
        try:
            record = read_object(path)
        except (OSError, ValueError):
            continue
        if args.json:
            print(json.dumps(record, ensure_ascii=False))
        else:
            print(record.get("message", ""))
        return 0
    raise UserError(f"show: no message {args.id} was received in this scope, or it is more than "
                    f"{int(INBOX_TTL // 3600)} hours old")


def message_body(args: argparse.Namespace) -> str:
    """The text to send, from --stdin, --message or the positional argument."""
    if args.stdin + (args.message is not None) + (args.text is not None) > 1:
        raise UserError("send: choose one of --stdin, --message or a positional message")
    if args.stdin:
        try:
            body = sys.stdin.buffer.read().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UserError("send: the message on standard input is not UTF-8 text") from exc
    else:
        body = args.message if args.message is not None else (args.text or "")
    if not body:
        raise UserError("send: refusing an empty message")
    try:
        size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    except UnicodeEncodeError as exc:    # argv holds the bytes that are not text as surrogates
        raise UserError("send: the message has bytes that are not UTF-8 text") from exc
    if size > MAX_BODY:
        raise UserError(f"send: the message is {size:,} bytes and filo carries {MAX_BODY:,} at most; "
                        f"send a file name instead, or split it")
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
        if args.command == "upgrade":
            return upgrade_command(args)
        if args.command == "who":
            if args.all and args.scope:
                raise UserError("who: --all lists every scope, so it does not take --scope")
            return who_command(args, None if args.all else resolve_scope(args.scope))
        if args.command == "watch":
            return watch_command(args, canonical_scope(args.scope))
        scope = resolve_scope(args.scope, valid_name(args.sender, "--from") if args.command == "send" else None)
        if args.command == "show":
            return show_command(args, scope)
        if args.command == "status":
            print(message_status(scope, args.id))
            return 0
        return send_command(args, scope)
    except UserError as exc:
        print(exc, file=sys.stderr)
        return 1
    except OSError as exc:    # the file system or a socket said no: that is a message, not a traceback
        print(f"{APP}: {exc.strerror or exc}" + (f": {exc.filename}" if exc.filename else ""), file=sys.stderr)
        return 1
    except KeyboardInterrupt:    # Ctrl-C ends a command that waits (watch, send --stdin, a question), quietly
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
