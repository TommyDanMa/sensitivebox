"""Shell completion generated from public vocabulary, scenarios and configured rule IDs."""

import shlex
from collections.abc import Sequence
from dataclasses import dataclass

from sensitivebox.alerts import Rule
from sensitivebox.simulate import SCENARIOS

PUBLIC_COMMANDS = (
    "watch",
    "doctor",
    "demo",
    "apps",
    "rules",
    "completion",
    "mute",
    "unmute",
    "quiet",
    "report",
    "open",
    "enable",
    "disable",
    "log",
    "rule",
)


@dataclass(frozen=True, slots=True)
class Completion:
    shell: str
    script: str


def completion(shell: str, rules: Sequence[Rule]) -> Completion:
    arrays = "\n".join(
        f"    local -a {name}=({' '.join(shlex.quote(value) for value in values)})"
        for name, values in (
            ("public_commands", PUBLIC_COMMANDS),
            ("scenarios", tuple(SCENARIOS)),
            ("rule_ids", tuple(r.id for r in rules)),
        )
    )
    if shell == "bash":
        script = """# Sensitive Box completion for bash; source this file.
_sbox_complete() {
@ARRAYS@
    local current="${COMP_WORDS[COMP_CWORD]}" previous="${COMP_WORDS[COMP_CWORD-1]}"
    local command='' item i
    local -a candidates=(--json --help)
    COMPREPLY=()
    for item in "${COMP_WORDS[@]:1:COMP_CWORD-1}"; do
        case "$item" in
            watch|doctor|demo|apps|rules|completion|rule|mute|unmute|quiet|log|enable|disable|open|report)
                command="$item"; break ;;
        esac
    done
    if [[ "$previous" == --db ]]; then
        mapfile -t COMPREPLY < <(compgen -f -- "$current")
        return 0
    fi
    case "$command" in
        '') candidates+=("${public_commands[@]}" --version --db) ;;
        demo) candidates+=("${scenarios[@]}" --no-notify --interval) ;;
        watch) candidates+=(--interval --db) ;;
        doctor) candidates+=(--verbose) ;;
        rules) candidates+=(--db "${rule_ids[@]}") ;;
        report) candidates+=(--fixture --keep-app-names --output) ;;
        open) candidates+=(log rule settings "${rule_ids[@]}") ;;
        log) candidates+=(--since --limit --weekly --daemon --follow --db "${rule_ids[@]}") ;;
        quiet) candidates+=(off --no-renotify --db) ;;
        completion) candidates+=(bash zsh) ;;
        rule|mute|unmute) candidates+=("${rule_ids[@]}") ;;
    esac
    for item in "${candidates[@]}"; do
        [[ "$item" == "$current"* ]] && COMPREPLY+=("$item")
    done
    return 0
}
complete -F _sbox_complete sbox sensitivebox
"""
    else:
        script = """#compdef sbox sensitivebox
# Sensitive Box completion for zsh; initialize compinit, then source this file.
_sbox_complete() {
@ARRAYS@
    local command='' item i
    local -a candidates=(--json --help)
    for ((i=2; i<CURRENT; i++)); do
        item="${words[i]}"
        case "$item" in
            watch|doctor|demo|apps|rules|completion|rule|mute|unmute|quiet|log|enable|disable|open|report)
                command="$item"; break ;;
        esac
    done
    if [[ "${words[CURRENT-1]}" == --db ]]; then
        _files
        return
    fi
    case "$command" in
        '') candidates+=("${public_commands[@]}" --version --db) ;;
        demo) candidates+=("${scenarios[@]}" --no-notify --interval) ;;
        watch) candidates+=(--interval --db) ;;
        doctor) candidates+=(--verbose) ;;
        rules) candidates+=(--db "${rule_ids[@]}") ;;
        report) candidates+=(--fixture --keep-app-names --output) ;;
        open) candidates+=(log rule settings "${rule_ids[@]}") ;;
        log) candidates+=(--since --limit --weekly --daemon --follow --db "${rule_ids[@]}") ;;
        quiet) candidates+=(off --no-renotify --db) ;;
        completion) candidates+=(bash zsh) ;;
        rule|mute|unmute) candidates+=("${rule_ids[@]}") ;;
    esac
    compadd -- "${candidates[@]}"
}
compdef _sbox_complete sbox sensitivebox
"""
    return Completion(shell, script.replace("@ARRAYS@", arrays))
