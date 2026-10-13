from __future__ import annotations

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
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "filo.py"


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
        self.state = base / "state_5.sqlite"
        self.environment = {
            **os.environ,
            "FILO_DIR": str(self.runtime),
            "FILO_CODEX_HOME": str(base),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
        }

    def tearDown(self) -> None:
        for process in self.live:
            process.terminate()
            process.wait(timeout=5)
        self.base.cleanup()

    def start_codex(self, count: int = 1, cwd: Path | None = None) -> None:
        """Run ``count`` processes that look like Codex sessions in the scope."""
        link = self.live_dir / "codex"
        if not link.exists():
            link.symlink_to(sys.executable)
        for _ in range(count):
            self.live.append(subprocess.Popen(
                [str(link), "-c", "import time; time.sleep(120)"], cwd=cwd or self.scope,
            ))
        time.sleep(0.2)

    def threads(self, rows: list[tuple[str, str, int, str, str, int]]) -> None:
        import sqlite3
        connection = sqlite3.connect(self.state)
        connection.execute(
            "create table threads (id text, cwd text, archived integer, "
            "thread_source text, source text, updated_at integer)"
        )
        connection.executemany("insert into threads values (?,?,?,?,?,?)", rows)
        connection.commit()
        connection.close()

    def send(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "send", *args],
            cwd=self.scope, env=self.environment, text=True, capture_output=True, check=False,
        )

    def test_queues_to_newest_interactive_thread_of_the_scope(self) -> None:
        cwd = str(self.scope.resolve())
        self.threads([
            ("old", cwd, 0, "user", "cli", 10),
            ("newest", cwd, 0, "user", "vscode", 30),
            ("guardian", cwd, 0, "guardian_review", "cli", 40),
            ("archived", cwd, 1, "user", "cli", 50),
            ("headless", cwd, 0, "user", "exec", 60),
            ("elsewhere", "/somewhere/else", 0, "user", "cli", 70),
        ])
        sent = self.send("--to", "codex", "--from", "claude", "please review")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        args = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(args[:3], ["queue", "--thread", "newest"])
        body = args[args.index("--message") + 1]
        self.assertIn("please review", body)
        self.assertIn("[filo] Message from claude:", body)
        self.assertIn("filo send --to claude --from codex", body)

    def test_fails_clearly_without_a_thread(self) -> None:
        self.threads([("elsewhere", "/somewhere/else", 0, "user", "cli", 70)])
        sent = self.send("--to", "codex", "--from", "claude", "hello")
        self.assertNotEqual(sent.returncode, 0)
        self.assertIn("no Codex thread", sent.stderr)
        self.assertFalse(self.record.exists())

    def test_other_recipients_do_not_use_codex(self) -> None:
        self.threads([])
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

    def test_registered_thread_wins_over_the_database_guess(self) -> None:
        cwd = str(self.scope.resolve())
        self.threads([("guessed", cwd, 0, "user", "cli", 99)])
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

    def test_falls_back_to_the_database_when_no_thread_id_is_known(self) -> None:
        cwd = str(self.scope.resolve())
        self.threads([("from-db", cwd, 0, "user", "cli", 5)])
        env = {key: value for key, value in self.environment.items() if key != "CODEX_THREAD_ID"}
        self.environment = env
        registered = self.register("--as", "codex")
        self.assertEqual(registered.returncode, 0, registered.stderr)
        self.assertIn("from-db", registered.stdout)

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

    def test_sessions_that_ended_are_forgotten(self) -> None:
        self.start_codex(1)
        self.register_session(self.A)
        self.register_session(self.B)
        names = [row["name"] for row in json.loads(self.msg_who().stdout)["registered"]]
        self.assertEqual(names, ["codex-2"])
        self.assertEqual(len(list(self.runtime.glob("*/*.reg"))), 1)
        for process in self.live:
            process.terminate()
            process.wait(timeout=5)
        self.assertEqual(json.loads(self.msg_who().stdout)["registered"], [])

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

    def test_leaving_names_the_way_out_for_a_listener(self) -> None:
        self.listen("claude")
        refused = self.msg("leave", "--name", "claude")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("stop", refused.stderr)

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


SHELL = shutil.which("sh") or "/bin/sh"


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.TemporaryDirectory()
        root = Path(self.base.name)
        self.bin = root / "bin"
        self.claude, self.agents, self.agy = (root / f"{client}-home" / "skills" for client in ("claude", "agents", "agy"))
        for skills in (self.claude, self.agents, self.agy):
            skills.parent.mkdir()
        # A PATH that holds only what the scripts need, so that the clients installed on
        # this machine (claude, codex, agy) do not change the result.
        self.tools = root / "tools"
        self.tools.mkdir()
        for tool in ("dirname", "readlink", "mkdir", "ln", "rm", "sed", "python3"):
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

    def test_uninstall_declares_what_it_leaves_behind(self) -> None:
        self.install()
        result = self.uninstall()
        self.assertIn("does not touch", result.stdout)
        self.assertIn("filo-runtime", result.stdout)
        self.assertIn("adapters.json", result.stdout)

    def test_unknown_options_are_refused(self) -> None:
        self.assertEqual(self.install("--uninstall").returncode, 2)
        self.assertEqual(self.uninstall("--force").returncode, 2)


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
