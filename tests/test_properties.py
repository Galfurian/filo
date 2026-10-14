"""Property-based tests: what filo must do for *any* input, not for the inputs somebody thought of.

Three kinds of property, chosen because filo reads bytes written by other processes and puts
text from them in front of a model or a shell:

* **Boundaries never crash.** A frame on a socket, a metadata file and an adapters file may hold
  anything. Each reader must return a value or raise the exceptions its callers catch, and never
  anything else (a RecursionError, an OverflowError) that would end a listener.
* **Invariants of the pure functions.** A name is a safe path component, a shortened event fits
  its limit and is the longest that does, a reply command splits back into exactly its words.
* **Round trips.** What `frame` writes, `read_frame` reads.

The default profile is deterministic, so a run on CI is the same as a run here. To look for new
failures use `HYPOTHESIS_PROFILE=explore python3 -m unittest tests.test_properties`; a failure
found that way belongs in an `@example` of the test that found it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

try:
    from hypothesis import example, given, settings
    from hypothesis import strategies as st
except ImportError as exc:     # the dev tools are not installed: the other tests still run
    raise unittest.SkipTest("hypothesis is not installed") from exc

settings.register_profile("ci", max_examples=200, deadline=None, derandomize=True, database=None)
settings.register_profile("explore", max_examples=5000, deadline=None, database=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))

SCRIPT = Path(__file__).resolve().parents[1] / "filo.py"


def load_filo() -> Any:
    spec = importlib.util.spec_from_file_location("filo_under_property_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


filo = load_filo()

# Text as it arrives from another process: any character but a lone surrogate, so a NUL, a newline,
# quotes and non-ASCII letters are all in it.
texts = st.text(max_size=120)
names = st.text(alphabet=sorted(filo.NAME_ALPHABET), min_size=1, max_size=30).filter(
    lambda value: value not in {".", ".."})
leaves = st.none() | st.booleans() | st.integers() | st.floats() | texts
json_values = st.recursive(
    leaves, lambda inner: st.lists(inner, max_size=4) | st.dictionaries(texts, inner, max_size=4), max_leaves=25)
json_objects = st.dictionaries(texts, json_values, max_size=6)
plain_objects = st.dictionaries(texts, st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | texts,
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(texts, inner, max_size=3), max_leaves=15), max_size=6)


class Reader:
    """Feeds bytes to `read_frame` through a real socket, from a thread, so a big frame cannot block."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> dict[str, Any]:
        reader, writer = socket.socketpair()
        reader.settimeout(5)

        def send() -> None:
            try:
                writer.sendall(self.data)
            except OSError:
                pass
            finally:
                writer.close()

        thread = threading.Thread(target=send)
        thread.start()
        try:
            return filo.read_frame(reader)    # type: ignore[no-any-return]
        finally:
            reader.close()
            thread.join(timeout=5)


class BoundaryTests(unittest.TestCase):
    @given(st.binary(max_size=2000))
    @example(b"[" * 100_000 + b"\n")                   # nested deeply enough to overflow the parser
    @example(b'{"pid":' + b"9" * 5000 + b"}\n")
    @example(b"\xff\xfe\n")
    @example(b"[]\n")
    def test_a_frame_is_an_object_or_one_of_the_errors_the_listener_catches(self, data: bytes) -> None:
        try:
            frame = Reader(data).read()
        except (OSError, ValueError):                  # ProtocolError and JSONDecodeError are ValueErrors
            return
        self.assertIsInstance(frame, dict)

    @given(plain_objects)
    def test_what_frame_writes_read_frame_reads(self, payload: dict[str, Any]) -> None:
        data = filo.frame(payload)
        self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(data.count(b"\n"), 1)
        self.assertEqual(Reader(data).read(), payload)

    @given(st.binary(max_size=500))
    @example(b"[" * 100_000)
    @example(b"Infinity")
    def test_a_metadata_file_is_an_object_or_one_of_the_errors_its_readers_catch(self, data: bytes) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "file.json"
            path.write_bytes(data)
            try:
                value = filo.read_object(path)
            except (OSError, ValueError):
                return
            self.assertIsInstance(value, dict)

    @given(json_objects, st.text(max_size=5))
    @example({"pid": float("inf")}, "pid")
    @example({"pid": float("nan")}, "pid")
    @example({"pid": 10**400}, "pid")
    def test_a_number_field_is_an_int_or_one_of_the_errors_its_callers_catch(
            self, data: dict[str, Any], key: str) -> None:
        try:
            value = filo.int_field(data, key, 0)
        except (ValueError, TypeError):
            return
        self.assertIsInstance(value, int)

    @given(st.text(max_size=300), st.text(max_size=20))
    def test_lsof_output_gives_strings_whatever_it_says(self, output: str, command: str) -> None:
        found = filo.parse_lsof(output, command)
        self.assertTrue(all(isinstance(path, str) for path in found))


