#!/bin/sh
# Install or remove filo from a release, in one command. No sudo.
#
#   curl -fsSL https://github.com/Galfurian/filo/releases/latest/download/get.sh | sh
#   curl -fsSL https://github.com/Galfurian/filo/releases/latest/download/get.sh | sh -s -- uninstall
#
#   sh get.sh [install|uninstall] [--dry-run]
#
# The release workflow stamps this file with the version and the SHA-256 of that
# version's archive, so a get.sh from a release can only fetch that archive and only
# accept it if the hash matches. It unpacks the archive into ~/.local/share/filo and
# runs the install.sh inside, which makes the links; uninstall runs the uninstall.sh
# there and deletes the directory. Override the place with FILO_HOME.
# shellcheck disable=SC2016  # the backticks in the messages are literal text
set -eu

version="@VERSION@"
sha256="@SHA256@"

marker=.filo-release

die() {
    printf 'get.sh: %s\n' "$*" >&2
    exit 1
}

usage() {
    printf 'usage: get.sh [install|uninstall] [--dry-run]\n'
}

# Download $1 into the file $2. Only https is accepted; file:// is for a mirror and
# for the tests.
fetch() {
    case $1 in
        file://*) cp -- "${1#file://}" "$2" ;;
        https://*)
            if command -v curl >/dev/null 2>&1; then
                curl --proto '=https' --tlsv1.2 -fsSL -o "$2" "$1"
            elif command -v wget >/dev/null 2>&1; then
                wget -q --https-only -O "$2" "$1"
            else
                die 'need curl or wget to download the release'
            fi ;;
        *) die "will not download from $1: only https:// is accepted" ;;
    esac
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d ' ' -f 1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d ' ' -f 1
    else
        die 'need sha256sum or shasum to check the download'
    fi
}

cleanup() {
    [ -z "${scratch:-}" ] || rm -rf "$scratch"
    [ -z "${stage:-}" ] || rm -rf "$stage"
    [ -z "${old:-}" ] || rm -rf "$old"
}

# The directory must be an absolute path that is neither / nor the home directory,
# and it must not be a link: it is the one thing this script deletes.
check_home() {
    case $home_dir in
        /*) ;;
        *) die "FILO_HOME must be an absolute path, not $home_dir" ;;
    esac
    case $home_dir in
        /|"$HOME"|"$HOME"/) die "refusing to use $home_dir" ;;
    esac
    [ ! -L "$home_dir" ] || die "$home_dir is a link: refusing to use it"
}

install_filo() {
    archive="filo-$version.tar.gz"
    url="$release_url/$archive"
    printf 'filo %s, installed from its release.\n\nThis will:\n' "$version"
    printf '  - download %s\n' "$url"
    printf '  - check that its SHA-256 is %s\n' "$sha256"
    printf '  - unpack it into %s\n' "$home_dir"
    printf '  - run %s/install.sh, which says which links it makes before it makes them\n' "$home_dir"
    printf '\nIt never uses sudo, and apart from a temporary directory and those links it\nwrites nowhere else.\n'

    command -v tar >/dev/null 2>&1 || die 'need tar to unpack the release'
    command -v python3 >/dev/null 2>&1 || die 'filo needs python3 (3.10 or later), and none is installed'
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
        die 'filo needs python 3.10 or later'
    if [ -e "$home_dir" ] && [ ! -f "$home_dir/$marker" ]; then
        die "$home_dir exists and was not made by this script: move it away, or set FILO_HOME"
    fi

    if [ "$dry_run" = 1 ]; then
        printf '\nDry run: nothing was changed.\n'
        return 0
    fi

    printf '\n'
    scratch=$(mktemp -d "${TMPDIR:-/tmp}/filo-get.XXXXXX")
    fetch "$url" "$scratch/$archive"
    actual=$(sha256_of "$scratch/$archive")
    [ "$actual" = "$sha256" ] || die "the download is not the release: expected $sha256, got $actual"
    printf 'checked %s\n' "$archive"

    # Unpack beside the final place, so that the last step is a rename.
    mkdir -p "$(dirname "$home_dir")"
    stage="$home_dir.new.$$"
    mkdir "$stage"
    tar -xzf "$scratch/$archive" -C "$stage" --strip-components=1
    [ -f "$stage/install.sh" ] && [ -x "$stage/filo.py" ] || die 'the archive does not look like filo'
    printf 'version=%s\nsha256=%s\n' "$version" "$sha256" > "$stage/$marker"

    if [ -d "$home_dir" ]; then
        old="$home_dir.old.$$"
        mv "$home_dir" "$old"
    fi
    if ! mv "$stage" "$home_dir"; then
        [ -z "${old:-}" ] || mv "$old" "$home_dir"
        old=
        die "could not move the new release into $home_dir"
    fi
    stage=
    printf 'unpacked into %s\n\n' "$home_dir"
    "$home_dir/install.sh"
}

uninstall_filo() {
    printf 'filo, removed from %s\n\n' "$home_dir"
    if [ ! -e "$home_dir" ]; then
        printf 'Nothing is installed there. If you installed from a checkout, run its ./uninstall.sh.\n'
        return 0
    fi
    [ -f "$home_dir/$marker" ] ||
        die "$home_dir was not made by this script, so nothing was removed. For a checkout, run its ./uninstall.sh"
    if [ "$dry_run" = 1 ]; then
        "$home_dir/uninstall.sh" --dry-run
        printf '\nThen it would delete %s.\nDry run: nothing was changed.\n' "$home_dir"
        return 0
    fi
    "$home_dir/uninstall.sh"
    rm -rf "$home_dir"
    printf 'deleted %s\n' "$home_dir"
}

main() {
    action=install
    dry_run=0
    for arg in "$@"; do
        case $arg in
            install|uninstall) action=$arg ;;
            -n|--dry-run) dry_run=1 ;;
            -h|--help) usage; exit 0 ;;
            *) usage >&2; exit 2 ;;
        esac
    done

    [ -n "${HOME:-}" ] || die 'HOME is not set'
    home_dir=${FILO_HOME:-"${XDG_DATA_HOME:-$HOME/.local/share}/filo"}
    check_home

    case $version in
        [0-9]*.[0-9]*.[0-9]*) ;;
        *) die 'this copy of get.sh was not made by a release: use the one attached to a release' ;;
    esac
    case $sha256 in
        *[!0-9a-f]*|"") die 'this copy of get.sh was not made by a release: use the one attached to a release' ;;
    esac
    [ "${#sha256}" -eq 64 ] || die 'this copy of get.sh has a malformed checksum'
    release_url=${FILO_RELEASE_URL:-"https://github.com/Galfurian/filo/releases/download/v$version"}

    trap cleanup EXIT
    trap 'exit 1' HUP INT TERM
    "${action}_filo"
}

# Everything is defined above and run here, so that a download cut short cannot run half.
main "$@"
