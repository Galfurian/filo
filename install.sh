#!/bin/sh
# Install filo for the current user. No sudo, no copies: it only makes symbolic
# links that point into this checkout, so `git pull` upgrades it.
#
#   ./install.sh            do it
#   ./install.sh --dry-run  show what it would do and change nothing
#
# `./uninstall.sh` removes exactly what this script creates.
# shellcheck disable=SC2016  # the backticks in the messages are literal text
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
. "$repo_dir/scripts/common.sh"

dry_run=0
case "${1:-}" in
    "") ;;
    -n|--dry-run) dry_run=1 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'usage: ./install.sh [--dry-run]\n' >&2; exit 2 ;;
esac

printf 'filo, installed from %s\n\nThis will:\n' "$repo_dir"
links | while IFS="$tab" read -r what target source required command; do
    case $(state "$target" "$source" "$required" "$command") in
        new)
            [ -d "$(dirname "$target")" ] ||
                printf '  - create the directory %s, and any parent of it that is missing; uninstall leaves it\n' "$(dirname "$target")"
            printf '  - link %s -> %s  (%s)\n' "$target" "$source" "$what" ;;
        current)  printf '  - leave %s alone: already linked  (%s)\n' "$target" "$what" ;;
        conflict) printf '  - skip %s: something that is not ours is already there  (%s)\n' "$target" "$what" ;;
        absent)   printf '  - skip %s: no %s directory and no `%s` command, so that client is not installed  (%s)\n' \
                      "$target" "$required" "$command" "$what" ;;
    esac
done
printf '\nIt never uses sudo, copies files, changes permissions, overwrites anything it\ndid not create, or writes outside the paths above.\n'

if [ "$dry_run" = 1 ]; then
    printf '\nDry run: nothing was changed.\n'
    exit 0
fi

if [ ! -x "$repo_dir/filo.py" ]; then
    printf '\n%s is not executable, so the `filo` command would not run.\n' "$repo_dir/filo.py" >&2
    printf 'Git tracks it as executable: run `git checkout -- filo.py`, or `chmod +x` it, then try again.\n' >&2
    exit 1
fi
printf '\n'
links | while IFS="$tab" read -r what target source required command; do
    case $(state "$target" "$source" "$required" "$command") in
        new)
            mkdir -p "$(dirname "$target")"
            ln -s "$source" "$target"
            printf 'linked %s\n' "$target" ;;
        current)  printf 'already linked %s\n' "$target" ;;
        conflict) printf 'skipped %s (exists and was not created by this installer)\n' "$target" >&2 ;;
        absent)   printf 'skipped %s (%s not found, and no `%s` command)\n' "$target" "$required" "$command" ;;
    esac
done

case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) printf '\nNote: %s is not in your PATH, so the command `filo` will not be found.\n' "$bin_dir" ;;
esac
printf '\nDone. In each session, in the same directory, run /filo.\n'
