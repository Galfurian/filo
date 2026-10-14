# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private report:
<https://github.com/Galfurian/filo/security/advisories/new> (the **Security** tab, then
**Report a vulnerability**). Say which version or commit you tried, what you did, and what
you expected.

I aim to answer within 7 days, to fix a confirmed problem or explain why not within 90
days, and to publish an advisory when the fix is out, crediting you unless you prefer not
to be named. Please keep the details private until then.

Only the latest commit on `main` is supported; there are no other maintained versions.

## What filo is, and the trust model

filo passes messages between AI sessions of **one user on one machine**. It is not a
network service and it has no accounts. Read this as the list of what it does and does not
protect.

**It protects against other users of the machine.**

- All state lives in `/tmp/filo-<uid>` (or `FILO_DIR`). filo refuses a directory that is a
  link, is not a directory, or belongs to someone else, and closes one that is too open to
  `0700`. Sockets and registration files are `0600`.
- A listener refuses a peer whose user is not its own, where the system can tell (Linux
  and macOS). Elsewhere that check is skipped and the private directory is what keeps
  other users out.
- Names are limited to letters, digits, `.`, `_` and `-`, so a name cannot be a path.

**It does not protect against anything running as you.**

- Any process of yours can connect to a socket, send messages, claim any `--from` name and
  read the files under the runtime directory. `--from` is a label, not an identity;
  `peer_pid` in a message event is the process that really sent it.
- A client registered with `capture_env` leaves secrets in its registration file. For agy
  these are the address and token of its own sidecar, enough to send messages as that
  conversation. The file is `0600` and is removed when the session is forgotten.

**Where a message came from.** A message carries the scope its sender belongs to (`home`, and the
directory it ran in, `origin`, for older filo), and a listener marks it `foreign` when that is not its
scope; the skill tells agents to tell the user about such a message. What it says is reported by the
sender's process, so it is as trustworthy as `--from` is, which is not at all against a process of yours:
it helps an honest agent notice that a message crossed scopes, and protects against nothing else. A
`home` that is not a scope counts as foreign, never as local. The `peer_pid` of an event is the one thing
the system vouches for. `filo who --all` reads the runtime directory, which only you can read.

**What a listener keeps.** For `filo show`, each listener writes the messages it receives to
`inbox/<name>/<id>.json` in the scope's runtime directory (`0600` files in a `0700`
directory) and removes them after 24 hours. They hold the whole text of the messages, so
they are as private as the runtime directory is, and no more.

**Messages are untrusted input to a model.** A message from another agent reaches a model
that can run commands, so it can carry a prompt injection. filo cannot prevent that. The
skill tells agents to treat messages as untrusted and to ask the user when in doubt, and a
message can never change a profile, which is text the user chose.

**Some files are code you wrote or chose.** The `deliver` list in the adapters file runs as
you when a message is sent, and the OpenCode plugin runs inside OpenCode and adds the tools
`filo_join` and `filo_leave` to every session. The installer only links these from the
checkout, so what runs is what is in the checkout.

**The installer.** `install.sh` makes symbolic links at the paths it prints, needs no sudo,
copies nothing, changes no permissions and never overwrites a file it did not create.
`uninstall.sh` removes only the links that point into the checkout.

**The network.** Messages never leave the machine. The one thing in filo that uses the
network is `filo upgrade`, plus the look for a new version that `FILO_CHECK_UPDATES=1`
turns on (a `HEAD` request for `/releases/latest`, at most once a day, answer kept in
`~/.cache/filo`). Nothing is sent but that request. `filo upgrade` downloads the `get.sh`
of the newest release, refuses it unless it is stamped for that version, shows its dry run
and asks before running it, so it carries the trust of the one-line install below. The
skill tells agents to pass on a "new version" notice and not to run `filo upgrade`
themselves.

**The one-line install.** `curl … | sh` runs a script from the network, so read this before
you use it. `get.sh` is attached to each release and the release workflow writes into it the
version and the SHA-256 of that release's archive; it downloads only that archive, over
https, and stops when the hash differs. A copy that was not stamped refuses to run. It
unpacks into `~/.local/share/filo` (or `FILO_HOME`), and deletes that directory on
uninstall only when it holds the marker file `.filo-release` that `get.sh` wrote; it refuses
`/`, your home directory, a relative path and a link. The script is defined whole and run
on its last line, so a download cut short does not run half of it. What this does not give
you: the hash and the archive come from the same release, so someone who can change a
release can change both. That is what the signature and the provenance below are for;
check `get.sh` with them first if that matters to you.

## Checking a release

A release has the archive, `filo-X.tar.gz.sigstore.json` (a Sigstore signature, made without
a key by the release workflow) and `filo-X.intoto.jsonl` (the build provenance, which says
the archive was built from that tag by `.github/workflows/release.yml`). It also has
`get.sh`, with its own signature `get.sh.sigstore.json`; the provenance covers both files.
To check them:

```sh
sha256sum -c SHA256SUMS
cosign verify-blob filo-X.tar.gz --bundle filo-X.tar.gz.sigstore.json \
  --certificate-identity https://github.com/Galfurian/filo/.github/workflows/release.yml@refs/tags/vX \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
cosign verify-blob get.sh --bundle get.sh.sigstore.json \
  --certificate-identity https://github.com/Galfurian/filo/.github/workflows/release.yml@refs/tags/vX \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify filo-X.tar.gz --repo Galfurian/filo --bundle filo-X.intoto.jsonl
gh attestation verify get.sh --repo Galfurian/filo --bundle filo-X.intoto.jsonl
```

The `get.sh` you checked contains the hash of the archive it installs, so checking it is
enough to trust what it then downloads.

## What counts as a vulnerability

In scope, for example: a way for **another user** to read, send or interfere with a
session; a path or symlink trick that makes filo write or delete outside its directory; a
race on the runtime directory; the installer touching something it did not announce.

Out of scope: anything that needs to already be running as the same user, spoofing `--from`,
and a model that follows a harmful message (report ideas for reducing that as ordinary
issues).

## Integrations that can change

The agy and OpenCode integrations use interfaces that are not part of a stable public
contract. If a client update changes them, delivery can stop, or in a bad case behave
differently from what is described here. Please report that with the client version.
