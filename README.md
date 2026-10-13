<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/banner-dark.svg">
    <img src="docs/images/banner-light.svg" alt="filo: a thread between your AI coding agents" width="100%">
  </picture>
</h1>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue"></a>
  <img alt="Python 3.10 or later" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="No dependencies" src="https://img.shields.io/badge/dependencies-none-brightgreen">
</p>

<p align="center">
  <b>Start a few Claude, Codex and agy sessions in one directory, give each a role,<br>and they message each other by name: one plans, one researches, one builds.</b><br>
  One Python file, standard library only, no sudo.
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#profiles">Profiles</a> ·
  <a href="#clients">Clients</a> ·
  <a href="#adding-another-llm">Add your own</a> ·
  <a href="#security">Security</a>
</p>

<p align="center">
  <img src="docs/images/four-sessions.png" alt="Four terminal panes: two Claude sessions, a Codex session and an agy session, each joined to the same pool under a role" width="100%">
</p>

*Four sessions in one directory, each started with a role: `/filo manager` and
`/filo researcher` in Claude, `$filo librarian` in Codex, `/filo worker` in agy. Each
loaded its profile, joined the pool, listed who else is there and waits.*

## Quick start

```sh
./install.sh
```

Then run `/filo` in each session, in the same directory, or `/filo manager` to
join under a role (see [Profiles](#profiles)). Each session connects, says who else
is there, and waits. Ask one to message another:

```sh
filo who
filo send --to codex --from claude --message 'Please review the storage change.'
```

## How it works

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/overview-dark.svg">
    <img src="docs/images/overview-light.svg" alt="You start each session and run /filo manager; a message reaches a listening Claude session over a Unix socket, registered Codex and agy sessions through their adapters, and a session that is away through a spool emptied when it rejoins" width="100%">
  </picture>
</p>

You start every session yourself and tell it to join the pool of the directory it is
in. From then on a session can message any other by name. How a message gets there
depends on the recipient: three routes, shown above and described under
[Delivery](#delivery).

## Clients

| Client | Connects with | A message arrives as | Limits |
|---|---|---|---|
| Claude | a Monitor on `filo join --as claude --json` | an event that wakes it, with the reply command | the Monitor expires; start it again |
| Codex | `filo register --as codex` (records `$CODEX_THREAD_ID`) | a normal turn, through `codex queue`, even while you type | none |
| agy | `filo register --as agy` (records `$ANTIGRAVITY_CONVERSATION_ID`) | a system message, through `agentapi send-message`; the chat is never locked and no second `agy` starts | an idle agy starts a turn on its own when one arrives; a busy one gets it when its current step ends, and it does not interrupt a running command; `register` must run inside agy |
| Anything else | `filo register --as NAME` | whatever its adapter does, see [Adding another LLM](#adding-another-llm) | |

The skill (`skills/filo/SKILL.md`) is the instruction sheet each agent reads.
It is the one place that says exactly what an agent does at boot and how it
replies.

## Profiles

A profile is a role: a markdown file of plain instructions that an agent follows
once it has joined. You choose it when you start the session:

```
/filo                   join as the client's default name
/filo manager           join as "manager", with the manager profile
/filo worker w2         the worker profile, under the name "w2"
```

| Profile | What it does |
|---|---|
| `manager` | Splits a goal, briefs the others, collects their reports, gives you one summary. |
| `worker` | Does what it is assigned, reports progress and blockers, starts nothing on its own. |
| `researcher` | Investigates a question and recommends an answer with its trade-offs. Does not implement. |
| `librarian` | Collects references, one line each on why they matter. |

`filo profiles` lists them and `filo profile manager` prints one. The built-in
profiles are `skills/filo/profiles/*.md`. To add your own, or to replace one, put
`NAME.md` in `~/.config/filo/profiles/` (or the directory named by `FILO_PROFILES`):
the first line is the summary shown in the list, the rest is the instructions. A word
after `/filo` that is not a profile is just a name.

A profile does not choose the model or the effort: set those on each session the way you
always do (`/model`, `/effort`), then join with the role you want it to play.

A profile is text you chose, so the agent follows it. A message from another agent
is not, and stays untrusted input.

## Install and uninstall

```sh
./install.sh --dry-run   # print what it would do, change nothing
./install.sh
./uninstall.sh           # also takes --dry-run
```

Both scripts first print exactly what they will do. The installer needs no
sudo and copies nothing, and it changes no permissions: it makes symbolic links
into this checkout, so `git pull` upgrades everything. (Git tracks `filo.py` as
executable; if yours is not, the installer stops and says so.)

- `~/.local/bin/filo` -> `filo.py`
- the skill `skills/filo`, in the skills directory of each client that is
  installed: `~/.claude/skills` (Claude), `~/.agents/skills` (Codex),
  `~/.gemini/antigravity-cli/skills` (agy). A client counts as installed when
  its directory exists or its command (`claude`, `codex`, `agy`) is in the
  `PATH`; any other is skipped and nothing is created for it.

It never overwrites an existing file or a link it did not create. The
uninstaller removes only links that point into this checkout. It leaves the
runtime state (`/tmp/filo-runtime`, which holds agy tokens), your adapters file
and this checkout, and says so. Override the locations with `FILO_BIN_DIR`,
`FILO_CLAUDE_SKILLS`, `FILO_AGENTS_SKILLS` and `FILO_AGY_SKILLS`. The profiles
travel with the skill link, so they need no install of their own.

## Commands

```sh
filo who
filo send --to claude --from codex --message 'text'
filo send --to codex --from claude --stdin < review.md
filo status ID          # delivered? pending? expired?
filo join --as human    # a human listener, no LLM
filo register --as agy --name reviewer   # choose the name of a registered session
filo rename agy-2 reviewer               # or change it later
filo leave                                  # a registered session leaves the pool
filo profiles                               # the roles you can join as
filo profile manager                        # print one
```

### Names and scopes

The scope is the current directory (or `--scope PATH_OR_KEYWORD`, to share one
across directories). Sessions in different directories never see each other.
In one directory the first `claude` is `claude`, the next `claude-2`, and so
on; `register` numbers `codex` and `agy` sessions the same way. `join --exact`
fails instead of adding a suffix. `register --name NAME` picks the name, and a
name that is taken is an error. `rename OLD NEW` changes the name of a
registered session; a listener changes its name by joining again. A registered
session leaves with `filo leave`, and a listener by stopping. Address a
session by its exact name from `filo who`, and use your own exact name in
`--from`.

### Delivery

| Recipient | How a message gets there |
|---|---|
| Listening (`join`) | Straight to its socket. |
| Registered (`register`) | Through the client's adapter, for example `codex queue` or `agentapi send-message`. |
| Known but away | Into a spool; delivered when a listener with that name joins. Kept for an hour (`FILO_SPOOL_TTL`), names are remembered for a day. |
| Never seen | Error. |

Leaving is not the same for everyone. A listener that stops is only away: messages
for it wait in the spool and arrive if it rejoins. A registered session that runs
`filo leave` is gone: its name is forgotten and messages for it are refused.

Sandboxed senders (the Codex sandbox cannot connect to Unix sockets, but can
write files) leave the message in the spool automatically, and the listener
delivers it within half a second. Every message has an id; `status` reports
whether it was delivered.

## Adding another LLM

Describe how to wake it in `~/.config/filo/adapters.json` (or the file named
by `FILO_ADAPTERS`). No code change is needed:

```json
{
  "gemini": {
    "id_env": "GEMINI_SESSION",
    "process": "gemini",
    "capture_env": ["GEMINI_ADDRESS"],
    "deliver": ["gemini-tool", "send", "--session", "{session}", "{message}"]
  }
}
```

- `deliver` is an argument list (no shell). `{session}`, `{message}` and
  `{scope}` are replaced; the message already carries its header and the reply
  command. It has 30 seconds to finish.
- `id_env` is the environment variable holding the session id.
- `process` is the executable name, used to forget sessions that have ended.
- `capture_env` lists environment variables that `register` copies from the
  session and `deliver` gets back when it runs. Use it for a client that can
  only be reached with the session's own address or token.

Then each Gemini session runs `filo register --as gemini`. The client must
offer a way to push a message into a running session from outside: Codex has
`codex queue`, agy has `agentapi send-message`, and Claude has a Monitor.

## What it keeps and forgets

Runtime state lives under `/tmp/filo-runtime` (or `FILO_DIR`): sockets,
the spool and small metadata files, all mode `0600`/`0700`. A registration is
forgotten when fewer of that client's processes run in the directory than there
are registrations (the newest are kept); `who` and `send` do this check. `who`
never deletes a listener it merely cannot probe: a sandboxed or slow one stays.

## Security

This is local, same-user coordination, not network messaging. The socket
directory is private, and a listener refuses peers of another user. `--from` is
a label the sender chooses, not an identity: any process of yours can claim any
name. Each message event reports `peer_pid`, the process that actually sent it.
The skill tells agents to treat messages as untrusted input, since a message
reaches a model that can run commands.

A client with `capture_env` leaves secrets in its registration file. For agy
these are the address and token of its own sidecar, enough to send messages as
that conversation. The file is mode `0600` and lives until the registration is
forgotten, which happens when the session's process is gone.

## Development

```sh
python3 -m unittest discover -s tests -v   # tests
pyright                                    # strict on filo.py, standard on tests
mypy                                       # strict
ruff check .
```

The type-checker settings live in `pyproject.toml`, so the result does not depend
on local configuration.

Run `pip install -e '.[dev]'` to get the checkers. CI runs all four on every push.

## License

MIT, see `LICENSE`.

The Claude, OpenAI and Gemini marks in the diagram come from
[Simple Icons](https://simpleicons.org). The names and logos belong to their owners
and are used here only to say which product a session is.
