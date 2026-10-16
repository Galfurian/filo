from __future__ import annotations

import fcntl
import hashlib
import http.server
import importlib.util
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest import mock

# macOS temporary directories are too long for a Unix socket path (104 bytes at most).
if sys.platform == "darwin":
    tempfile.tempdir = "/tmp"

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "filo.py"
RELEASE_MARKER = ".filo-release"


class MessagePassingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = tempfile.TemporaryDirectory()
        self.scope = tempfile.TemporaryDirectory()
        self.environment = {**os.environ, "FILO_DIR": self.runtime.name}
        self.processes: list[subprocess.Popen[str]] = []

    def tearDown(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        self.runtime.cleanup()
        self.scope.cleanup()

    def command(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.scope.name,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def start_listener(self, name: str) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "join", "--as", name, "--json"],
            cwd=self.scope.name,
            env=self.environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.processes.append(process)
        return process

    def wait_for(self, name: str) -> None:
        for _ in range(50):
            if name in self.command("who", "--json").stdout:
                return
            time.sleep(0.02)
        self.fail(f"listener {name} did not join")

    def stop(self, process: subprocess.Popen[str]) -> None:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=2)

    def test_direct_message_and_presence_are_delivered(self) -> None:
        first = self.start_listener("first")
        self.wait_for("first")
        second = self.start_listener("second")
        self.wait_for("second")
        sent = self.command("send", "--to", "first", "--from", "second", "hello")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        time.sleep(0.05)
        self.stop(first)
        output = first.stdout.read() if first.stdout is not None else ""
        self.assertIn('"kind": "presence"', output)
        self.assertIn('"event": "joined"', output)
        self.assertIn('"kind": "message"', output)
        self.assertIn("hello", output)
        self.stop(second)

    def test_a_killed_listener_is_not_trusted_and_its_name_can_be_reused(self) -> None:
        self.assertEqual(self.command("send", "--to", "victim", "--message", "x").returncode, 1)
        listener = self.start_listener("victim")
        self.wait_for("victim")
        listener.kill()
        listener.wait(timeout=2)
        sent = self.command("send", "--to", "victim", "--from", "other", "while you were away")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertNotIn("shortly", sent.stdout)
        self.assertIn("offline", sent.stdout)
        again = self.start_listener("victim")
        self.wait_for("victim")
        time.sleep(0.7)
        self.stop(again)
        output = again.stdout.read() if again.stdout is not None else ""
        self.assertIn('"name": "victim"', output)
        self.assertIn("while you were away", output)

    def test_stale_participant_is_removed_and_offline_send_fails(self) -> None:
        listener = self.start_listener("gone")
        self.wait_for("gone")
        self.stop(listener)
        rows = self.command("who", "--json")
        self.assertEqual(json.loads(rows.stdout)["participants"], [])
        failed = self.command("send", "--to", "never-seen", "--message", "hello")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("not connected", failed.stderr)
        queued = self.command("send", "--to", "gone", "--message", "hello")
        self.assertEqual(queued.returncode, 0, queued.stderr)
        self.assertIn("offline", queued.stdout)

    def test_stdin_and_keyword_scopes(self) -> None:
        listener = subprocess.Popen(
            [sys.executable, str(SCRIPT), "join", "--scope", "shared-key", "--as", "target", "--json"],
            cwd=self.scope.name,
            env=self.environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.processes.append(listener)
        for _ in range(50):
            if "target" in self.command("who", "--scope", "shared-key").stdout:
                break
            time.sleep(0.02)
        sent = subprocess.run(
            [sys.executable, str(SCRIPT), "send", "--scope", "shared-key", "--to", "target", "--stdin"],
            cwd=self.scope.name,
            env=self.environment,
            input="a long\nmessage",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.stop(listener)


class CodexQueueRouteTests(unittest.TestCase):
    """`send --to codex` falls back to `codex queue` when no socket listens."""

    def setUp(self) -> None:
        self.base = tempfile.TemporaryDirectory()
        base = Path(self.base.name)
        self.scope = base / "project"
        self.scope.mkdir()
        self.runtime = base / "runtime"
        self.bin = base / "bin"
        self.bin.mkdir()
        self.record = base / "queue-args.json"
        fake = self.bin / "codex"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"open({str(self.record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            "print('Queued message m for thread ' + sys.argv[sys.argv.index('--thread') + 1])\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        self.live_dir = base / "live"
        self.live_dir.mkdir()
        self.live: list[subprocess.Popen[bytes]] = []
        self.environment = {
            **os.environ,
            "FILO_DIR": str(self.runtime),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
        }

    def tearDown(self) -> None:
        for process in self.live:
            process.terminate()
            process.wait(timeout=5)
        self.base.cleanup()

    def start_codex(self, count: int = 1, cwd: Path | None = None) -> None:
        """Run ``count`` processes that look like Codex sessions in the scope."""
        # A copy of a plain binary, so that the process is called "codex" on every system;
        # a link to Python would be started again under another name on macOS.
        program = self.live_dir / "codex"
        if not program.exists():
            sleep = shutil.which("sleep")
            assert sleep is not None
            shutil.copy(sleep, program)
        for _ in range(count):
            self.live.append(subprocess.Popen([str(program), "120"], cwd=cwd or self.scope))
        time.sleep(0.2)

    def send(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "send", *args],
            cwd=self.scope, env=self.environment, text=True, capture_output=True, check=False,
        )

    def test_a_codex_that_has_not_registered_is_not_guessed(self) -> None:
        sent = self.send("--to", "codex", "--from", "claude", "hello")
        self.assertEqual(sent.returncode, 1)
        self.assertIn("has not registered here", sent.stderr)
        self.assertIn("does not guess", sent.stderr)
        self.assertFalse(self.record.exists())

    def test_other_recipients_do_not_use_codex(self) -> None:
        sent = self.send("--to", "claude", "--from", "codex", "hello")
        self.assertNotEqual(sent.returncode, 0)
        self.assertIn("not connected", sent.stderr)
        self.assertFalse(self.record.exists())


class ReplyHintTests(unittest.TestCase):
    def test_monitor_lines_carry_a_reply_command(self) -> None:
        runtime = tempfile.TemporaryDirectory()
        scope = tempfile.TemporaryDirectory()
        environment = {**os.environ, "FILO_DIR": runtime.name}
        listener = subprocess.Popen(
            [sys.executable, str(SCRIPT), "join", "--as", "claude", "--json"],
            cwd=scope.name, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            assert listener.stdout is not None
            listener.stdout.readline()  # joined
            listener.stdout.readline()  # participants
            sent = subprocess.run(
                [sys.executable, str(SCRIPT), "send", "--to", "claude", "--from", "codex", "hi"],
                cwd=scope.name, env=environment, text=True, capture_output=True, check=False,
            )
            self.assertEqual(sent.returncode, 0, sent.stderr)
            line = json.loads(listener.stdout.readline())
            self.assertEqual(line["kind"], "message")
            self.assertEqual(line["from"], "codex")
            self.assertIn("filo send --to codex --from claude", line["reply"])
        finally:
            listener.terminate()
            listener.wait(timeout=5)
            if listener.stdout is not None:
                listener.stdout.close()
            runtime.cleanup()
            scope.cleanup()


class SandboxedSenderTests(unittest.TestCase):
    """A sender that may not connect to the socket (Codex sandbox) drops a file."""

    def setUp(self) -> None:
        self.runtime = tempfile.TemporaryDirectory()
        self.scope = tempfile.TemporaryDirectory()
        self.environment = {**os.environ, "FILO_DIR": self.runtime.name}
        self.listener = subprocess.Popen(
            [sys.executable, str(SCRIPT), "join", "--as", "claude", "--json"],
            cwd=self.scope.name, env=self.environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        assert self.listener.stdout is not None
        self.listener.stdout.readline()  # joined
        self.listener.stdout.readline()  # participants
        self.sockets = list(Path(self.runtime.name).glob("*/claude.sock"))
        self.assertEqual(len(self.sockets), 1)
        self.sockets[0].chmod(0)  # connect() fails with PermissionError

    def tearDown(self) -> None:
        self.sockets[0].chmod(0o600)
        self.listener.terminate()
        self.listener.wait(timeout=5)
        if self.listener.stdout is not None:
            self.listener.stdout.close()
        self.runtime.cleanup()
        self.scope.cleanup()

    def run_msg(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.scope.name, env=self.environment, text=True, capture_output=True, check=False,
        )

    def test_message_is_left_in_the_spool_and_delivered(self) -> None:
        sent = self.run_msg("send", "--to", "claude", "--from", "codex", "14")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertIn("cannot confirm", sent.stdout)
        assert self.listener.stdout is not None
        line = json.loads(self.listener.stdout.readline())
        self.assertEqual((line["kind"], line["from"], line["message"]), ("message", "codex", "14"))
        self.assertIn("filo send --to codex --from claude", line["reply"])
        self.assertEqual(list(Path(self.runtime.name).glob("*/spool/claude/*.msg")), [])

    def test_unreachable_recipient_is_not_spooled(self) -> None:
        sent = self.run_msg("send", "--to", "nobody", "--from", "codex", "hi")
        self.assertNotEqual(sent.returncode, 0)
        self.assertEqual(list(Path(self.runtime.name).glob("*/spool/*/*.msg")), [])

    def test_who_keeps_a_peer_it_cannot_probe(self) -> None:
        who = self.run_msg("who", "--json")
        self.assertEqual([row["name"] for row in json.loads(who.stdout)["participants"]], ["claude"])
        self.assertEqual(len(list(Path(self.runtime.name).glob("*/claude.json"))), 1)
        self.assertEqual(len(list(Path(self.runtime.name).glob("*/claude.sock"))), 1)


class RegistrationTests(CodexQueueRouteTests):
    """`register --as codex` pins the thread so nothing has to be guessed."""

    def register(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "register", *args],
            cwd=self.scope, env={**self.environment, **(env or {})},
            text=True, capture_output=True, check=False,
        )

    def test_a_registered_thread_receives_the_message(self) -> None:
        self.start_codex()
        registered = self.register("--as", "codex", env={"CODEX_THREAD_ID": "01a125b1-5fb3-70a3-81ad-3a51833def23"})
        self.assertEqual(registered.returncode, 0, registered.stderr)
        sent = self.send("--to", "codex", "--from", "claude", "hello")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        args = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(args[:3], ["queue", "--thread", "01a125b1-5fb3-70a3-81ad-3a51833def23"])

    def test_explicit_thread_option_and_who_lists_the_registration(self) -> None:
        self.start_codex()
        self.assertEqual(self.register("--as", "codex", "--session", "0123abcd-4567").returncode, 0)
        who = subprocess.run(
            [sys.executable, str(SCRIPT), "who", "--json"],
            cwd=self.scope, env=self.environment, text=True, capture_output=True, check=False,
        )
        registered = json.loads(who.stdout)["registered"]
        self.assertEqual([(row["name"], row["session"]) for row in registered], [("codex", "0123abcd-4567")])

    def test_registering_without_a_thread_id_is_refused_not_guessed(self) -> None:
        self.environment = {key: value for key, value in self.environment.items() if key != "CODEX_THREAD_ID"}
        refused = self.register("--as", "codex")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("CODEX_THREAD_ID", refused.stderr)
        self.assertEqual(list(self.runtime.glob("*/*.reg")), [])

    def test_rejects_other_clients_and_unsafe_thread_ids(self) -> None:
        self.assertNotEqual(self.register("--as", "claude", "--session", "abcd1234").returncode, 0)
        unsafe = self.register("--as", "codex", "--session=--dangerous")
        self.assertNotEqual(unsafe.returncode, 0)
        self.assertIn("session id", unsafe.stderr)


class Lines:
    """Line-by-line reads with a timeout from a child's stdout (bytes mode)."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stdout is not None
        self.fd = process.stdout.fileno()
        self.buffer = b""

    def next(self, timeout: float = 5.0) -> dict[str, object] | None:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                return None
            chunk = os.read(self.fd, 65536)
            if not chunk:
                return None
            self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b"\n")
        return json.loads(line)

    def message(self, timeout: float = 5.0) -> dict[str, object] | None:
        """Next event of kind "message", skipping presence notices."""
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            event = self.next(remaining)
            if event is None:
                return None
            if event.get("kind") == "message":
                return event
        return None


def make_recorder(path: Path, record: Path) -> Path:
    """An executable that stores its arguments as JSON, standing in for a client CLI."""
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


class RuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.TemporaryDirectory()
        self.root = Path(self.base.name)
        self.runtime = self.root / "runtime"
        self.scope = self.root / "project"
        self.scope.mkdir()
        self.environment = {**os.environ, "FILO_DIR": str(self.runtime)}
        self.environment.pop("CODEX_SANDBOX_NETWORK_DISABLED", None)
        self.children: list[subprocess.Popen[bytes]] = []

    def tearDown(self) -> None:
        for child in self.children:
            if child.poll() is None:
                child.send_signal(signal.SIGCONT)
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
            if child.stdout is not None:
                child.stdout.close()
        self.base.cleanup()

    def msg(self, *args: str, scope: Path | None = None, env: dict[str, str] | None = None,
            stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=scope or self.scope, env={**self.environment, **(env or {})},
            input=stdin, text=True, capture_output=True, check=False,
        )

    def listen(self, name: str, *extra: str, scope: Path | None = None,
               env: dict[str, str] | None = None) -> tuple[subprocess.Popen[bytes], Lines, dict[str, object]]:
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "join", "--as", name, "--json", *extra],
            cwd=scope or self.scope, env={**self.environment, **(env or {})},
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.children.append(process)
        lines = Lines(process)
        joined = lines.next()
        participants = lines.next()
        self.assertIsNotNone(joined, "listener did not start")
        self.assertIsNotNone(participants)
        assert joined is not None
        return process, lines, joined


class NamingTests(RuntimeCase):
    def test_a_second_listener_with_the_same_name_gets_a_suffix(self) -> None:
        _, first_lines, first = self.listen("claude")
        _, second_lines, second = self.listen("claude")
        self.assertEqual((first["name"], second["name"]), ("claude", "claude-2"))
        who = json.loads(self.msg("who", "--json").stdout)
        self.assertEqual(sorted(row["name"] for row in who["participants"]), ["claude", "claude-2"])
        sent = self.msg("send", "--to", "claude-2", "--from", "codex", "for the second")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        message = second_lines.message()
        assert message is not None
        self.assertEqual(message["message"], "for the second")
        self.assertIn("--from claude-2", str(message["reply"]))
        self.assertIsNone(first_lines.message(0.5))

    def test_exact_refuses_a_taken_name(self) -> None:
        self.listen("claude")
        refused = self.msg("join", "--as", "claude", "--exact", "--json")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("already connected", refused.stderr)

    def test_a_name_too_long_for_a_file_is_refused_with_a_message(self) -> None:
        name = "a" * 300
        for command in (("send", "--to", name, "--message", "hi"),
                        ("send", "--to", "x", "--from", name, "--message", "hi"),
                        ("register", "--as", "codex", "--session", "abcdefgh", "--name", name),
                        ("join", "--as", name, "--json"),
                        ("leave", "--name", name)):
            failed = self.msg(*command)
            self.assertEqual(failed.returncode, 1, (command, failed.stderr))
            self.assertNotIn("Traceback", failed.stderr)
            self.assertIn("at most", failed.stderr)
        self.assertEqual(self.msg("join", "--as", "a" * 40, "--detach", "--parent", str(os.getpid())).returncode, 0)


class ScopeTests(RuntimeCase):
    def test_sessions_in_different_directories_do_not_see_each_other(self) -> None:
        other = self.root / "other"
        other.mkdir()
        _, lines_here, here = self.listen("claude")
        _, lines_there, there = self.listen("claude", scope=other)
        self.assertEqual((here["name"], there["name"]), ("claude", "claude"))
        self.assertEqual(self.msg("send", "--to", "claude", "--from", "x", "only here").returncode, 0)
        message = lines_here.message()
        assert message is not None
        self.assertEqual(message["message"], "only here")
        self.assertIsNone(lines_there.message(0.7))
        who_there = json.loads(self.msg("who", "--json", scope=other).stdout)
        self.assertEqual([row["name"] for row in who_there["participants"]], ["claude"])

    def test_a_directory_that_is_gone_or_a_scope_that_is_absurd_gives_a_message_or_a_keyword(self) -> None:
        gone = self.root / "gone"
        gone.mkdir()
        code = ("import os, runpy, sys\nos.chdir(sys.argv[1])\nos.rmdir(sys.argv[1])\n"
                f"sys.argv = ['filo', 'who']\nrunpy.run_path({str(SCRIPT)!r}, run_name='__main__')")
        failed = subprocess.run([sys.executable, "-c", code, str(gone)], env=self.environment,
                                text=True, capture_output=True, check=False)
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertNotIn("Traceback", failed.stderr)
        self.assertIn("--scope", failed.stderr)
        long_scope = "k" * 5000    # not a path the system can look at, so a keyword
        listed = self.msg("who", "--json", "--scope", long_scope)
        self.assertEqual(listed.returncode, 0, listed.stderr)


class OfflineQueueTests(RuntimeCase):
    def message_id(self, output: str) -> str:
        match = re.search(r"\(([0-9a-f]+-[0-9a-f]{6})\)", output)
        self.assertIsNotNone(match, output)
        assert match is not None
        return match.group(1)

    def test_message_for_a_name_that_is_away_waits_and_is_acknowledged(self) -> None:
        process, _, _ = self.listen("claude")
        process.terminate()
        process.wait(timeout=5)
        sent = self.msg("send", "--to", "claude", "--from", "codex", "while you were out")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertIn("offline", sent.stdout)
        identifier = self.message_id(sent.stdout)
        self.assertIn("pending for claude", self.msg("status", identifier).stdout)
        _, lines, _ = self.listen("claude")
        message = lines.message()
        assert message is not None
        self.assertEqual((message["from"], message["message"], message["id"]),
                         ("codex", "while you were out", identifier))
        self.assertIn("delivered to claude", self.msg("status", identifier).stdout)

    def test_direct_delivery_is_acknowledged_too(self) -> None:
        _, lines, _ = self.listen("claude")
        sent = self.msg("send", "--to", "claude", "--from", "codex", "hi")
        identifier = self.message_id(sent.stdout)
        self.assertIsNotNone(lines.message())
        self.assertIn("delivered to claude", self.msg("status", identifier).stdout)

    def test_expired_messages_are_dropped(self) -> None:
        process, _, _ = self.listen("claude")
        process.terminate()
        process.wait(timeout=5)
        self.assertEqual(self.msg("send", "--to", "claude", "--from", "codex", "stale",
                                  env={"FILO_SPOOL_TTL": "0.05"}).returncode, 0)
        time.sleep(0.3)
        _, lines, _ = self.listen("claude", env={"FILO_SPOOL_TTL": "0.05"})
        self.assertIsNone(lines.message(1.0))

    def test_unknown_names_still_fail_and_status_rejects_garbage(self) -> None:
        failed = self.msg("send", "--to", "ghost", "--from", "codex", "hi")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("not connected", failed.stderr)
        self.assertNotEqual(self.msg("status", "../../etc/passwd").returncode, 0)

    def test_a_spooled_message_stays_until_it_has_been_delivered(self) -> None:
        filo = load_filo()
        scope = "keyword:spool-order"
        with mock.patch.dict(os.environ, {"FILO_DIR": str(self.runtime)}):
            filo.spool_message(scope, "claude", "codex", "wait for me", filo.new_message_id())
            [path] = (filo.scope_directory(scope) / "spool" / "claude").glob("*.msg")

            def fail(_message: dict[str, object]) -> None:
                raise BrokenPipeError("the session's output is gone")

            with self.assertRaises(BrokenPipeError):
                filo.drain_spool(scope, "claude", fail)
            self.assertTrue(path.exists(), "the message was deleted before it was delivered")
            delivered: list[dict[str, object]] = []
            filo.drain_spool(scope, "claude", delivered.append)
            self.assertEqual([item["message"] for item in delivered], ["wait for me"])
            self.assertFalse(path.exists())
            self.assertIn("delivered to claude", filo.message_status(scope, path.stem))

    def test_a_spooled_message_that_cannot_be_shown_is_dropped_and_not_tried_for_ever(self) -> None:
        filo = load_filo()
        scope = "keyword:spool-poison"

        def refuse(_message: dict[str, object]) -> None:
            "\udcff".encode("utf-8")    # what printing a lone surrogate does: UnicodeEncodeError

        with mock.patch.dict(os.environ, {"FILO_DIR": str(self.runtime)}):
            filo.spool_message(scope, "claude", "codex", "caf\udce9", filo.new_message_id())
            filo.drain_spool(scope, "claude", refuse)
            self.assertEqual(list((filo.scope_directory(scope) / "spool" / "claude").glob("*.msg")), [])


class HardeningTests(RuntimeCase):
    def test_who_keeps_a_listener_that_is_only_slow(self) -> None:
        process, _, _ = self.listen("claude")
        process.send_signal(signal.SIGSTOP)
        who = json.loads(self.msg("who", "--json").stdout)
        self.assertEqual([row["name"] for row in who["participants"]], ["claude"])
        self.assertEqual(len(list(self.runtime.glob("*/claude.sock"))), 1)
        process.send_signal(signal.SIGCONT)
        self.assertEqual(self.msg("send", "--to", "claude", "--from", "x", "back").returncode, 0)

    def test_a_silent_client_does_not_block_the_listener_for_long(self) -> None:
        _, lines, _ = self.listen("claude")
        [sock_file] = self.runtime.glob("*/claude.sock")
        silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        silent.connect(str(sock_file))
        try:
            sent = self.msg("send", "--to", "claude", "--from", "x", "after the stall")
            self.assertEqual(sent.returncode, 0, sent.stderr)
            message = lines.message(8.0)
            assert message is not None
            self.assertEqual(message["message"], "after the stall")
        finally:
            silent.close()

    def test_a_client_that_sends_a_byte_at_a_time_is_cut_off(self) -> None:
        # The timeout of a read starts again with every byte, so a client that drips one every half
        # second could hold the listener for as long as it liked: no spool, no check on its parent.
        _, lines, _ = self.listen("claude")
        [sock_file] = self.runtime.glob("*/claude.sock")
        dripper = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dripper.connect(str(sock_file))
        stop = threading.Event()

        def drip() -> None:
            while not stop.is_set():
                try:
                    dripper.send(b" ")
                except OSError:
                    return
                stop.wait(0.5)

        thread = threading.Thread(target=drip, daemon=True)
        thread.start()
        try:
            time.sleep(0.2)
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(5)
            started = time.monotonic()
            probe.connect(str(sock_file))
            probe.sendall(b'{"op":"info"}\n')
            self.assertEqual(json.loads(probe.makefile().readline())["name"], "claude")
            self.assertLess(time.monotonic() - started, 4.0)
            probe.close()
        finally:
            stop.set()
            thread.join(timeout=2)
            dripper.close()
        self.assertIsNone(lines.message(0.2))

    def test_events_name_the_process_that_sent_them(self) -> None:
        _, lines, _ = self.listen("claude")
        sender = subprocess.Popen(
            [sys.executable, str(SCRIPT), "send", "--to", "claude", "--from", "x", "who am i"],
            cwd=self.scope, env=self.environment, stdout=subprocess.DEVNULL,
        )
        sender.wait(timeout=10)
        message = lines.message()
        assert message is not None
        self.assertEqual(message["peer_pid"], sender.pid)

    def test_an_id_that_is_not_a_message_id_is_never_part_of_a_path(self) -> None:
        _, lines, _ = self.listen("claude")
        [sock_file] = self.runtime.glob("*/claude.sock")
        for index, identifier in enumerate(("../../escape", "a/b", "x" * 300)):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(5)
            client.connect(str(sock_file))
            client.sendall(json.dumps({"op": "message", "id": identifier, "from": "raw",
                                       "message": f"hello {index}"}).encode() + b"\n")
            self.assertEqual(json.loads(client.makefile().readline()), {"ok": True})
            client.close()
            event = lines.message()
            assert event is not None
            self.assertEqual(event["message"], f"hello {index}")
        self.assertEqual(sorted(path.name for path in self.runtime.parent.glob("escape*")), [])
        self.assertEqual(sorted(path.name for path in self.runtime.glob("escape*")), [])
        self.assertEqual(list(self.runtime.glob("*/acks/*")), [])


class SessionTests(CodexQueueRouteTests):
    """Several Codex sessions in one directory are told apart by name."""

    A = "aaaa1111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    B = "bbbb2222-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    def register_session(self, thread: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "register", "--as", "codex"],
            cwd=self.scope, env={**self.environment, "CODEX_THREAD_ID": thread},
            text=True, capture_output=True, check=False,
        )

    def queued_thread(self) -> str:
        return json.loads(self.record.read_text(encoding="utf-8"))[2]

    def test_two_sessions_have_their_own_names_and_are_addressed_separately(self) -> None:
        self.start_codex(2)
        first, second = self.register_session(self.A), self.register_session(self.B)
        self.assertIn("registered as codex ", first.stdout)
        self.assertIn("registered as codex-2 ", second.stdout)
        self.assertIn("--from codex-2", second.stdout)
        self.assertIn("registered as codex ", self.register_session(self.A).stdout)

        self.assertEqual(self.send("--to", "codex-2", "--from", "claude", "to the second").returncode, 0)
        self.assertEqual(self.queued_thread(), self.B)
        args = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertIn("filo send --to claude --from codex-2", args[args.index("--message") + 1])
        self.assertEqual(self.send("--to", "codex", "--from", "claude", "to the first").returncode, 0)
        self.assertEqual(self.queued_thread(), self.A)

    def test_sessions_are_forgotten_only_when_none_of_them_runs_any_more(self) -> None:
        self.start_codex(1)
        self.register_session(self.A)
        self.register_session(self.B)
        # One process for two registrations: which one ended cannot be told, so both stay.
        names = [row["name"] for row in json.loads(self.msg_who().stdout)["registered"]]
        self.assertEqual(names, ["codex", "codex-2"])
        self.assertEqual(len(list(self.runtime.glob("*/*.reg"))), 2)
        for process in self.live:
            process.terminate()
            process.wait(timeout=5)
        self.assertEqual(json.loads(self.msg_who().stdout)["registered"], [])
        self.assertEqual(list(self.runtime.glob("*/*.reg")), [])

    def test_nothing_is_forgotten_where_the_process_table_is_not_trusted(self) -> None:
        self.register_session(self.A)
        who = self.msg_who(env={"CODEX_SANDBOX_NETWORK_DISABLED": "1"})
        self.assertEqual([row["name"] for row in json.loads(who.stdout)["registered"]], ["codex"])

    def msg_who(self, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "who", "--json"],
            cwd=self.scope, env={**self.environment, **(env or {})},
            text=True, capture_output=True, check=False,
        )


class AdapterConfigTests(RuntimeCase):
    """A client the code has never heard of can be added from a config file."""

    def setUp(self) -> None:
        super().setUp()
        self.record = self.root / "gemini-args.json"
        self.tool = make_recorder(self.root / "gemini-tool", self.record)
        self.config = self.root / "adapters.json"
        self.config.write_text(json.dumps({"gemini": {
            "id_env": "GEMINI_SESSION",
            "deliver": [str(self.tool), "send", "--session", "{session}", "{message}"],
        }}), encoding="utf-8")
        self.environment["FILO_ADAPTERS"] = str(self.config)

    def test_register_and_send_through_a_configured_adapter(self) -> None:
        registered = self.msg("register", "--as", "gemini", env={"GEMINI_SESSION": "gem-123456"})
        self.assertEqual(registered.returncode, 0, registered.stderr)
        self.assertIn("registered as gemini ", registered.stdout)
        who = json.loads(self.msg("who", "--json").stdout)
        self.assertEqual([(row["name"], row["client"]) for row in who["registered"]], [("gemini", "gemini")])
        sent = self.msg("send", "--to", "gemini", "--from", "claude", "hello gemini")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        args = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(args[:3], ["send", "--session", "gem-123456"])
        self.assertIn("[filo] Message from claude:", args[3])
        self.assertIn("hello gemini", args[3])
        self.assertIn("--from gemini", args[3])

    def test_captured_environment_is_stored_privately_and_given_to_delivery(self) -> None:
        env_record = self.root / "env.json"
        tool = self.root / "env-tool"
        tool.write_text("#!/usr/bin/env python3\nimport json, os\n"
                        f"open({str(env_record)!r}, 'w').write(json.dumps(os.environ.get('GEM_TOKEN')))\n",
                        encoding="utf-8")
        tool.chmod(0o755)
        self.config.write_text(json.dumps({"gemini": {
            "id_env": "GEMINI_SESSION", "capture_env": ["GEM_TOKEN"], "deliver": [str(tool)],
        }}), encoding="utf-8")
        registered = self.msg("register", "--as", "gemini", env={"GEMINI_SESSION": "gem-123456", "GEM_TOKEN": "s3cret"})
        self.assertEqual(registered.returncode, 0, registered.stderr)
        (reg,) = self.runtime.glob("*/gemini.reg")
        self.assertEqual(reg.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.msg("send", "--to", "gemini", "--from", "claude", "hi").returncode, 0)
        self.assertEqual(json.loads(env_record.read_text(encoding="utf-8")), "s3cret")

    def test_delivery_gets_only_the_variables_that_the_adapter_captures(self) -> None:
        env_record = self.root / "env.json"
        tool = self.root / "env-tool"
        tool.write_text("#!/usr/bin/env python3\nimport json, os\n"
                        f"open({str(env_record)!r}, 'w').write(json.dumps([os.environ.get('GEM_TOKEN'), "
                        "os.environ.get('LD_PRELOAD')]))\n", encoding="utf-8")
        tool.chmod(0o755)
        self.config.write_text(json.dumps({"gemini": {
            "id_env": "GEMINI_SESSION", "capture_env": ["GEM_TOKEN"], "deliver": [str(tool)],
        }}), encoding="utf-8")
        registered = self.msg("register", "--as", "gemini", env={"GEMINI_SESSION": "gem-123456", "GEM_TOKEN": "s3cret"})
        self.assertEqual(registered.returncode, 0, registered.stderr)
        (reg,) = self.runtime.glob("*/gemini.reg")
        record = json.loads(reg.read_text(encoding="utf-8"))
        record["env"]["LD_PRELOAD"] = "/tmp/not-yours.so"    # what anything that can write the file could add
        reg.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(self.msg("send", "--to", "gemini", "--from", "claude", "hi").returncode, 0)
        self.assertEqual(json.loads(env_record.read_text(encoding="utf-8")), ["s3cret", None])

    def test_agy_registers_its_sidecar_and_delivers_through_agentapi(self) -> None:
        tool = self.root / "agy-stub"
        tool.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                        f"open({str(self.record)!r}, 'w').write(json.dumps([sys.argv[1:], os.environ['ANTIGRAVITY_LS_ADDRESS']]))\n",
                        encoding="utf-8")
        tool.chmod(0o755)
        session = {"ANTIGRAVITY_CONVERSATION_ID": "conv-123456", "ANTIGRAVITY_LS_ADDRESS": "localhost:1",
                   "ANTIGRAVITY_AGENTAPI_EXE": str(tool)}
        self.assertEqual(self.msg("register", "--as", "agy", env=session).returncode, 0)
        sent = self.msg("send", "--to", "agy", "--from", "claude", "hello agy", env={"FILO_NO_PRUNE": "1"})
        self.assertEqual(sent.returncode, 0, sent.stderr)
        args, address = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(args[:3], ["agentapi", "send-message", "--title=filo"])
        self.assertEqual(args[3], "conv-123456")
        self.assertIn("hello agy", args[4])
        self.assertEqual(address, "localhost:1")

    def test_registered_sessions_can_be_named_and_renamed(self) -> None:
        env = {"GEMINI_SESSION": "gem-123456"}
        chosen = self.msg("register", "--as", "gemini", "--name", "reviewer", env=env)
        self.assertEqual(chosen.returncode, 0, chosen.stderr)
        self.assertIn("registered as reviewer ", chosen.stdout)
        again = self.msg("register", "--as", "gemini", "--name", "reviewer", env=env)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.msg("register", "--as", "gemini", "--name", "reviewer",
                                  env={"GEMINI_SESSION": "gem-654321"}).returncode, 1)
        self.assertEqual(self.msg("register", "--as", "gemini", "--name", "bad name", env=env).returncode, 1)
        renamed = self.msg("rename", "reviewer", "critic")
        self.assertEqual(renamed.returncode, 0, renamed.stderr)
        who = json.loads(self.msg("who", "--json").stdout)
        self.assertEqual([(row["name"], row["client"]) for row in who["registered"]], [("critic", "gemini")])
        self.assertEqual(self.msg("send", "--to", "critic", "--from", "claude", "hi").returncode, 0)
        self.assertEqual(json.loads(self.record.read_text(encoding="utf-8"))[:3], ["send", "--session", "gem-123456"])
        self.assertNotEqual(self.msg("send", "--to", "reviewer", "--from", "claude", "hi").returncode, 0)
        self.assertNotEqual(self.msg("rename", "ghost", "x").returncode, 0)
        self.msg("register", "--as", "gemini", "--name", "other", env={"GEMINI_SESSION": "gem-654321"})
        taken = self.msg("rename", "critic", "other")
        self.assertEqual(taken.returncode, 1)
        self.assertIn("taken", taken.stderr)

    def test_a_dead_agy_session_is_forgotten_and_nothing_is_queued_for_it(self) -> None:
        tool = self.root / "agy-stub"
        tool.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                        f"open({str(self.record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
        tool.chmod(0o755)
        sidecar = socket.socket()
        sidecar.bind(("127.0.0.1", 0))
        sidecar.listen(1)
        session = {"ANTIGRAVITY_CONVERSATION_ID": "conv-123456", "ANTIGRAVITY_AGENTAPI_EXE": str(tool),
                   "ANTIGRAVITY_LS_ADDRESS": f"localhost:{sidecar.getsockname()[1]}"}
        self.assertEqual(self.msg("register", "--as", "agy", env=session).returncode, 0)
        self.assertEqual(self.msg("send", "--to", "agy", "--from", "claude", "alive").returncode, 0)
        sidecar.close()
        gone = self.msg("send", "--to", "agy", "--from", "claude", "dead")
        self.assertEqual(gone.returncode, 1)
        self.assertIn("has ended", gone.stderr)
        self.assertEqual(list(self.runtime.glob("*/agy.reg")), [])
        self.assertEqual(list(self.runtime.glob("*/spool/*/*.msg")), [])

    def test_a_registered_session_can_leave_the_pool(self) -> None:
        env = {"GEMINI_SESSION": "gem-123456"}
        self.assertEqual(self.msg("register", "--as", "gemini", "--name", "reviewer", env=env).returncode, 0)
        self.assertEqual(self.msg("register", "--as", "gemini", "--name", "other",
                                  env={"GEMINI_SESSION": "gem-654321"}).returncode, 0)
        left = self.msg("leave", env=env)
        self.assertEqual(left.returncode, 0, left.stderr)
        self.assertIn("reviewer left the pool", left.stdout)
        who = json.loads(self.msg("who", "--json").stdout)
        self.assertEqual([row["name"] for row in who["registered"]], ["other"])
        refused = self.msg("send", "--to", "reviewer", "--from", "claude", "hi")
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(self.msg("register", "--as", "gemini", "--name", "reviewer", env=env).returncode, 0)
        named = self.msg("leave", "--name", "other")
        self.assertEqual(named.returncode, 0, named.stderr)
        self.assertEqual(self.msg("leave", "--name", "other").returncode, 1)
        self.assertIn("not registered", self.msg("leave", env={"GEMINI_SESSION": "gem-000000"}).stderr)

    def test_leave_asks_a_listener_to_stop_and_refuses_a_name_that_is_not_there(self) -> None:
        process, _lines, _joined = self.listen("claude")
        left = self.msg("leave", "--name", "claude")
        self.assertEqual(left.returncode, 0, left.stderr)
        self.assertEqual(process.wait(timeout=5), 0)
        refused = self.msg("leave", "--name", "claude")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("not a registered session or a listener", refused.stderr)

    def test_a_listener_started_for_a_parent_leaves_when_the_parent_ends(self) -> None:
        parent = subprocess.Popen(["sleep", "30"])
        self.children.append(cast("subprocess.Popen[bytes]", parent))
        process, _lines, _joined = self.listen("child", "--parent", str(parent.pid))
        self.assertIsNone(process.poll())
        parent.kill()
        parent.wait(timeout=5)
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(json.loads(self.msg("who", "--json").stdout)["participants"], [])

    def test_the_runtime_directory_must_be_a_plain_directory_of_yours(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        link = self.root / "runtime-link"
        link.symlink_to(elsewhere)
        refused = self.msg("who", env={"FILO_DIR": str(link)})
        self.assertEqual(refused.returncode, 1)
        self.assertIn("not a plain directory of yours", refused.stderr)
        a_file = self.root / "runtime-file"
        a_file.write_text("x", encoding="utf-8")
        self.assertEqual(self.msg("who", env={"FILO_DIR": str(a_file)}).returncode, 1)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_scope_directory_that_is_a_link_is_refused(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        module = load_filo()
        with mock.patch.dict(os.environ, {"FILO_DIR": str(self.runtime)}):
            root = module.runtime_root()
            digest = hashlib.sha256(module.canonical_scope(str(self.scope)).encode("utf-8")).hexdigest()[:16]
        (root / digest).symlink_to(elsewhere)
        refused = self.msg("who")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("not a plain directory of yours", refused.stderr)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_an_open_directory_that_holds_a_link_is_not_trusted(self) -> None:
        loose = self.root / "loose-with-link"
        loose.mkdir()
        (loose / "trap").symlink_to(self.root)
        loose.chmod(0o755)
        refused = self.msg("who", env={"FILO_DIR": str(loose)})
        self.assertEqual(refused.returncode, 1)
        self.assertIn("'trap'", refused.stderr)
        self.assertEqual(loose.stat().st_mode & 0o777, 0o755)

    def test_a_metadata_file_that_is_a_link_is_not_read(self) -> None:
        module = load_filo()
        real = self.root / "real.json"
        real.write_text("{}", encoding="utf-8")
        link = self.root / "link.json"
        link.symlink_to(real)
        self.assertEqual(module.read_object(real), {})
        with self.assertRaises(OSError):
            module.read_object(link)

    def test_a_runtime_directory_that_is_too_open_is_closed(self) -> None:
        loose = self.root / "loose"
        loose.mkdir()
        loose.chmod(0o755)
        self.assertEqual(self.msg("who", env={"FILO_DIR": str(loose)}).returncode, 0)
        self.assertEqual(loose.stat().st_mode & 0o777, 0o700)

    def test_unknown_clients_and_bad_configs_are_rejected_clearly(self) -> None:
        unknown = self.msg("register", "--as", "nobody", "--session", "abcdef123")
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("known: agy, codex, gemini", unknown.stderr)
        self.config.write_text(json.dumps({"codex": {"deliver": ["x"]}}), encoding="utf-8")
        self.assertIn("built in", self.msg("who").stderr)
        self.config.write_text("{not json", encoding="utf-8")
        self.assertIn("adapters:", self.msg("who").stderr)
        self.config.write_text(json.dumps({"gemini": {"deliver": "not-a-list"}}), encoding="utf-8")
        self.assertIn("non-empty list", self.msg("who").stderr)

    def test_a_bad_adapters_file_is_reported_but_does_not_stop_who_or_join(self) -> None:
        registered = self.msg("register", "--as", "gemini", env={"GEMINI_SESSION": "gem-123456"})
        self.assertEqual(registered.returncode, 0, registered.stderr)
        self.config.write_text("{not json", encoding="utf-8")
        who = self.msg("who", "--json")
        self.assertEqual(who.returncode, 0, who.stderr)
        self.assertIn("adapters:", who.stderr)
        self.assertEqual([row["name"] for row in json.loads(who.stdout)["registered"]], ["gemini"])
        _, _, joined = self.listen("claude")
        self.assertEqual(joined["name"], "claude")
        self.assertEqual([row["name"] for row in json.loads(self.msg("who", "--json").stdout)["participants"]],
                         ["claude"])

    def test_a_bad_adapters_file_does_not_stop_a_message_to_a_listener_that_is_away(self) -> None:
        process, _, _ = self.listen("claude")
        process.terminate()
        process.wait(timeout=5)
        self.config.write_text("{not json", encoding="utf-8")
        sent = self.msg("send", "--to", "claude", "--from", "me", "hello")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertIn("queued", sent.stdout)


SHELL = shutil.which("sh") or "/bin/sh"


def load_filo() -> Any:
    """The filo module itself, to call its functions directly."""
    spec = importlib.util.spec_from_file_location("filo_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module   # dataclasses look their own module up here
    spec.loader.exec_module(module)
    return module


class NameClaimTests(unittest.TestCase):
    def test_only_one_of_many_simultaneous_claims_gets_a_name(self) -> None:
        module = load_filo()
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "codex.reg"
            results: list[bool] = []
            start = threading.Barrier(16)

            def claim(number: int) -> None:
                start.wait()
                results.append(module.write_metadata(path, {"name": "codex", "n": number}, exclusive=True))

            threads = [threading.Thread(target=claim, args=(number,)) for number in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(results.count(True), 1)
            self.assertEqual(sorted(entry.name for entry in Path(raw).iterdir()), ["codex.reg"])

    def test_sessions_that_register_at_the_same_time_get_different_names(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            environment = {**os.environ, "FILO_DIR": f"{raw}/rt"}
            scope = Path(raw, "project")
            scope.mkdir()
            processes = [subprocess.Popen(
                [sys.executable, str(SCRIPT), "register", "--as", "codex", "--session", f"session-{number:04d}"],
                cwd=scope, env=environment, stdout=subprocess.PIPE, text=True) for number in range(8)]
            for process in processes:
                process.wait(timeout=30)
                if process.stdout is not None:
                    process.stdout.close()
            who = subprocess.run([sys.executable, str(SCRIPT), "who", "--json"], cwd=scope,
                                 env={**environment, "FILO_NO_PRUNE": "1"}, capture_output=True, text=True,
                                 check=True, timeout=30)
            names = [row["name"] for row in json.loads(who.stdout)["registered"]]
            self.assertEqual(len(names), 8)
            self.assertEqual(len(set(names)), 8)


class PlatformTests(unittest.TestCase):
    def test_a_runtime_directory_whose_sockets_could_not_bind_is_named_as_the_problem(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            deep = Path(raw, "a" * 60, "b" * 60)
            result = subprocess.run([sys.executable, str(SCRIPT), "join", "--as", "x", "--json"],
                                    env={**os.environ, "FILO_DIR": str(deep)}, cwd=raw,
                                    capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("socket path is too long", result.stderr)

    def test_lsof_output_is_read_for_the_command_that_was_asked_about(self) -> None:
        output = "p101\ncfoo\nfcwd\nn/home/a\np102\nccodex\nfcwd\nn/home/b\np103\nccodex\nfcwd\nn/home/c\n"
        self.assertEqual(load_filo().parse_lsof(output, "codex"), ["/home/b", "/home/c"])
        self.assertEqual(load_filo().parse_lsof(output, "nobody"), [])
        self.assertEqual(load_filo().parse_lsof("", "codex"), [])

    def test_a_system_that_cannot_list_processes_forgets_nobody(self) -> None:
        module = load_filo()
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {"FILO_DIR": f"{raw}/rt"}):
            scope = module.canonical_scope(raw)
            module.write_metadata(module.scope_directory(scope) / "codex.reg", {
                "name": "codex", "client": "codex", "session": "abcdef123456", "registered_ns": 1, "env": {}})
            with mock.patch.object(module, "process_cwds", return_value=None):
                self.assertEqual([row.name for row in module.live_registrations(scope)], ["codex"])
            with mock.patch.object(module, "process_cwds", return_value=[]):
                self.assertEqual(module.live_registrations(scope), [])


class ProfileTests(RuntimeCase):
    """A profile is a markdown file of instructions; yours win over the built-in ones."""

    def setUp(self) -> None:
        super().setUp()
        self.mine = self.root / "profiles"
        self.environment["FILO_PROFILES"] = str(self.mine)

    def test_the_built_in_profiles_are_listed_and_printed(self) -> None:
        listed = self.msg("profiles")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        for name in ("manager", "worker", "researcher", "librarian"):
            self.assertRegex(listed.stdout, rf"(?m)^{name}\s+\[built in\]\s+\S")
        printed = self.msg("profile", "manager")
        self.assertEqual(printed.returncode, 0, printed.stderr)
        self.assertIn("filo send", printed.stdout)

    def test_every_built_in_profile_starts_with_a_one_line_summary(self) -> None:
        for path in sorted((ROOT / "skills" / "filo" / "profiles").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            first, _, rest = text.partition("\n")
            self.assertTrue(first.strip() and len(first) <= 120, path.name)
            self.assertTrue(rest.strip(), path.name)
            self.assertNotIn("llm", text.lower(), path.name)

    def test_your_profiles_add_to_the_list_and_hide_a_built_in_one_of_the_same_name(self) -> None:
        self.mine.mkdir()
        (self.mine / "dba.md").write_text("Looks after the database.\n\nOnly touch tables you were given.\n", encoding="utf-8")
        (self.mine / "worker.md").write_text("A stricter worker.\n\nAsk before every step.\n", encoding="utf-8")
        (self.mine / "not a name.md").write_text("ignored\n", encoding="utf-8")
        listed = self.msg("profiles").stdout
        self.assertRegex(listed, r"(?m)^dba\s+\[yours\]\s+Looks after the database\.")
        self.assertRegex(listed, r"(?m)^worker\s+\[yours\]\s+A stricter worker\.")
        self.assertNotIn("not a name", listed)
        self.assertIn("Ask before every step.", self.msg("profile", "worker").stdout)
        self.assertIn("Only touch tables", self.msg("profile", "dba").stdout)

    def test_an_unknown_or_unsafe_profile_is_refused_with_the_known_names(self) -> None:
        unknown = self.msg("profile", "nobody")
        self.assertEqual(unknown.returncode, 1)
        self.assertIn("known:", unknown.stderr)
        self.assertIn("manager", unknown.stderr)
        self.assertEqual(self.msg("profile", "../manager").returncode, 1)
        self.assertEqual(self.msg("profile", "a/b").returncode, 1)


class OpencodePluginTests(unittest.TestCase):
    def test_the_plugin_bridges_filo_to_a_session(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        result = subprocess.run([node, str(ROOT / "tests" / "opencode_plugin.test.mjs")],
                                capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class WorkflowTests(unittest.TestCase):
    def test_every_action_is_pinned_to_a_commit_and_tokens_are_read_only_by_default(self) -> None:
        workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        self.assertTrue(workflows)
        for workflow in workflows:
            text = workflow.read_text(encoding="utf-8")
            for action, ref in re.findall(r"uses:\s*(\S+)@(\S+)", text):
                self.assertRegex(ref, r"^[0-9a-f]{40}$", f"{workflow.name}: {action} is not pinned to a commit")
            self.assertRegex(text, r"(?m)^permissions:", f"{workflow.name} has no top-level permissions")
            for command in re.findall(r"run:\s*(pip install[^\n]*)", text):
                self.assertIn("--require-hashes", command, f"{workflow.name}: pip install without hash checking")


class ReleaseWorkflowTests(unittest.TestCase):
    """What a release checks has to be checked before it signs: signing writes to a public log."""

    def test_a_release_looks_at_its_tag_and_its_notes_before_signing_and_builds_from_a_commit_on_main(self) -> None:
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        signing = text.index("cosign sign-blob")
        for needed in ("merge-base --is-ancestor \"$GITHUB_SHA\" origin/main", 'test -f "docs/releases/$GITHUB_REF_NAME.md"',
                       'test "v$version" = "$GITHUB_REF_NAME"'):
            self.assertIn(needed, text)
            self.assertLess(text.index(needed), signing, f"{needed} comes after the signing")
        self.assertIn("fetch-depth: 0", text)
        self.assertIn('"$GITHUB_SHA"\n', text.split("git archive", 1)[1].split("\n", 1)[0] + "\n")
        self.assertIn("SHA256SUMS.sigstore.json", text)

    def test_the_checks_are_bounded_and_one_failure_does_not_hide_another(self) -> None:
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertEqual(text.count("timeout-minutes:"), 2)
        self.assertIn("fail-fast: false", text)
        self.assertIn("branches: [main]", text)


class VersionTests(unittest.TestCase):
    def test_the_command_reports_the_version_the_package_declares(self) -> None:
        match = re.search(r'(?m)^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert match is not None
        result = subprocess.run([sys.executable, str(SCRIPT), "--version"], capture_output=True, text=True,
                                check=False, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"filo {match.group(1)}")


class ReleaseTests(unittest.TestCase):
    def test_the_current_version_has_release_notes(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'(?m)^version = "([^"]+)"', text)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertTrue((ROOT / "docs" / "releases" / f"v{match.group(1)}.md").is_file())


class ReadmeTests(unittest.TestCase):
    def test_every_image_the_readme_shows_exists(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        shown = re.findall(r'(?:src|srcset)="([^"]+)"|!\[[^\]]*\]\(([^)]+)\)', readme)
        paths = {path for first, second in shown if not (path := first or second).startswith("http")}
        self.assertTrue(paths)
        for path in sorted(paths):
            self.assertTrue((ROOT / path).is_file(), path)


    def test_every_variable_that_the_code_reads_is_in_the_readme(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        sources = [ROOT / "filo.py", ROOT / "get.sh", ROOT / "install.sh", ROOT / "uninstall.sh",
                   ROOT / "scripts" / "common.sh", ROOT / "plugins" / "opencode" / "filo.js"]
        used = {name for source in sources for name in re.findall(r"\bFILO_[A-Z_]+\b", source.read_text(encoding="utf-8"))}
        self.assertIn("FILO_NO_PRUNE", used)    # the search itself finds them
        for name in sorted(used):
            self.assertTrue(name in readme, f"{name} is read by the code and the README does not say so")


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.TemporaryDirectory()
        root = Path(self.base.name)
        self.bin = root / "bin"
        self.claude, self.agents, self.agy = (root / f"{client}-home" / "skills" for client in ("claude", "agents", "agy"))
        for skills in (self.claude, self.agents, self.agy):
            skills.parent.mkdir()
        self.opencode = root / "opencode-home"
        self.opencode.mkdir()
        # A PATH that holds only what the scripts need, so that the clients installed on
        # this machine (claude, codex, agy) do not change the result.
        self.tools = root / "tools"
        self.tools.mkdir()
        for tool in ("dirname", "readlink", "mkdir", "ln", "rm", "sed", "python3", "id"):
            found = shutil.which(tool)
            assert found is not None
            (self.tools / tool).symlink_to(found)
        self.environment = {
            **os.environ,
            "PATH": f"{self.bin}:{self.tools}",
            "FILO_BIN_DIR": str(self.bin),
            "FILO_CLAUDE_SKILLS": str(self.claude),
            "FILO_AGENTS_SKILLS": str(self.agents),
            "FILO_AGY_SKILLS": str(self.agy),
            "FILO_OPENCODE_DIR": str(self.opencode),
        }

    def tearDown(self) -> None:
        self.base.cleanup()

    def run_script(self, name: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([SHELL, str(ROOT / name), *args], env=self.environment,
                              text=True, capture_output=True, check=False)

    def install(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run_script("install.sh", *args)

    def uninstall(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run_script("uninstall.sh", *args)

    def test_install_is_idempotent_and_uninstall_removes_only_its_own_links(self) -> None:
        self.agents.mkdir()
        (self.agents / "filo").symlink_to("/somewhere/else")   # someone else's link
        self.assertEqual(self.install().returncode, 0)
        again = self.install()
        self.assertEqual(again.returncode, 0)
        self.assertIn("already linked", again.stdout)
        self.assertEqual((self.bin / "filo").resolve(), SCRIPT)
        self.assertEqual((self.claude / "filo").resolve(), ROOT / "skills" / "filo")
        self.assertEqual((self.agy / "filo").resolve(), ROOT / "skills" / "filo")
        self.assertEqual(os.readlink(self.agents / "filo"), "/somewhere/else")
        entry = subprocess.run([str(self.bin / "filo"), "who", "--scope", "x"], env={
            **os.environ, "FILO_DIR": str(Path(self.base.name) / "rt")},
            text=True, capture_output=True, check=False)
        self.assertEqual(entry.returncode, 0, entry.stderr)
        self.assertEqual(self.uninstall().returncode, 0)
        self.assertFalse((self.bin / "filo").exists() or (self.bin / "filo").is_symlink())
        self.assertFalse((self.claude / "filo").is_symlink())
        self.assertFalse((self.agy / "filo").is_symlink())
        self.assertEqual(os.readlink(self.agents / "filo"), "/somewhere/else")

    def test_existing_files_that_are_not_ours_are_never_overwritten(self) -> None:
        self.bin.mkdir()
        (self.bin / "filo").write_text("not ours", encoding="utf-8")
        skipped = self.install()
        self.assertIn("skipped", skipped.stderr)
        self.assertEqual((self.bin / "filo").read_text(encoding="utf-8"), "not ours")
        self.assertEqual(self.uninstall().returncode, 0)
        self.assertEqual((self.bin / "filo").read_text(encoding="utf-8"), "not ours")

    def test_install_says_what_it_will_do_and_a_dry_run_changes_nothing(self) -> None:
        dry = self.install("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        for expected in (str(self.bin / "filo"), str(self.claude / "filo"), "changes permissions", "never uses sudo",
                         "Dry run: nothing was changed"):
            self.assertIn(expected, dry.stdout)
        self.assertFalse(self.bin.exists())
        self.assertFalse((self.claude / "filo").is_symlink())
        mode = SCRIPT.stat().st_mode
        self.assertEqual(self.uninstall("--dry-run").returncode, 0)
        self.assertEqual(SCRIPT.stat().st_mode, mode)

    def test_a_directory_that_install_will_make_is_announced_before(self) -> None:
        dry = self.install("--dry-run")
        self.assertIn(f"create the directory {self.bin}", dry.stdout)
        self.assertIn(f"create the directory {self.opencode / 'plugins'}", dry.stdout)
        self.assertNotIn(f"create the directory {self.claude.parent},", dry.stdout)    # it is there already
        self.install()
        again = self.install("--dry-run")
        self.assertNotIn("create the directory", again.stdout)    # nothing left to make

    def test_a_checkout_that_is_reached_through_a_link_is_still_the_checkout(self) -> None:
        alias = Path(self.base.name) / "alias"
        alias.symlink_to(ROOT)
        installed = subprocess.run([SHELL, str(alias / "install.sh")], env=self.environment, text=True,
                                   capture_output=True, check=False)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(os.readlink(self.bin / "filo"), str(alias / "filo.py"))
        self.assertIn("already linked", self.install().stdout)    # by the other name of the same checkout
        self.assertEqual(self.uninstall().returncode, 0)
        self.assertFalse((self.bin / "filo").is_symlink())
        self.assertFalse((self.claude / "filo").is_symlink())

    def test_a_client_that_is_not_installed_gets_no_directory(self) -> None:
        self.agy.parent.rmdir()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.agy.parent.exists())
        self.assertIn("not found", result.stdout)
        self.assertTrue((self.claude / "filo").is_symlink())

    def test_a_client_with_a_command_but_no_directory_yet_still_gets_the_skill(self) -> None:
        self.agy.parent.rmdir()
        stub = self.tools / "agy"
        stub.write_text("#!/bin/sh\n", encoding="utf-8")
        stub.chmod(0o755)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.agy / "filo").resolve(), ROOT / "skills" / "filo")

    def test_install_never_changes_permissions_and_refuses_a_command_that_cannot_run(self) -> None:
        checkout = Path(self.base.name) / "checkout"
        shutil.copytree(ROOT, checkout, ignore=shutil.ignore_patterns(".git", "__pycache__", "tests"))
        (checkout / "filo.py").chmod(0o644)
        result = subprocess.run([SHELL, str(checkout / "install.sh")], env=self.environment,
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not executable", result.stderr)
        self.assertEqual((checkout / "filo.py").stat().st_mode & 0o777, 0o644)
        self.assertFalse((self.bin / "filo").is_symlink())

    def test_opencode_gets_its_plugin_and_command_and_loses_them_on_uninstall(self) -> None:
        plugin, command = self.opencode / "plugins" / "filo.js", self.opencode / "commands" / "filo.md"
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(plugin.resolve(), ROOT / "plugins" / "opencode" / "filo.js")
        self.assertEqual(command.resolve(), ROOT / "commands" / "opencode" / "filo.md")
        self.assertEqual(self.uninstall().returncode, 0)
        self.assertFalse(plugin.is_symlink() or command.is_symlink())

    def test_without_opencode_nothing_is_created_for_it(self) -> None:
        self.opencode.rmdir()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.opencode.exists())

    def test_uninstall_declares_what_it_leaves_behind(self) -> None:
        self.install()
        result = self.uninstall()
        self.assertIn("does not touch", result.stdout)
        self.assertIn("/tmp/filo-", result.stdout)
        self.assertIn("adapters.json", result.stdout)

    def test_unknown_options_are_refused(self) -> None:
        self.assertEqual(self.install("--uninstall").returncode, 2)
        self.assertEqual(self.uninstall("--force").returncode, 2)


class PreviewTests(unittest.TestCase):
    def test_the_body_comes_last_and_an_event_that_fits_is_left_alone(self) -> None:
        module = load_filo()
        message = {"id": "18de6610f927c276-fe7820", "from": "manager", "message": "short", "created_ns": 1}
        for preview in (None, 400):
            event = module.message_event("researcher", message, 4242, preview)
            self.assertEqual(list(event)[-1], "message")
            self.assertLess(list(event).index("reply"), list(event).index("message"))
            self.assertEqual(event["message"], "short")
            self.assertNotIn("truncated", event)

    def test_a_long_event_is_cut_to_the_limit_and_says_so(self) -> None:
        module = load_filo()
        body = ("Say \"hello\"\nè già tardi, ci vediamo domani. " * 80)
        message = {"id": "18de6610f927c276-fe7820", "from": "manager", "message": body, "created_ns": 1}
        for limit in (100, 400, 1000):
            event = module.message_event("researcher", message, 4242, limit)
            self.assertIs(event["truncated"], True)
            self.assertEqual(event["length"], len(body))
            self.assertEqual(event["show"], "filo show 18de6610f927c276-fe7820")
            self.assertTrue(body.startswith(event["message"]))
            self.assertEqual(list(event)[-1], "message")
            if limit >= 400:
                self.assertLessEqual(len(json.dumps(event, ensure_ascii=False)), limit)
                self.assertGreater(len(event["message"]), 0)
            else:   # the head alone is longer than the limit: no body, but the way to read it
                self.assertEqual(event["message"], "")

    def test_the_cut_is_the_longest_that_fits(self) -> None:
        module = load_filo()
        message = {"id": "18de6610f927c276-fe7820", "from": "a", "message": "x" * 5000, "created_ns": 1}
        event = module.message_event("b", message, None, 500)
        size = len(json.dumps(event, ensure_ascii=False))
        self.assertLessEqual(size, 500)
        self.assertGreater(size, 498)

    def test_a_message_without_an_id_is_never_cut(self) -> None:
        module = load_filo()
        event = module.message_event("b", {"from": "a", "message": "x" * 5000}, None, 200)
        self.assertEqual(len(event["message"]), 5000)
        self.assertNotIn("truncated", event)


class ShowTests(RuntimeCase):
    def test_a_preview_says_it_is_one_and_show_prints_the_rest(self) -> None:
        _, lines, _ = self.listen("researcher", "--preview", "400")
        body = "First the instruction, then \"the rest\".\n" + "More text with è and ü. " * 120 + "THE END"
        sent = self.msg("send", "--to", "researcher", "--from", "manager", "--stdin", stdin=body)
        self.assertEqual(sent.returncode, 0, sent.stderr)
        event = lines.message()
        assert event is not None
        self.assertIs(event["truncated"], True)
        self.assertEqual(event["length"], len(body))
        self.assertTrue(body.startswith(str(event["message"])))
        self.assertNotIn("THE END", json.dumps(event))
        shown = self.msg("show", str(event["id"]))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(shown.stdout, body + "\n")
        as_json = json.loads(self.msg("show", str(event["id"]), "--json").stdout)
        self.assertEqual((as_json["message"], as_json["from"], as_json["to"]), (body, "manager", "researcher"))
        self.assertEqual(event["show"], f"filo show {event['id']}")

    def test_without_a_preview_the_event_is_whole_and_show_still_works(self) -> None:
        _, lines, _ = self.listen("researcher")
        body = "y" * 3000
        self.msg("send", "--to", "researcher", "--from", "manager", "--stdin", stdin=body)
        event = lines.message()
        assert event is not None
        self.assertEqual(event["message"], body)
        self.assertNotIn("truncated", event)
        self.assertEqual(self.msg("show", str(event["id"])).stdout, body + "\n")

    def test_what_is_kept_is_private(self) -> None:
        _, lines, _ = self.listen("researcher")
        self.msg("send", "--to", "researcher", "--from", "manager", "--message", "secret plans")
        event = lines.message()
        assert event is not None
        files = list(self.runtime.glob(f"**/inbox/researcher/{event['id']}.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(files[0].parent.stat().st_mode & 0o777, 0o700)

    def test_a_message_left_for_a_listener_is_kept_when_it_arrives(self) -> None:
        process, lines, _ = self.listen("researcher")
        process.send_signal(signal.SIGSTOP)
        sent = self.msg("send", "--to", "researcher", "--from", "manager", "--message", "while stopped " * 50)
        self.assertEqual(sent.returncode, 0, sent.stderr)
        process.send_signal(signal.SIGCONT)
        event = lines.message()
        assert event is not None
        self.assertEqual(self.msg("show", str(event["id"])).stdout, "while stopped " * 50 + "\n")

    def test_an_unknown_id_and_a_value_that_is_not_an_id_are_errors(self) -> None:
        unknown = self.msg("show", "18de6610f927c276-fe7820")
        self.assertEqual(unknown.returncode, 1)
        self.assertIn("no message", unknown.stderr)
        bad = self.msg("show", "../../etc/passwd")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("not a message id", bad.stderr)

    def test_old_messages_are_forgotten(self) -> None:
        module = load_filo()
        with tempfile.TemporaryDirectory() as raw:
            os.environ["FILO_DIR"] = str(Path(raw) / "rt")
            try:
                scope = module.canonical_scope(str(self.scope))
                message = {"id": "18de6610f927c276-fe7820", "from": "a", "message": "x", "created_ns": 1}
                module.keep_received(scope, "b", message)
                kept = module.scope_directory(scope) / "inbox" / "b" / "18de6610f927c276-fe7820.json"
                self.assertTrue(kept.is_file())
                module.prune_inbox(scope)
                self.assertTrue(kept.is_file())
                old = time.time() - module.INBOX_TTL - 10
                os.utime(kept, (old, old))
                module.prune_inbox(scope)
                self.assertFalse(kept.exists())
            finally:
                del os.environ["FILO_DIR"]

    def test_old_acknowledgements_are_forgotten_by_the_listener_and_not_at_every_pass(self) -> None:
        module = load_filo()
        scope = "keyword:ack-pruning"
        with mock.patch.dict(os.environ, {"FILO_DIR": str(self.runtime)}):
            module.write_ack(scope, "18de6610f927c276-fe7820", "b", "delivered directly")
            [ack] = (module.scope_directory(scope) / "acks").glob("*.json")
            old = time.time() - module.ACK_TTL - 10
            os.utime(ack, (old, old))
            module.drain_spool(scope, "b", lambda _message: None)    # runs twice a second: not its job
            self.assertTrue(ack.exists())
            module.prune_acks(scope)
            self.assertFalse(ack.exists())

    def test_a_preview_that_is_too_small_is_refused(self) -> None:
        result = self.msg("join", "--as", "x", "--json", "--preview", "10")
        self.assertEqual(result.returncode, 1)
        self.assertIn("at least", result.stderr)


def add_gemini_adapter(case: RuntimeCase) -> Path:
    """Let the case register a `gemini` client whose deliveries are recorded; returns the record."""
    record = case.root / "gemini-args.json"
    tool = make_recorder(case.root / "gemini-tool", record)
    config = case.root / "adapters.json"
    config.write_text(json.dumps({"gemini": {
        "id_env": "GEMINI_SESSION", "deliver": [str(tool), "send", "--session", "{session}", "{message}"],
    }}), encoding="utf-8")
    case.environment["FILO_ADAPTERS"] = str(config)
    return record


class OriginTests(RuntimeCase):
    """Where a message comes from: the scope its sender belongs to, not the directory it happens to be in."""

    def setUp(self) -> None:
        super().setUp()
        self.elsewhere = self.root / "elsewhere"
        self.elsewhere.mkdir()
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        add_gemini_adapter(self)

    def test_a_message_from_inside_the_scope_says_nothing_about_where_it_came_from(self) -> None:
        _, lines, _ = self.listen("boss")
        self.msg("send", "--to", "boss", "--from", "me", "--message", "hello")
        event = lines.message()
        assert event is not None
        for key in ("foreign", "origin", "scope"):
            self.assertNotIn(key, event)
        self.assertEqual(event["reply"], 'filo send --to me --from boss --message "..."')

    def test_a_message_from_an_outsider_says_so_and_names_where_to_answer(self) -> None:
        _, lines, _ = self.listen("boss")
        sent = self.msg("send", "--to", "boss", "--from", "stranger", "--scope", str(self.scope),
                        "--message", "do this", scope=self.elsewhere)
        self.assertEqual(sent.returncode, 0, sent.stderr)
        event = lines.message()
        assert event is not None
        self.assertIs(event["foreign"], True)
        self.assertEqual(event["origin"], str(self.elsewhere.resolve()))
        self.assertEqual(event["reply"], f'filo send --to stranger --from boss --scope {self.elsewhere.resolve()} --message "..."')
        self.assertLess(list(event).index("foreign"), list(event).index("reply"))

    def test_a_session_that_moved_to_another_directory_is_still_in_its_own_scope(self) -> None:
        # registered in the project, then at work in a worktree: it writes to the project with --scope
        _, lines, _ = self.listen("boss")
        registered = self.msg("register", "--as", "gemini", "--name", "worker", env={"GEMINI_SESSION": "gem-123456"})
        self.assertEqual(registered.returncode, 0, registered.stderr)
        sent = self.msg("send", "--to", "boss", "--from", "worker", "--scope", str(self.scope),
                        "--message", "done", scope=self.worktree)
        self.assertEqual(sent.returncode, 0, sent.stderr)
        event = lines.message()
        assert event is not None
        self.assertNotIn("foreign", event)
        self.assertEqual(event["reply"], 'filo send --to worker --from boss --message "..."')

    def test_a_listener_in_another_scope_is_found_by_name_wherever_it_runs(self) -> None:
        _, lines, _ = self.listen("boss")
        self.listen("remote", scope=self.elsewhere)
        sent = self.msg("send", "--to", "boss", "--from", "remote", "--scope", str(self.scope),
                        "--message", "hi", scope=self.worktree)
        self.assertEqual(sent.returncode, 0, sent.stderr)
        event = lines.message()
        assert event is not None
        self.assertIs(event["foreign"], True)
        self.assertEqual(event["origin"], str(self.elsewhere.resolve()))     # not the worktree it ran in
        self.assertIn(f"--scope {self.elsewhere.resolve()}", str(event["reply"]))

    def test_a_name_that_belongs_to_two_scopes_is_not_guessed(self) -> None:
        second = self.root / "second"
        second.mkdir()
        _, lines, _ = self.listen("boss")
        self.listen("twin", scope=self.elsewhere)
        self.listen("twin", scope=second)
        self.msg("send", "--to", "boss", "--from", "twin", "--scope", str(self.scope),
                 "--message", "which one?", scope=self.worktree)
        event = lines.message()
        assert event is not None
        self.assertIs(event["foreign"], True)
        self.assertEqual(event["origin"], str(self.worktree.resolve()))      # where it ran: the safe answer

    def test_keyword_scopes_count_too(self) -> None:
        _, lines, _ = self.listen("boss", "--scope", "team")
        self.msg("send", "--to", "boss", "--from", "stranger", "--scope", "team", "--message", "hello",
                 scope=self.elsewhere)
        event = lines.message()
        assert event is not None
        self.assertIs(event["foreign"], True)
        self.assertEqual(event["origin"], str(self.elsewhere.resolve()))
        _, from_team, _ = self.listen("lead", scope=self.elsewhere)
        self.listen("member", "--scope", "team")
        self.msg("send", "--to", "lead", "--from", "member", "--scope", str(self.elsewhere), "--message", "hi")
        back = from_team.message()
        assert back is not None
        self.assertEqual((back["foreign"], back["origin"]), (True, "team"))
        self.assertIn("--scope team ", str(back["reply"]))

    def test_a_message_that_waited_in_the_spool_keeps_where_it_came_from(self) -> None:
        process, lines, _ = self.listen("boss")
        process.send_signal(signal.SIGSTOP)
        self.msg("send", "--to", "boss", "--from", "stranger", "--scope", str(self.scope),
                 "--message", "later", scope=self.elsewhere)
        process.send_signal(signal.SIGCONT)
        event = lines.message()
        assert event is not None
        self.assertIs(event["foreign"], True)
        self.assertEqual(event["origin"], str(self.elsewhere.resolve()))

    def test_what_an_older_filo_sends_is_still_understood(self) -> None:
        module = load_filo()
        scope = f"path:{self.scope.resolve()}"
        message = {"id": "18de6610f927c276-fe7820", "from": "old", "message": "x", "created_ns": 1}
        inside = module.message_event("me", {**message, "origin": str(self.scope.resolve())}, None, None, scope)
        outside = module.message_event("me", {**message, "origin": "/somewhere"}, None, None, scope)
        silent = module.message_event("me", message, None, None, scope)
        self.assertNotIn("foreign", inside)
        self.assertEqual((outside["foreign"], outside["origin"]), (True, "/somewhere"))
        self.assertNotIn("foreign", silent)

    def test_a_home_that_is_not_a_scope_is_an_outsider_and_is_never_trusted(self) -> None:
        module = load_filo()
        scope = f"path:{self.scope.resolve()}"
        for home in ("/just/a/path", 7, None, "path:" + "x" * 5000, ["path:/x"]):
            event = module.message_event("me", {"id": "18de6610f927c276-fe7820", "from": "x", "message": "y",
                                                "home": home}, None, None, scope)
            self.assertIs(event["foreign"], True, repr(home))
            self.assertNotIn("origin", event)
            self.assertEqual(event["reply"], 'filo send --to x --from me --message "..."')

    def test_a_registered_client_is_told_where_a_message_from_another_scope_came_from(self) -> None:
        record = add_gemini_adapter(self)
        env = {"GEMINI_SESSION": "gem-123456"}
        self.assertEqual(self.msg("register", "--as", "gemini", env=env).returncode, 0)
        sent = self.msg("send", "--to", "gemini", "--from", "stranger", "--scope", str(self.scope),
                        "--message", "do this", scope=self.elsewhere)
        self.assertEqual(sent.returncode, 0, sent.stderr)
        text = json.loads(record.read_text(encoding="utf-8"))[-1]
        self.assertIn(f"[filo] Message from stranger, sent from {self.elsewhere.resolve()} (another scope): do this", text)
        self.assertIn(f"--scope {self.elsewhere.resolve()} --message", text)
        self.msg("send", "--to", "gemini", "--from", "me", "--message", "inside")
        self.assertIn("[filo] Message from me: inside", json.loads(record.read_text(encoding="utf-8"))[-1])

    def test_text_for_a_client_without_a_listener(self) -> None:
        module = load_filo()
        scope = f"path:{self.scope.resolve()}"
        plain = module.compose_text("a", "b", "hi", scope, scope)
        self.assertEqual(plain, '[filo] Message from a: hi\n\nTo reply: filo send --to a --from b --message "..."')
        outside = module.compose_text("a", "b", "hi", "path:/some where", scope)
        self.assertIn("[filo] Message from a, sent from /some where (another scope): hi", outside)
        self.assertIn("filo send --to a --from b --scope '/some where' --message", outside)
        self.assertIn("sent from another scope", module.compose_text("a", "b", "hi", "nonsense", scope))
        self.assertEqual(module.compose_text("a", "b", "hi"), plain)

    def test_a_keyword_scope_shows_as_its_keyword(self) -> None:
        module = load_filo()
        self.assertEqual(module.display_scope("keyword:team"), "team")
        self.assertFalse(module.is_foreign("keyword:team", "/anywhere"))


class SessionScopeTests(RuntimeCase):
    """A registered session, or a sender that is a member of a scope, stays in that scope when its shell
    is in another directory, such as a git worktree of the project."""

    def setUp(self) -> None:
        super().setUp()
        self.worktree = self.scope / ".worktrees" / "feature"
        self.worktree.mkdir(parents=True)
        add_gemini_adapter(self)        # a client without a process of its own to look for: its registration stays
        self.me = {"GEMINI_SESSION": "gem-123456"}
        self.listen("boss")
        registered = self.msg("register", "--as", "gemini", "--name", "worker", env=self.me)
        self.assertEqual(registered.returncode, 0, registered.stderr)

    def test_a_registered_session_sends_from_a_worktree_without_saying_where_to(self) -> None:
        sent = self.msg("send", "--to", "boss", "--from", "worker", "--message", "hi", scope=self.worktree, env=self.me)
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertIn("sent directly to boss", sent.stdout)
        self.assertIn(f"working in {self.scope.resolve()}", sent.stderr)

    def test_who_status_and_leave_follow_it_too(self) -> None:
        who = self.msg("who", scope=self.worktree, env=self.me)
        self.assertIn(f"connected to {self.scope.resolve()}:", who.stdout)
        self.assertIn("boss", who.stdout)
        self.assertIn("worker", who.stdout)
        as_json = json.loads(self.msg("who", "--json", scope=self.worktree, env=self.me).stdout)
        self.assertEqual(as_json["scope"], str(self.scope.resolve()))
        sent = self.msg("send", "--to", "boss", "--from", "worker", "--message", "hi", scope=self.worktree, env=self.me)
        message_id = sent.stdout.split("(")[-1].rstrip(")\n")
        status = self.msg("status", message_id, scope=self.worktree, env=self.me)
        self.assertIn("delivered", status.stdout)
        left = self.msg("leave", scope=self.worktree, env=self.me)
        self.assertEqual(left.returncode, 0, left.stderr)
        self.assertNotIn("worker", self.msg("who").stdout)

    def test_a_name_that_is_a_member_of_no_scope_gets_nothing_inferred(self) -> None:
        stranger = self.msg("send", "--to", "boss", "--from", "nobody", "--message", "hi", scope=self.worktree)
        self.assertEqual(stranger.returncode, 1)
        self.assertIn("not connected", stranger.stderr)
        self.assertNotIn("working in", stranger.stderr)

    def test_a_sender_that_is_a_member_of_one_scope_is_found_by_its_name(self) -> None:
        sent = self.msg("send", "--to", "boss", "--from", "worker", "--message", "hi", scope=self.worktree)
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertIn("worker is there", sent.stderr)

    def test_a_name_in_two_scopes_is_not_guessed(self) -> None:
        other = self.root / "other"
        other.mkdir()
        self.listen("worker", scope=other)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        sent = self.msg("send", "--to", "boss", "--from", "worker", "--message", "hi", scope=elsewhere)
        self.assertEqual(sent.returncode, 1)
        self.assertIn("not connected", sent.stderr)

    def test_scope_always_wins_and_the_current_scope_is_kept_when_it_holds_the_session(self) -> None:
        other = self.root / "other"
        other.mkdir()
        self.listen("boss", scope=other)
        sent = self.msg("send", "--scope", str(other), "--to", "boss", "--from", "worker", "--message", "hi",
                        scope=self.worktree, env=self.me)
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertNotIn("working in", sent.stderr)
        inside = self.msg("who", env=self.me)                  # run in self.scope, which holds the session
        self.assertNotIn("working in", inside.stderr)

    def test_a_broken_adapters_file_does_not_stop_a_command_that_needs_none_of_it(self) -> None:
        broken = self.root / "adapters.json"
        broken.write_text("{ not json", encoding="utf-8")
        sent = self.msg("send", "--to", "boss", "--from", "worker", "--message", "hi", scope=self.worktree,
                        env={**self.me, "FILO_ADAPTERS": str(broken)})
        self.assertEqual(sent.returncode, 0, sent.stderr)


class WhoAllTests(RuntimeCase):
    def setUp(self) -> None:
        super().setUp()
        add_gemini_adapter(self)

    def test_every_scope_with_someone_in_it_is_listed_and_empty_ones_are_not(self) -> None:
        other = self.root / "other"
        other.mkdir()
        empty = self.root / "empty"
        empty.mkdir()
        self.listen("boss")
        self.listen("worker", scope=other)
        quiet, _, _ = self.listen("gone", scope=empty)
        quiet.terminate()
        quiet.wait(timeout=5)
        self.assertEqual(self.msg("register", "--as", "gemini", env={"GEMINI_SESSION": "gem-123456"}, scope=other).returncode, 0)
        listing = json.loads(self.msg("who", "--all", "--json").stdout)["scopes"]
        by_scope = {row["scope"]: row for row in listing}
        self.assertEqual(set(by_scope), {str(self.scope.resolve()), str(other.resolve())})
        self.assertEqual([row["name"] for row in by_scope[str(other.resolve())]["participants"]], ["worker"])
        self.assertEqual([row["name"] for row in by_scope[str(other.resolve())]["registered"]], ["gemini"])
        text = self.msg("who", "--all").stdout
        self.assertIn(f"{other.resolve()}:\n  worker  pid=", text)
        self.assertIn("gemini  gemini session=gem-123456  (registered)", text)

    def test_nobody_connected_says_so_and_all_does_not_take_a_scope(self) -> None:
        self.assertIn("nobody is connected", self.msg("who", "--all").stdout)
        self.assertEqual(json.loads(self.msg("who", "--all", "--json").stdout), {"scopes": []})
        refused = self.msg("who", "--all", "--scope", str(self.scope))
        self.assertEqual(refused.returncode, 1)
        self.assertIn("does not take --scope", refused.stderr)

    def test_the_file_that_names_a_scope_is_not_taken_for_a_participant(self) -> None:
        self.listen("boss")
        files = list(self.runtime.glob("*/.scope"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual([row["name"] for row in json.loads(self.msg("who", "--json").stdout)["participants"]], ["boss"])


class JoinedEventTests(RuntimeCase):
    def setUp(self) -> None:
        super().setUp()
        add_gemini_adapter(self)

    def test_participants_lists_registered_sessions_too(self) -> None:
        self.assertEqual(self.msg("register", "--as", "gemini", env={"GEMINI_SESSION": "gem-123456"}).returncode, 0)
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "join", "--as", "boss", "--json"], cwd=self.scope,
            env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.children.append(process)
        lines = Lines(process)
        self.assertEqual((lines.next() or {}).get("kind"), "joined")
        participants = lines.next()
        assert participants is not None
        self.assertEqual(participants["participants"], [])
        self.assertEqual(participants["registered"], [{"name": "gemini", "client": "gemini", "session": "gem-123456"}])


class DetachedListenerTests(RuntimeCase):
    """`join --detach` and `watch`: a listener that outlives the command that started it, and a reader
    that can come and go without the listener or the others noticing."""

    def parent(self) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(["sleep", "600"], stdout=subprocess.PIPE)
        self.children.append(process)
        return process

    def tearDown(self) -> None:
        # A detached listener is nobody's child: it notices that its parent is gone within half a second
        # and then writes its last files into the runtime directory. Wait for it, so that the directory
        # is not removed under it.
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)    # until it is reaped a parent still counts as a live process
        self.wait_until_gone()
        super().tearDown()

    def held(self) -> list[Path]:
        """The names that a listener still holds: each listener gives its lock back as the last thing it does."""
        found = []
        for lock_file in self.runtime.glob("*/*.lock"):
            with open(lock_file, "rb") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    found.append(lock_file)
        return found

    def wait_until_gone(self, seconds: float = 30.0) -> bool:
        deadline = time.monotonic() + seconds
        while self.held():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.1)
        return True

    def detach(self, name: str, parent: subprocess.Popen[bytes]) -> dict[str, object]:
        # run() waits for the end of the output: it would hang if the listener kept it open
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "join", "--as", name, "--detach", "--parent", str(parent.pid), "--json"],
            cwd=self.scope, env=self.environment, capture_output=True, text=True, check=False, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        joined = json.loads(result.stdout.splitlines()[0])
        self.assertEqual((joined["kind"], joined["name"], joined["detached"]), ("joined", name, True))
        return dict(joined)

    def watch(self, name: str, *extra: str) -> tuple[subprocess.Popen[bytes], Lines]:
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "watch", "--as", name, *extra], cwd=self.scope,
            env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.children.append(process)
        return process, Lines(process)

    def listening(self) -> list[str]:
        return [row["name"] for row in json.loads(self.msg("who", "--json").stdout)["participants"]]

    def wait_until(self, condition: Callable[[], bool], what: str) -> None:
        for _ in range(100):
            if condition():
                return
            time.sleep(0.1)
        self.fail(what)

    def test_the_listener_goes_on_after_the_command_returns_and_ends_with_its_parent(self) -> None:
        parent = self.parent()
        joined = self.detach("boss", parent)
        self.assertEqual(self.listening(), ["boss"])
        pid = int(str(joined["pid"]))
        self.assertNotEqual(pid, parent.pid)
        os.kill(pid, 0)                                 # alive, though the command is long gone
        self.assertEqual(os.getsid(pid), pid)            # in a session of its own
        parent.terminate()
        parent.wait(timeout=5)
        self.wait_until(lambda: self.listening() == [], "the listener did not end with its parent")

    def test_detach_needs_a_parent_that_is_running(self) -> None:
        missing = self.msg("join", "--as", "boss", "--detach")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("--parent", missing.stderr)
        parent = self.parent()
        parent.terminate()
        parent.wait(timeout=5)
        gone = self.msg("join", "--as", "boss", "--detach", "--parent", str(parent.pid))
        self.assertEqual(gone.returncode, 1)
        self.assertIn("not running", gone.stderr)

    def test_watch_reports_what_arrives_with_where_it_came_from(self) -> None:
        self.detach("boss", self.parent())
        _, lines = self.watch("boss", "--json")      # accepted, for symmetry with join
        time.sleep(0.5)
        self.assertEqual(self.msg("send", "--to", "boss", "--from", "me", "--message", "hello").returncode, 0)
        event = lines.message()
        assert event is not None
        self.assertEqual((event["from"], event["message"], event["to"]), ("me", "hello", "boss"))
        self.assertNotIn("foreign", event)
        self.assertNotIn("origin", event)
        self.assertIsInstance(event["peer_pid"], int)
        self.assertEqual(list(event)[-1], "message")

    def test_a_long_message_is_cut_by_preview_and_show_prints_it_whole(self) -> None:
        self.detach("boss", self.parent())
        _, lines = self.watch("boss", "--preview", "400")
        time.sleep(0.5)
        body = "Do this first. " + "More text. " * 200 + "THE END"
        self.msg("send", "--to", "boss", "--from", "me", "--stdin", stdin=body)
        event = lines.message()
        assert event is not None
        self.assertIs(event["truncated"], True)
        self.assertNotIn("THE END", json.dumps(event))
        self.assertEqual(self.msg("show", str(event["id"])).stdout, body + "\n")

    def test_a_watch_that_starts_again_prints_only_what_it_missed(self) -> None:
        self.detach("boss", self.parent())
        first, lines = self.watch("boss")
        time.sleep(0.5)
        self.msg("send", "--to", "boss", "--from", "me", "--message", "one")
        seen = lines.message()
        assert seen is not None
        # A watch prints a message and then notes it; one that is stopped in between prints it again
        # on its return (it never loses one). Wait for the note, so that the test is about the usual case.
        self.wait_until(lambda: any(self.runtime.glob("*/inbox/boss/.cursor")), "the watch did not note it")
        first.terminate()
        first.wait(timeout=5)
        self.msg("send", "--to", "boss", "--from", "me", "--message", "two")
        self.msg("send", "--to", "boss", "--from", "me", "--message", "three")
        _, again = self.watch("boss")
        self.assertEqual([(again.message() or {}).get("message"), (again.message(2) or {}).get("message")],
                         ["two", "three"])
        self.assertIsNone(again.message(1), "a message was printed twice")

    def test_a_message_with_an_older_id_that_arrives_later_is_not_skipped(self) -> None:
        # The id is made by the sender before it delivers: one that is slow (or that goes through the
        # spool) can arrive after a message that was sent later, and it must not be taken for old.
        self.detach("boss", self.parent())
        _, lines = self.watch("boss")
        time.sleep(0.5)
        older = f"{time.time_ns():x}-aaaaaa"
        time.sleep(0.05)
        self.msg("send", "--to", "boss", "--from", "me", "--message", "sent second, arrived first")
        first = lines.message()
        assert first is not None
        self.assertEqual(first["message"], "sent second, arrived first")
        self.wait_until(lambda: any(self.runtime.glob("*/inbox/boss/.cursor")), "the watch did not note it")
        scope_directory = next(self.runtime.glob("*/boss.json")).parent
        spool = scope_directory / "spool" / "boss"
        spool.mkdir(parents=True, mode=0o700, exist_ok=True)
        (spool / f"{older}.msg").write_text(json.dumps({
            "id": older, "from": "me", "message": "sent first, arrived last", "created_ns": time.time_ns()}),
            encoding="utf-8")
        late = lines.message(5)
        assert late is not None
        self.assertEqual(late["message"], "sent first, arrived last")

    def test_a_message_is_printed_when_the_clock_of_the_system_went_back_after_the_listener_started(self) -> None:
        # What the listener stamps as its start and as the arrival of a message are two readings of a clock that
        # can be set back (a time service does it), and then the message looks older than the listener.
        self.detach("boss", self.parent())
        [metadata_file] = self.runtime.glob("*/boss.json")
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        metadata["started_ns"] = time.time_ns() + 3_600 * 1_000_000_000
        metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
        _, lines = self.watch("boss")
        time.sleep(0.5)
        self.msg("send", "--to", "boss", "--from", "me", "--message", "after the clock went back")
        shown = lines.message()
        assert shown is not None
        self.assertEqual(shown["message"], "after the clock went back")

    def test_a_restarted_watch_does_not_skip_what_the_clock_stamped_earlier_than_its_cursor(self) -> None:
        self.detach("boss", self.parent())
        first, lines = self.watch("boss")
        time.sleep(0.5)
        self.msg("send", "--to", "boss", "--from", "me", "--message", "one")
        self.assertIsNotNone(lines.message())
        self.wait_until(lambda: any(self.runtime.glob("*/inbox/boss/.cursor")), "the watch did not note it")
        first.terminate()
        first.wait(timeout=5)
        [cursor_file] = self.runtime.glob("*/inbox/boss/.cursor")
        cursor = json.loads(cursor_file.read_text(encoding="utf-8"))
        cursor["last_ns"] = time.time_ns() + 3_600 * 1_000_000_000    # the clock was set back since
        cursor_file.write_text(json.dumps(cursor), encoding="utf-8")
        self.msg("send", "--to", "boss", "--from", "me", "--message", "two")
        _, again = self.watch("boss")
        shown = again.message()
        assert shown is not None
        self.assertEqual(shown["message"], "two")
        self.assertIsNone(again.message(timeout=1), "what the first watch printed came again")

    def test_a_record_from_a_filo_that_numbered_nothing_is_still_printed(self) -> None:
        self.detach("boss", self.parent())
        [metadata_file] = self.runtime.glob("*/boss.json")
        inbox = metadata_file.parent / "inbox" / "boss"
        inbox.mkdir(parents=True, mode=0o700, exist_ok=True)
        (inbox / "18df0000000000aa-bbbbbb.json").write_text(json.dumps({
            "id": "18df0000000000aa-bbbbbb", "from": "old", "to": "boss", "message": "from before the numbers",
            "created_ns": time.time_ns(), "received_ns": time.time_ns()}), encoding="utf-8")
        _, lines = self.watch("boss")
        shown = lines.message()
        assert shown is not None
        self.assertEqual(shown["message"], "from before the numbers")

    def test_a_cursor_written_by_an_older_filo_is_still_understood(self) -> None:
        joined = self.detach("boss", self.parent())
        first, lines = self.watch("boss")
        time.sleep(0.5)
        self.msg("send", "--to", "boss", "--from", "me", "--message", "one")
        seen = lines.message()
        assert seen is not None
        self.wait_until(lambda: any(self.runtime.glob("*/inbox/boss/.cursor")), "the watch did not note it")
        first.terminate()
        first.wait(timeout=5)
        cursor = next(self.runtime.glob("*/inbox/boss/.cursor"))
        cursor.write_text(json.dumps({"pid": joined["pid"], "last": seen["id"]}), encoding="utf-8")
        self.msg("send", "--to", "boss", "--from", "me", "--message", "two")
        _, again = self.watch("boss")
        self.assertEqual((again.message() or {}).get("message"), "two")
        self.assertIsNone(again.message(1), "a message was printed twice")

    def test_the_messages_of_an_earlier_listener_are_not_printed_to_the_next_one(self) -> None:
        old_parent = self.parent()
        self.detach("boss", old_parent)
        self.msg("send", "--to", "boss", "--from", "me", "--message", "for the old one")
        old_parent.terminate()
        old_parent.wait(timeout=5)
        self.wait_until(lambda: self.listening() == [], "the first listener did not end")
        self.detach("boss", self.parent())
        _, lines = self.watch("boss")
        self.assertIsNone(lines.message(1.5))

    def test_watch_says_when_the_listener_is_gone_and_refuses_one_that_is_not_there(self) -> None:
        refused = self.msg("watch", "--as", "nobody")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("nobody called", refused.stderr)
        parent = self.parent()
        self.detach("boss", parent)
        process, lines = self.watch("boss")
        time.sleep(0.5)
        parent.terminate()
        parent.wait(timeout=5)
        ended = lines.next(8)
        assert ended is not None
        self.assertEqual((ended["kind"], ended["name"]), ("ended", "boss"))
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)

    def test_listeners_that_join_at_the_same_time_get_different_names(self) -> None:
        # Sessions that are started together (a few agents in one terminal multiplexer) all ask for the
        # same default name, and each must end up with one that is its own.
        parent = self.parent()
        processes = [subprocess.Popen(
            [sys.executable, str(SCRIPT), "join", "--as", "same", "--detach", "--parent", str(parent.pid), "--json"],
            cwd=self.scope, env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(8)]
        names: list[str] = []
        for process in processes:
            out, err = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, err)
            names.append(json.loads(out.splitlines()[0])["name"])
        self.assertEqual(len(set(names)), 8, names)
        self.assertEqual(sorted(self.listening()), sorted(names))
        for name in names:    # the name is held by the listener itself, not by the command that started it
            [lock_file] = self.runtime.glob(f"*/{name}.lock")
            with open(lock_file, "rb") as handle, self.assertRaises(BlockingIOError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_listeners_that_end_together_do_not_wait_for_each_other(self) -> None:
        # Each one used to tell the others that it was leaving before it went, and those that were leaving
        # too did not answer: eight of them took 14 seconds, of one second for each request.
        parent = self.parent()
        for _ in range(8):
            started = self.msg("join", "--as", "same", "--detach", "--parent", str(parent.pid))
            self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(len(self.listening()), 8)
        parent.terminate()
        parent.wait(timeout=5)
        started = time.monotonic()
        self.assertTrue(self.wait_until_gone(30))
        self.assertLess(time.monotonic() - started, 8.0)

    def test_a_listener_with_a_full_queue_of_connections_is_busy_and_not_gone(self) -> None:
        process, _, _ = self.listen("rx")
        [socket_file] = self.runtime.glob("*/rx.sock")
        process.send_signal(signal.SIGSTOP)
        crowd: list[socket.socket] = []
        try:
            for _ in range(200):    # it accepts nobody: the queue fills, and then a connect fails at once
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.settimeout(0.0)
                crowd.append(connection)
                try:
                    connection.connect(str(socket_file))
                except OSError:
                    break
            self.assertEqual(json.loads(self.msg("who", "--json").stdout)["participants"][0]["name"], "rx")
            self.assertTrue(socket_file.exists())
            self.assertTrue(socket_file.with_suffix(".json").exists())
        finally:
            for connection in crowd:
                connection.close()
            process.send_signal(signal.SIGCONT)

    def test_ctrl_c_ends_watch_quietly(self) -> None:
        self.detach("boss", self.parent())
        process, _ = self.watch("boss")
        time.sleep(0.5)
        process.send_signal(signal.SIGINT)
        self.assertEqual(process.wait(timeout=5), 130)

    def test_a_parent_that_is_not_a_process_is_refused(self) -> None:
        # kill(0, 0) and kill(-1, 0) succeed, so such a "parent" would never end and the listener with it
        for parent in ("0", "-1"):
            for extra in ((), ("--detach",)):
                refused = self.msg("join", "--as", "boss", "--parent", parent, *extra)
                self.assertEqual(refused.returncode, 1, (parent, extra, refused.stderr))
                self.assertIn("--parent", refused.stderr)
        self.assertEqual(self.listening(), [])

    def test_leave_stops_a_detached_listener(self) -> None:
        self.detach("boss", self.parent())
        left = self.msg("leave", "--name", "boss")
        self.assertEqual(left.returncode, 0, left.stderr)
        self.wait_until(lambda: self.listening() == [], "the listener did not leave")
        self.assertEqual(self.msg("leave", "--name", "boss").returncode, 1)

    def test_a_detached_listener_prints_no_presence_of_its_own(self) -> None:
        self.detach("boss", self.parent())
        _, lines = self.watch("boss")
        other, _, _ = self.listen("other")            # joins, which would be a presence event for a classic listener
        other.terminate()
        other.wait(timeout=5)
        self.assertIsNone(lines.next(1.5), "a presence notice reached the watcher")


class GetScriptTests(unittest.TestCase):
    """get.sh, as the release workflow stamps it, against an archive on disk."""

    def setUp(self) -> None:
        self.base = tempfile.TemporaryDirectory()
        root = Path(self.base.name)
        self.release = root / "release"
        self.release.mkdir()
        tree = root / "filo-9.9.9"
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "__pycache__", "tests"))
        self.archive = self.release / "filo-9.9.9.tar.gz"
        subprocess.run(["tar", "-czf", str(self.archive), "-C", str(root), "filo-9.9.9"], check=True)
        self.sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.home = root / "data" / "filo"
        self.bin = root / "bin"
        self.environment = {
            **os.environ,
            "HOME": str(root / "home"),
            "FILO_HOME": str(self.home),
            "FILO_RELEASE_URL": f"file://{self.release}",
            "FILO_BIN_DIR": str(self.bin),
            "FILO_CLAUDE_SKILLS": str(root / "claude" / "skills"),
            "FILO_AGENTS_SKILLS": str(root / "agents" / "skills"),
            "FILO_AGY_SKILLS": str(root / "agy" / "skills"),
            "FILO_OPENCODE_DIR": str(root / "opencode"),
        }
        (root / "home").mkdir()
        self.script = self.stamp(version="9.9.9", sha=self.sha)

    def tearDown(self) -> None:
        self.base.cleanup()

    def stamp(self, version: str, sha: str, name: str = "get.sh") -> Path:
        text = (ROOT / "get.sh").read_text(encoding="utf-8")
        text = text.replace('version="@VERSION@"', f'version="{version}"').replace('sha256="@SHA256@"', f'sha256="{sha}"')
        path = Path(self.base.name) / name
        path.write_text(text, encoding="utf-8")
        return path

    def get(self, *args: str, script: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([SHELL, str(script or self.script), *args], env=self.environment,
                              text=True, capture_output=True, check=False, timeout=60)

    def test_a_directory_named_with_a_slash_or_a_dot_at_its_end_is_the_same_directory(self) -> None:
        for ending in ("/", "/.", "//"):
            with self.subTest(ending=ending):
                self.environment["FILO_HOME"] = f"{self.home}{ending}"
                result = self.get()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((self.home / ".filo-release").is_file())
                self.assertEqual(self.get("uninstall").returncode, 0)
                self.assertFalse(self.home.exists())

    def test_a_directory_with_dots_in_it_is_refused(self) -> None:
        self.environment["FILO_HOME"] = f"{self.home}/../elsewhere"
        refused = self.get()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("must not have ..", refused.stdout + refused.stderr)

    def test_a_version_that_is_not_three_numbers_is_refused(self) -> None:
        for version in ("1.2.3/../x", "1.2.3.4", "1..3", ".1.2", "1.2.3."):
            with self.subTest(version=version):
                refused = self.get(script=self.stamp(version=version, sha=self.sha, name="odd.sh"))
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("not made by a release", refused.stdout + refused.stderr)

    def test_install_checks_the_hash_unpacks_and_links_and_uninstall_removes_it_all(self) -> None:
        result = self.get()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("checked filo-9.9.9.tar.gz", result.stdout)
        self.assertTrue((self.home / "filo.py").is_file())
        self.assertEqual((self.home / ".filo-release").read_text(encoding="utf-8"),
                         f"version=9.9.9\nsha256={self.sha}\n")
        self.assertEqual((self.bin / "filo").resolve(), (self.home / "filo.py").resolve())
        again = self.get()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("already linked", again.stdout)
        self.assertEqual([path.name for path in self.home.parent.iterdir()], ["filo"])
        removed = self.get("uninstall")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(self.home.exists())
        self.assertFalse((self.bin / "filo").is_symlink())

    def test_a_dry_run_says_what_it_would_do_and_changes_nothing(self) -> None:
        dry = self.get("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        for expected in (f"file://{self.release}/filo-9.9.9.tar.gz", self.sha, str(self.home), "never uses sudo",
                         "Dry run: nothing was changed"):
            self.assertIn(expected, dry.stdout)
        self.assertFalse(self.home.parent.exists())
        self.assertFalse(self.bin.exists())
        self.assertEqual(self.get().returncode, 0)
        self.assertIn("Dry run", self.get("uninstall", "--dry-run").stdout)
        self.assertTrue((self.home / "filo.py").is_file())
        self.assertTrue((self.bin / "filo").is_symlink())

    def test_an_archive_with_the_wrong_hash_is_refused_and_leaves_nothing(self) -> None:
        wrong = self.stamp(version="9.9.9", sha="0" * 64, name="wrong.sh")
        result = self.get(script=wrong)
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not the release", result.stderr)
        self.assertFalse(self.home.exists())
        self.assertEqual([path.name for path in self.home.parent.iterdir()] if self.home.parent.exists() else [], [])
        self.assertFalse(self.bin.exists())

    def test_a_copy_that_was_not_stamped_by_a_release_is_refused(self) -> None:
        result = self.get(script=ROOT / "get.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not made by a release", result.stderr)
        self.assertFalse(self.home.exists())

    def test_a_directory_that_is_not_ours_is_neither_replaced_nor_deleted(self) -> None:
        self.home.mkdir(parents=True)
        (self.home / "mine.txt").write_text("keep", encoding="utf-8")
        for action in ((), ("uninstall",)):
            result = self.get(*action)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertEqual((self.home / "mine.txt").read_text(encoding="utf-8"), "keep")
        self.assertFalse(self.bin.exists())

    def test_it_refuses_a_directory_it_should_never_delete(self) -> None:
        for bad in ("relative/dir", "/", self.environment["HOME"]):
            self.environment["FILO_HOME"] = bad
            result = self.get("uninstall")
            self.assertEqual(result.returncode, 1, bad)
            self.assertTrue(Path(self.environment["HOME"]).is_dir())
        link = Path(self.base.name) / "link"
        link.symlink_to(self.release)
        self.environment["FILO_HOME"] = str(link)
        self.assertEqual(self.get("uninstall").returncode, 1)
        self.assertTrue(self.archive.is_file())

    def test_only_https_is_accepted_for_a_download(self) -> None:
        self.environment["FILO_RELEASE_URL"] = "http://example.invalid/releases"
        result = self.get()
        self.assertEqual(result.returncode, 1)
        self.assertIn("only https://", result.stderr)

    def test_unknown_options_are_refused(self) -> None:
        self.assertEqual(self.get("--force").returncode, 2)

    def test_the_stamp_lines_are_the_ones_the_release_workflow_replaces(self) -> None:
        text = (ROOT / "get.sh").read_text(encoding="utf-8")
        self.assertIn('\nversion="@VERSION@"\n', text)
        self.assertIn('\nsha256="@SHA256@"\n', text)
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("@VERSION@", workflow)
        self.assertIn("@SHA256@", workflow)


class ReleaseServer:
    """A stand-in for github.com/Galfurian/filo/releases on 127.0.0.1."""

    def __init__(self, latest: str, scripts: dict[str, str]) -> None:
        self.latest = latest
        self.scripts = scripts
        self.hits: list[str] = []
        self.methods: list[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def answer(self, send_body: bool) -> None:
                outer.hits.append(self.path)
                outer.methods.append(self.command)
                if self.path == "/releases/latest":
                    self.send_response(302)
                    self.send_header("Location", f"/releases/tag/v{outer.latest}")
                    self.end_headers()
                    return
                match = re.fullmatch(r"/releases/download/v([0-9.]+)/get\.sh", self.path)
                body = b"release page"
                if match is not None:
                    if match.group(1) not in outer.scripts:
                        self.send_error(404)
                        return
                    body = outer.scripts[match.group(1)].encode("utf-8")
                elif not self.path.startswith("/releases/tag/"):
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if send_body:
                    self.wfile.write(body)

            def do_GET(self) -> None:
                self.answer(True)

            def do_HEAD(self) -> None:
                self.answer(False)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/releases"

    def close(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()


class UpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.TemporaryDirectory()
        self.root = Path(self.base.name)
        self.release = self.root / "release"
        self.release.mkdir()
        self.tree = self.root / "tree"
        shutil.copytree(ROOT, self.tree, ignore=shutil.ignore_patterns(".git", "__pycache__", "tests"))
        self.stamped: dict[str, str] = {}
        for version in ("9.9.8", "9.9.9"):
            package = self.root / f"filo-{version}"
            shutil.copytree(self.tree, package)
            archive = self.release / f"filo-{version}.tar.gz"
            subprocess.run(["tar", "-czf", str(archive), "-C", str(self.root), package.name], check=True)
            sha = hashlib.sha256(archive.read_bytes()).hexdigest()
            text = (ROOT / "get.sh").read_text(encoding="utf-8")
            self.stamped[version] = text.replace('version="@VERSION@"', f'version="{version}"').replace(
                'sha256="@SHA256@"', f'sha256="{sha}"')
        (self.root / "home").mkdir()
        self.home, self.bin = self.root / "data" / "filo", self.root / "bin"
        self.server = ReleaseServer("9.9.9", self.stamped)
        self.addCleanup(self.server.close)
        self.addCleanup(self.base.cleanup)
        self.environment = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "FILO_DIR": str(self.root / "runtime"),
            "FILO_HOME": str(self.home),
            "FILO_RELEASE_URL": f"file://{self.release}",
            "FILO_RELEASES_URL": self.server.url,
            "FILO_BIN_DIR": str(self.bin),
            "FILO_CLAUDE_SKILLS": str(self.root / "claude" / "skills"),
            "FILO_AGENTS_SKILLS": str(self.root / "agents" / "skills"),
            "FILO_AGY_SKILLS": str(self.root / "agy" / "skills"),
            "FILO_OPENCODE_DIR": str(self.root / "opencode"),
        }
        self.environment.pop("FILO_CHECK_UPDATES", None)

    def install_old_release(self) -> None:
        script = self.root / "get-old.sh"
        script.write_text(self.stamped["9.9.8"], encoding="utf-8")
        result = subprocess.run(["sh", str(script)], env=self.environment, text=True, capture_output=True,
                                check=False, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("version=9.9.8", (self.home / RELEASE_MARKER).read_text(encoding="utf-8"))

    def filo(self, *args: str, command: Path | None = None, env: dict[str, str] | None = None,
             stdin: int | None = subprocess.DEVNULL) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(command or self.bin / "filo"), *args], env=env or self.environment,
                              text=True, capture_output=True, check=False, timeout=60, stdin=stdin)

    def test_the_check_for_a_new_version_only_asks_with_head_all_the_way(self) -> None:
        # Before Python 3.13 a redirect turned the HEAD into a GET, and the page of the release was read
        self.install_old_release()
        self.server.methods.clear()
        self.assertEqual(self.filo("upgrade", "--check").returncode, 0)
        self.assertEqual(set(self.server.methods), {"HEAD"}, self.server.hits)

    def test_a_server_that_does_not_speak_http_is_an_error_and_not_a_traceback(self) -> None:
        self.install_old_release()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        self.addCleanup(listener.close)

        def answer() -> None:
            while True:
                try:
                    connection, _ = listener.accept()
                except OSError:
                    return
                with connection:
                    connection.recv(4096)
                    connection.sendall(b"not http at all\r\n\r\n")

        threading.Thread(target=answer, daemon=True).start()
        env = {**self.environment, "FILO_RELEASES_URL": f"http://127.0.0.1:{listener.getsockname()[1]}"}
        result = self.filo("upgrade", "--check", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not fetch", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_upgrade_finds_its_directory_through_a_link_to_the_link(self) -> None:
        self.install_old_release()
        (self.root / "other").mkdir()
        (self.root / "other" / "filo").symlink_to(self.bin / "filo")
        result = self.filo("upgrade", "--yes", command=self.root / "other" / "filo")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Dry run: nothing was changed", result.stdout)

    def test_upgrade_installs_the_newest_release_through_the_link_the_installer_made(self) -> None:
        self.install_old_release()
        checked = self.filo("upgrade", "--check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("9.9.9 is available", checked.stdout)
        self.assertIn("version=9.9.8", (self.home / RELEASE_MARKER).read_text(encoding="utf-8"))
        result = self.filo("upgrade", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Dry run: nothing was changed", result.stdout)
        self.assertIn("version=9.9.9", (self.home / RELEASE_MARKER).read_text(encoding="utf-8"))
        self.assertEqual(os.readlink(self.bin / "filo"), str(self.home / "filo.py"))
        self.assertEqual([path.name for path in self.home.parent.iterdir()], ["filo"])

    def test_upgrade_does_not_go_ahead_without_a_terminal_or_an_answer(self) -> None:
        self.install_old_release()
        result = self.filo("upgrade")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--yes", result.stderr)
        self.assertIn("version=9.9.8", (self.home / RELEASE_MARKER).read_text(encoding="utf-8"))

    def test_there_is_nothing_to_do_when_this_is_the_newest_release(self) -> None:
        self.server.latest = self.version()
        result = self.filo("upgrade", command=SCRIPT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("is the latest version", result.stdout)
        self.assertEqual(self.server.hits.count("/releases/latest"), 1)

    def test_a_get_script_stamped_for_another_version_is_not_run(self) -> None:
        self.install_old_release()
        self.server.scripts["9.9.9"] = self.stamped["9.9.8"]
        result = self.filo("upgrade", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not stamped for 9.9.9", result.stderr)
        self.assertIn("version=9.9.8", (self.home / RELEASE_MARKER).read_text(encoding="utf-8"))

    def test_a_checkout_is_pointed_to_git_and_a_stray_copy_is_refused(self) -> None:
        (self.tree / ".git").mkdir()
        result = self.filo("upgrade", command=self.tree / "filo.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("git -C", result.stdout)
        shutil.rmtree(self.tree / ".git")
        stray = self.filo("upgrade", command=self.tree / "filo.py")
        self.assertEqual(stray.returncode, 1)
        self.assertIn("not installed by get.sh", stray.stderr)

    def test_a_releases_address_that_is_not_https_is_refused(self) -> None:
        self.environment["FILO_RELEASES_URL"] = "http://example.invalid/releases"
        result = self.filo("upgrade", "--check", command=SCRIPT)
        self.assertEqual(result.returncode, 1)
        self.assertIn("must be an https:// URL", result.stderr)

    def test_an_address_is_judged_by_its_host_and_not_by_how_it_begins(self) -> None:
        secure = load_filo().secure_url
        for good in ("https://github.com/Galfurian/filo/releases", "http://127.0.0.1:8000/x", "http://localhost:9/x"):
            self.assertTrue(secure(good), good)
        for bad in ("http://example.invalid/x", "http://127.0.0.1/x", "ftp://127.0.0.1:21/x",
                    "http://127.0.0.1:80@evil.example/x", "http://localhost:1@evil.example/",
                    "http://127.0.0.1:@evil.example/", "https://github.com@evil.example/x",
                    "http://127.0.0.1.evil.example:80/x", "http://[::1]:80/x", "https:///x", "http://localhost:99999/x",
                    "https://", ""):
            self.assertFalse(secure(bad), bad)

    def test_an_unreachable_server_is_an_error_for_upgrade_and_silent_for_the_notice(self) -> None:
        self.server.close()
        result = self.filo("upgrade", "--check", command=SCRIPT)
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not fetch", result.stderr)
        self.environment["FILO_CHECK_UPDATES"] = "1"
        who = self.filo("who", "--scope", "x", command=SCRIPT)
        self.assertEqual(who.returncode, 0, who.stderr)
        self.assertNotIn("available", who.stderr)
        cached = (self.root / "cache" / "filo" / "latest-version").read_text(encoding="utf-8").strip()
        self.assertEqual(cached, self.version())

    def version(self) -> str:
        match = re.search(r'(?m)^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert match is not None
        return match.group(1)

    def test_the_notice_is_opt_in_once_a_day_and_never_in_json(self) -> None:
        quiet = self.filo("who", "--scope", "x", command=SCRIPT)
        self.assertEqual((quiet.returncode, quiet.stderr, self.server.hits), (0, "", []))
        self.environment["FILO_CHECK_UPDATES"] = "1"
        first = self.filo("who", "--scope", "x", command=SCRIPT)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("filo 9.9.9 is available", first.stderr)
        self.assertIn("filo upgrade", first.stderr)
        self.assertEqual(self.server.hits.count("/releases/latest"), 1)
        again = self.filo("who", "--scope", "x", command=SCRIPT)
        self.assertIn("filo 9.9.9 is available", again.stderr)
        as_json = self.filo("who", "--scope", "x", "--json", command=SCRIPT)
        self.assertEqual(as_json.stderr, "")
        self.assertEqual(self.server.hits.count("/releases/latest"), 1)

    def test_the_marker_name_is_the_one_get_sh_writes(self) -> None:
        self.assertIn(f"marker={RELEASE_MARKER}\n", (ROOT / "get.sh").read_text(encoding="utf-8"))


class PeerCheckTests(unittest.TestCase):
    """The listener answers connections from this user only."""

    def answer(self, peer_uid: int) -> dict[str, object]:
        import importlib.util
        from unittest import mock

        spec = importlib.util.spec_from_file_location("filo_under_test", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as runtime, mock.patch.dict(os.environ, {"FILO_DIR": runtime}):
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener = module.Listener("keyword:peer-check", "claude", server, True)
            client, connection = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.sendall(module.frame({"op": "info"}))
                with mock.patch.object(module, "peer_credentials", return_value=(4242, peer_uid)):
                    listener.handle(connection)
                return dict(json.loads(client.recv(4096)))
            finally:
                client.close()
                connection.close()
                server.close()

    def test_a_peer_of_another_user_is_refused(self) -> None:
        reply = self.answer(os.getuid() + 1)
        self.assertEqual(reply["ok"], False)
        self.assertIn("another user", str(reply["error"]))

    def test_a_peer_of_the_same_user_is_answered(self) -> None:
        self.assertEqual(self.answer(os.getuid())["ok"], True)


class SizeTests(RuntimeCase):
    """A message that filo accepts has to be one it can also keep and deliver again."""

    BODY = "漢" * 1_000_000    # 3 MB as UTF-8, 6 MB once escaped for a file

    def test_a_big_non_ascii_message_can_be_shown_after_it_is_received(self) -> None:
        _, lines, _ = self.listen("rx")
        self.assertEqual(self.msg("send", "--to", "rx", "--from", "tx", "--stdin", stdin=self.BODY).returncode, 0)
        event = lines.message(timeout=20)
        assert event is not None
        shown = self.msg("show", str(event["id"]))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(shown.stdout, self.BODY + "\n")

    def test_a_big_non_ascii_message_that_waited_is_delivered(self) -> None:
        process, _, _ = self.listen("rx")
        process.terminate()
        process.wait(timeout=5)
        sent = self.msg("send", "--to", "rx", "--from", "tx", "--stdin", stdin=self.BODY)
        self.assertEqual(sent.returncode, 0, sent.stderr)
        _, lines, _ = self.listen("rx")
        event = lines.message(timeout=20)
        assert event is not None
        self.assertEqual(event["message"], self.BODY)

    def test_a_message_too_big_to_carry_is_refused_and_nothing_is_queued(self) -> None:
        _, lines, _ = self.listen("rx")
        sent = self.msg("send", "--to", "rx", "--from", "tx", "--stdin", stdin="x" * (4 * 1024 * 1024))
        self.assertEqual(sent.returncode, 1)
        self.assertIn("at most", sent.stderr)
        self.assertEqual(list(self.runtime.glob("*/spool/*/*.msg")), [])
        self.assertIsNone(lines.message(timeout=1))
        fits = self.msg("send", "--to", "rx", "--from", "tx", "--stdin", stdin="x" * (4 * 1024 * 1024 - 100_000))
        self.assertEqual(fits.returncode, 0, fits.stderr)


class TextTests(RuntimeCase):
    """A message is text. What cannot be carried as text is refused, and what arrives twice is shown once."""

    def send_bytes(self, *args: bytes, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "send", "--to", "rx", "--from", "tx", *args],
            cwd=self.scope, env=self.environment, input=stdin, capture_output=True, check=False,
        )

    def test_a_message_that_is_not_utf8_is_refused_and_nothing_is_queued(self) -> None:
        _, lines, _ = self.listen("rx")
        sent = self.send_bytes(b"--message", b"bad\xffbyte")
        self.assertEqual(sent.returncode, 1, sent.stdout)
        self.assertIn(b"UTF-8", sent.stderr)
        self.assertNotIn(b"Traceback", sent.stderr)
        self.assertEqual(list(self.runtime.glob("*/spool/*/*.msg")), [])
        self.assertIsNone(lines.message(timeout=1))

    def test_a_message_on_standard_input_that_is_not_utf8_is_an_error_and_not_a_traceback(self) -> None:
        _, lines, _ = self.listen("rx")
        sent = self.send_bytes(b"--stdin", stdin=b"bad\xffbyte")
        self.assertEqual(sent.returncode, 1, sent.stdout)
        self.assertIn(b"UTF-8", sent.stderr)
        self.assertNotIn(b"Traceback", sent.stderr)
        self.assertIsNone(lines.message(timeout=1))

    def test_a_message_that_arrives_twice_is_shown_once(self) -> None:
        # A sender whose wait for the answer ran out sends again through the spool: the same id comes twice.
        _, lines, _ = self.listen("rx")
        paths = list(self.runtime.glob("*/rx.sock"))
        self.assertEqual(len(paths), 1)
        wire = json.dumps({"op": "message", "id": "0123456789abcdef-abcdef", "from": "tx", "message": "once"}) + "\n"
        for _ in range(2):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(5)
                connection.connect(str(paths[0]))
                connection.sendall(wire.encode())
                self.assertIn(b'"ok": true', connection.recv(4096).replace(b'":true', b'": true'))
        first = lines.message(timeout=5)
        assert first is not None
        self.assertEqual(first["message"], "once")
        self.assertIsNone(lines.message(timeout=1), "the same message was shown a second time")


class ErrorTests(RuntimeCase):
    """What goes wrong is said in a line, and an adapter whose process has a long name is still found."""

    def adapter(self, **fields: object) -> Path:
        config = self.root / "adapters.json"
        config.write_text(json.dumps({"gemini": {"id_env": "GEMINI_SESSION", **fields}}), encoding="utf-8")
        self.environment["FILO_ADAPTERS"] = str(config)
        return config

    def tool(self, body: str) -> Path:
        tool = self.root / "tool"
        tool.write_text(f"#!/usr/bin/env python3\nimport sys\n{body}\n", encoding="utf-8")
        tool.chmod(0o755)
        return tool

    def test_an_adapter_whose_process_has_a_long_name_keeps_its_registration(self) -> None:
        if not Path("/proc").is_dir():
            self.skipTest("the 15 characters are the kernel's limit in /proc; lsof is used elsewhere")
        name = "my-long-agent-binary"    # more than the 15 characters that the kernel keeps
        program = self.root / name
        sleep = shutil.which("sleep")
        assert sleep is not None
        shutil.copy(sleep, program)
        client = subprocess.Popen([str(program), "120"], cwd=self.scope)
        self.addCleanup(client.wait)
        self.addCleanup(client.terminate)
        time.sleep(0.2)
        self.adapter(process=name, deliver=["true"])
        registered = self.msg("register", "--as", "gemini", env={"GEMINI_SESSION": "gem-123456"})
        self.assertEqual(registered.returncode, 0, registered.stderr)
        for _ in range(2):    # the first look used to remove it
            who = json.loads(self.msg("who", "--json").stdout)
            self.assertEqual([row["name"] for row in who["registered"]], ["gemini"])

    def test_a_message_with_a_nul_is_an_error_for_a_delivery_that_cannot_take_it(self) -> None:
        self.adapter(deliver=[str(self.tool("")), "{message}"])
        self.assertEqual(self.msg("register", "--as", "gemini", env={"GEMINI_SESSION": "gem-123456"}).returncode, 0)
        sent = self.msg("send", "--to", "gemini", "--from", "claude", "--stdin", stdin="a\0b")
        self.assertEqual(sent.returncode, 1, sent.stdout)
        self.assertIn("failed", sent.stderr)
        self.assertNotIn("Traceback", sent.stderr)

    def test_output_that_is_not_text_does_not_hide_why_a_delivery_failed(self) -> None:
        self.adapter(deliver=[str(self.tool("sys.stderr.buffer.write(b'no \\xff\\xfe good'); sys.exit(3)"))])
        self.assertEqual(self.msg("register", "--as", "gemini", env={"GEMINI_SESSION": "gem-123456"}).returncode, 0)
        sent = self.msg("send", "--to", "gemini", "--from", "claude", "hello")
        self.assertEqual(sent.returncode, 1, sent.stdout)
        self.assertIn("no", sent.stderr)
        self.assertIn("good", sent.stderr)
        self.assertNotIn("Traceback", sent.stderr)

    def test_a_parent_that_no_process_can_have_is_refused(self) -> None:
        for extra in ((), ("--detach",)):
            refused = self.msg("join", "--as", "boss", "--parent", "99999999999999999999", *extra)
            self.assertEqual(refused.returncode, 1, (extra, refused.stderr))
            self.assertIn("--parent", refused.stderr)
            self.assertNotIn("Traceback", refused.stderr)