class AdapterTests(unittest.TestCase):
    @given(texts, json_values)
    def test_an_adapter_description_is_an_adapter_or_a_user_error(self, client: str, spec: Any) -> None:
        try:
            adapter = filo.command_adapter(client, spec)
        except filo.UserError:
            return
        self.assertEqual(adapter.client, client)

    @given(texts, texts, texts)
    @example("{message}", "{session}", "/x")
    @example("a", "{scope}", "/some/dir")
    def test_a_message_reaches_the_command_as_it_is_even_if_it_looks_like_a_placeholder(
            self, session: str, text: str, directory: str) -> None:
        adapter = filo.command_adapter("x", {"deliver": ["tool", "{session}", "--", "{message}", "in={scope}"]})
        with mock.patch.object(filo, "run_delivery") as run:
            adapter.deliver(session, text, directory, {})
        self.assertEqual(run.call_args.args[0], ["tool", session, "--", text, f"in={directory}"])


class NameAndScopeTests(unittest.TestCase):
    @given(st.text(max_size=40))
    @example(".")
    @example("..")
    @example("")
    def test_a_name_is_accepted_only_if_it_is_a_path_component_of_its_own(self, value: str) -> None:
        try:
            name = filo.valid_name(value)
        except filo.UserError:
            return
        self.assertTrue(name and all(char in filo.NAME_ALPHABET for char in name))
        base = Path("/runtime/scope")
        self.assertEqual((base / name).name, name)
        self.assertEqual((base / name).parent, base)
        self.assertEqual((base / f"{name}.json").parent, base)
        self.assertEqual((base / "spool" / name).parent, base / "spool")

    @given(st.text(max_size=40))
    @example("~0")
    @example("~no-such-user/x")
    def test_a_scope_is_a_path_or_a_keyword_and_shows_as_what_was_given(self, raw: str) -> None:
        scope = filo.canonical_scope(raw)
        if scope.startswith("keyword:"):
            self.assertEqual(filo.display_scope(scope), raw)
            self.assertIsNone(filo.scope_path(scope))
        else:
            self.assertTrue(scope.startswith("path:/"))
            self.assertEqual(filo.canonical_scope(filo.display_scope(scope)), scope)

    @given(st.text(max_size=60))
    @example("a\x00b")
    @example("..")
    @example("")
    def test_whether_a_message_came_from_outside_is_a_yes_or_no_for_any_origin(self, origin: str) -> None:
        with tempfile.TemporaryDirectory() as raw:
            scope = filo.canonical_scope(raw)
            self.assertIsInstance(filo.is_foreign(scope, origin), bool)
            self.assertFalse(filo.is_foreign(scope, str(Path(raw) / ".")))
            self.assertFalse(filo.is_foreign(scope, None))
            self.assertFalse(filo.is_foreign("keyword:team", origin))


