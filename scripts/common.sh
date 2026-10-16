# shellcheck shell=sh
# shellcheck disable=SC2154,SC2034  # repo_dir comes from the caller; tab is used by the caller
# Shared by install.sh and uninstall.sh. Sourced, not run.
# The caller sets $repo_dir (this checkout) before sourcing.

bin_dir=${FILO_BIN_DIR:-"$HOME/.local/bin"}
claude_skills=${FILO_CLAUDE_SKILLS:-"$HOME/.claude/skills"}
agents_skills=${FILO_AGENTS_SKILLS:-"$HOME/.agents/skills"}
agy_skills=${FILO_AGY_SKILLS:-"$HOME/.gemini/antigravity-cli/skills"}
opencode_dir=${FILO_OPENCODE_DIR:-"$HOME/.config/opencode"}
skill_src="$repo_dir/skills/filo"
tab=$(printf '\t')

# The only links filo ever creates or removes, one per line:
#   what <TAB> target <TAB> source <TAB> client directory <TAB> client command
# The last two fields are empty for the filo command, whose directory is created.
# A skill is linked only for a client that is there: its own directory exists,
# or its command is in the PATH. So installing filo never creates a
# configuration directory for a program you do not have, and a client that is
# installed but has not made its directory yet is not missed.
links() {
    printf '%s\t%s\t%s\t%s\t%s\n' \
        "the filo command" "$bin_dir/filo" "$repo_dir/filo.py" "" "" \
        "the skill for Claude" "$claude_skills/filo" "$skill_src" "$(dirname "$claude_skills")" claude \
        "the skill for Codex" "$agents_skills/filo" "$skill_src" "$(dirname "$agents_skills")" codex \
        "the skill for agy" "$agy_skills/filo" "$skill_src" "$(dirname "$agy_skills")" agy \
        "the plugin for OpenCode" "$opencode_dir/plugins/filo.js" "$repo_dir/plugins/opencode/filo.js" "$opencode_dir" opencode \
        "the /filo command for OpenCode" "$opencode_dir/commands/filo.md" "$repo_dir/commands/opencode/filo.md" "$opencode_dir" opencode
}

# $1 as an absolute path with every link in its directories followed; it need not exist itself.
physical() {
    _dir=$(cd -P -- "$(dirname -- "$1")" 2>/dev/null && pwd -P) || return 1
    printf '%s/%s\n' "$_dir" "${1##*/}"
}

# True when $1 is a symlink that points at $2, by the same name or by the same place (a checkout that
# is reached through a link of its own is the same checkout).
owned() {
    [ -L "$1" ] || return 1
    _target=$(readlink "$1")
    [ "$_target" = "$2" ] && return 0
    case $_target in
        /*) ;;
        *) _target="$(dirname -- "$1")/$_target" ;;
    esac
    _here=$(physical "$_target") && _there=$(physical "$2") && [ "$_here" = "$_there" ]
}

# What is at $1 compared with what filo wants there ($2), for the client whose
# directory is $3 and whose command is $4.
# One of: current (already ours), conflict (something else is there),
# absent (the client is not installed), new.
state() {
    if owned "$1" "$2"; then
        echo current
    elif [ -e "$1" ] || [ -L "$1" ]; then
        echo conflict
    elif [ -n "$3" ] && [ ! -d "$3" ] && ! command -v "$4" >/dev/null 2>&1; then
        echo absent
    else
        echo new
    fi
}
