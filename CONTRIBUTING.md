# Contributing

Thanks for looking. filo is small on purpose, so a few things are worth knowing before you
change it. Taking part means following the [Code of Conduct](CODE_OF_CONDUCT.md).

## What filo is meant to stay

- **It does not take sessions over.** A session stays an ordinary interactive one that you
  keep talking to. If a change needs a pty, tmux, or a wrapper around the client, it is
  probably not for filo.
- **No daemon, no server, no dependencies.** One Python file, standard library only. A
  listener may go on after the command that started it (`join --detach`), but only for as long as
  the process that it was given as its parent.
- **A small, transparent installer.** `install.sh` makes symbolic links and says first
  which ones. Anything new it touches has to be announced the same way.

## Set up

```sh
git clone git@github.com:Galfurian/filo.git && cd filo
pip install --require-hashes -r requirements-dev.txt   # the tools CI uses, with their hashes
```

Run the same checks as CI before you open a pull request:

```sh
python3 -m unittest discover -s tests
ruff check .
mypy
pyright
shellcheck -s sh install.sh uninstall.sh get.sh scripts/common.sh
```

`tests/test_properties.py` holds property-based tests (Hypothesis, one of the dev tools): they
check what filo does for *any* input, and the module is skipped when Hypothesis is not
installed. The default run is deterministic. To look for new failures run
`HYPOTHESIS_PROFILE=explore python3 -m unittest tests.test_properties` (a couple of minutes), and
put what it finds in an `@example` of the test that found it, with the fix.

The tests need no client installed: they use stand-ins. The OpenCode plugin is checked with
`node`; the test is skipped when it is missing.

## Making a change

1. Branch from `main`: `feat/…`, `fix/…` or `docs/…`.
2. Open a pull request. CI has to pass on Linux (Python 3.10 and 3.12) and macOS.
3. It is merged with *squash*, so `main` stays linear.

A commit message has a subject in the imperative of about 70 characters or less
("Add …", "Fix …"), then a body that says why, not only what. Please add or change a test
with the code, and update the README and `skills/filo/SKILL.md` when behaviour changes: the
skill is what the agents read.

Releases are tags: raise `version` in `pyproject.toml` and `__version__` in `filo.py`
together, write `docs/releases/vX.Y.Z.md`, and tag `vX.Y.Z`. The release workflow stamps
`get.sh` with the version and the hash of the archive (the two lines that hold `@VERSION@` and
`@SHA256@`; keep them as they are) and attaches it, signed, to the release.

## Adding a client

Most clients need no code: describe how to wake one in the adapters file (see
[Adding another LLM](README.md#adding-another-llm)). A client that cannot be reached from
outside a running session, as OpenCode could not without a plugin, needs a bridge.

A built-in client is an `Adapter` in `filo.py` plus a test, a section in the skill and a row
in the README table. Say which version of the client you tried it with, since these
integrations lean on interfaces that can change.

## Adding a profile

A profile is a markdown file in `skills/filo/profiles/`: the first line is the summary
shown by `filo profiles`, the rest is what the agent is told to do. It is text the user
chose, so keep it to behaviour, and have it say what it will not do.

## Reporting a security problem

Not here. See [SECURITY.md](SECURITY.md).