class SentFromTests(unittest.TestCase):
    @given(json_objects)
    @example({"home": "path:" + "x" * 5000})
    @example({"home": 7})
    @example({"origin": "a\x00b"})
    def test_whether_a_message_comes_from_outside_is_a_yes_or_no_for_any_message(self, message: dict[str, Any]) -> None:
        outside, where = filo.sent_from("path:/some/scope", message)
        self.assertIsInstance(outside, bool)
        self.assertTrue(where is None or isinstance(where, str))
        if "home" in message and message["home"] != "path:/some/scope":
            self.assertTrue(outside)       # a home that is not this scope is never taken for it

    @given(texts, texts)
    def test_a_home_that_is_this_scope_is_inside(self, name: str, body: str) -> None:
        self.assertEqual(filo.sent_from("path:/a", {"home": "path:/a", "from": name, "message": body}), (False, "/a"))


class TextForTheReaderTests(unittest.TestCase):
    @given(texts, texts, st.none() | texts)
    @example("a; rm -rf ~", "b", None)
    @example("a", "b c", "/some where/'x'")
    def test_a_reply_command_splits_back_into_exactly_its_words(self, sender: str, me: str, origin: str | None) -> None:
        words = shlex.split(filo.reply_hint(sender, me, origin))
        expected = ["filo", "send", "--to", sender, "--from", me]
        if origin:
            expected += ["--scope", origin]
        self.assertEqual(words, expected + ["--message", "..."])

    @given(names, names)
    def test_the_reply_command_of_ordinary_names_is_left_as_written(self, sender: str, me: str) -> None:
        self.assertEqual(filo.reply_hint(sender, me), f'filo send --to {sender} --from {me} --message "..."')

    @given(texts, texts, st.none() | texts)
    def test_an_event_is_one_json_line_with_the_body_last(self, sender: str, body: str, origin: str | None) -> None:
        message: dict[str, Any] = {"id": "18de6610f927c276-fe7820", "from": sender, "message": body, "created_ns": 1}
        if origin is not None:
            message["origin"] = origin
        scope = "path:/some/scope"
        event = filo.message_event("me", message, 7, None, scope)
        line = json.dumps(event, ensure_ascii=False)
        self.assertNotIn("\n", line)
        self.assertEqual(json.loads(line), event)
        self.assertEqual(list(event)[-1], "message")
        self.assertEqual(event["message"], body)

    @given(json_objects)
    def test_an_event_can_be_made_from_any_message_object_or_it_is_a_value_error(self, message: dict[str, Any]) -> None:
        try:
            event = filo.message_event("me", message, None, 150, "path:/some/scope")
        except ValueError:
            return
        self.assertEqual(event["kind"], "message")

    @given(texts, st.integers(min_value=filo.MIN_PREVIEW, max_value=1500), texts)
    @example("é\"\n" * 400, 100, "x")
    @example("x" * 5000, 400, "x" * 40)
    def test_a_shortened_event_fits_its_limit_and_is_the_longest_start_that_does(
            self, body: str, limit: int, sender: str) -> None:
        message = {"id": "18de6610f927c276-fe7820", "from": sender, "message": body, "created_ns": 1}
        event = filo.message_event("me", message, 7, limit, "path:/some/scope")

        def size(candidate: dict[str, Any]) -> int:
            return len(json.dumps(candidate, ensure_ascii=False))

        self.assertEqual(list(event)[-1], "message")
        if not event.get("truncated"):
            self.assertEqual(event["message"], body)
            if body:
                self.assertLessEqual(size(event), limit)
            return
        if not body:
            self.assertNotIn("truncated", event)    # there is nothing to cut from it
            return
        shown = event["message"]
        self.assertTrue(body.startswith(shown))
        self.assertLess(len(shown), len(body))
        self.assertEqual(event["length"], len(body))
        self.assertEqual(event["show"], "filo show 18de6610f927c276-fe7820")
        if shown:
            self.assertLessEqual(size(event), limit)
        self.assertGreater(size({**event, "message": body[:len(shown) + 1]}), limit)


if __name__ == "__main__":
    unittest.main()
