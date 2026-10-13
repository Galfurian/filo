#!/bin/sh
# Remove what ./install.sh created, and nothing else: it deletes a link only when
# the link points into this checkout. No sudo.
#
#   ./uninstall.sh            do it
#   ./uninstall.sh --dry-run  show what it would do and change nothing
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
. "$repo_dir/scripts/common.sh"

dry_run=0
case "${1:-}" in
    "") ;;
    -n|--dry-run) dry_run=1 ;;
    -h|--help) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'usage: ./uninstall.sh [--dry-run]\n' >&2; exit 2 ;;
esac

printf 'filo, removed for the checkout at %s\n\nThis will:\n' "$repo_dir"
links | while IFS="$tab" read -r what target source _ _; do
    if owned "$target" "$source"; then
        printf '  - remove the link %s  (%s)\n' "$target" "$what"
    elif [ -e "$target" ] || [ -L "$target" ]; then
        printf '  - keep %s: it is not a link created by this installer\n' "$target"
    else
        printf '  - nothing to do for %s: not there\n' "$target"
    fi
done
runtime=${FILO_DIR:-/tmp/filo-$(id -u)}
printf '\nIt does not touch these; delete them yourself if you want them gone:\n'
printf '  - %s  (sessions, spool; holds the tokens of registered agy sessions)\n' "$runtime"
printf '  - %s  (your adapters file, if you made one)\n' "${FILO_ADAPTERS:-$HOME/.config/filo/adapters.json}"
printf '  - this checkout, %s\n' "$repo_dir"

if [ "$dry_run" = 1 ]; then
    printf '\nDry run: nothing was changed.\n'
    exit 0
fi

printf '\n'
links | while IFS="$tab" read -r what target source _ _; do
    if owned "$target" "$source"; then
        rm "$target"
        printf 'removed %s\n' "$target"
    elif [ -e "$target" ] || [ -L "$target" ]; then
        printf 'kept %s (not created by this installer)\n' "$target" >&2
    fi
done
printf '\nDone.\n'
