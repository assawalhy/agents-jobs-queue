#!/bin/sh
# ajq: PreToolUse guard. Classifies the Bash command in the stdin payload and,
# when it is heavy, steers the model to `ajq submit`.
#
# Usage: ajq-pre-tool-use.sh <harness>   (payload arrives on stdin)
#
# This runs inside somebody else's agent turn, on the critical path of a tool
# call, so: bounded work (`timeout 5` per CLI call, `timeout 2` on stdin), no jq
# dependency, and every error path exits 0 quietly. The one exception is a
# deliberate kiro block, which is exit 2 plus a reason on stderr.
#
# hooks.guard_mode (ajq config, default "warn"):
#   off   -> print nothing, exit 0
#   warn  -> model-visible context naming the ajq submit equivalent, exit 0
#   block -> deny the tool call with a reason naming ajq submit
#
# Safety rails: a command that already mentions ajq is never touched, and
# nothing is ever blocked unless the daemon actually answers.
#
# Envelopes (verified upstream):
#   claude/codex context -> {"hookSpecificOutput":{"hookEventName":"PreToolUse",
#                             "additionalContext":"<text>"}}
#   claude/codex block   -> {"hookSpecificOutput":{"hookEventName":"PreToolUse",
#                             "permissionDecision":"deny",
#                             "permissionDecisionReason":"<reason>"}}
#   kiro context -> plain stdout; kiro block -> exit 2 with the reason on stderr
#
# CLI resolution: $AJQ_BIN, else the literal @AJQ_BIN@ placeholder if this file
# still carries one, else `ajq` from PATH.

set -eu

HARNESS="${1:-unknown}"

if command -v timeout >/dev/null 2>&1; then
    run() { timeout 5 "$@"; }
    drain() { timeout 2 cat 2>/dev/null || :; }
else
    run() { "$@"; }
    drain() { cat 2>/dev/null || :; }
fi

json_string() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

if [ -t 0 ]; then
    payload=""
else
    payload="$(drain)"
fi

ajq=""
if [ -n "${AJQ_BIN:-}" ]; then
    ajq="$AJQ_BIN"
else
    case "$0" in
        *@AJQ_BIN@*) ajq="@AJQ_BIN@" ;;
        *) ajq="ajq" ;;
    esac
fi
case "$ajq" in
    */*) [ -x "$ajq" ] || exit 0 ;;
    *) command -v "$ajq" >/dev/null 2>&1 || exit 0 ;;
esac

# `.tool_input.command` is the only field we need. Pull it out with a small JSON
# string scanner instead of a regex, so a command containing escaped quotes
# (`npm test -- --grep \"foo\"`) survives intact and still gets classified.
command="$(printf '%s' "$payload" | awk '
    {
        s = $0
        i = index(s, "\"command\"")
        if (i == 0) next
        s = substr(s, i + 9)
        while (match(s, /^[ \t]*:[ \t]*/)) s = substr(s, RLENGTH + 1)
        if (substr(s, 1, 1) != "\"") next
        s = substr(s, 2)
        out = ""
        while (length(s) > 0) {
            c = substr(s, 1, 1)
            if (c == "\\") {
                n = substr(s, 2, 1)
                if (n == "n" || n == "t" || n == "r") out = out " "
                else if (n == "u") { out = out "?"; s = substr(s, 7); continue }
                else out = out n
                s = substr(s, 3)
                continue
            }
            if (c == "\"") { print out; found = 1; exit }
            out = out c
            s = substr(s, 2)
        }
    }
    END { if (!found) exit 1 }
')" || exit 0
[ -n "$command" ] || exit 0

# Our own commands are never in scope.
case "$command" in
    *ajq*) exit 0 ;;
esac

verdict="$(run "$ajq" guard --explain "$command" 2>/dev/null)" || exit 0

heavy="$(printf '%s' "$verdict" | sed -n 's/^heavy:[[:space:]]*//p' | head -n 1)"
[ "$heavy" = "true" ] || exit 0

kind="$(printf '%s' "$verdict" | sed -n 's/^kind:[[:space:]]*//p' | head -n 1)"
[ -n "$kind" ] || kind="unknown"

cfg="$(run "$ajq" config 2>/dev/null)" || exit 0
mode="$(printf '%s' "$cfg" \
    | sed -n 's/^hooks\.guard_mode[[:space:]]*\([a-zA-Z]*\).*/\1/p' \
    | head -n 1)"
case "$mode" in
    off | warn | block) ;;
    *) mode="warn" ;;
esac
if [ "$mode" = "off" ]; then
    exit 0
fi

# A shell-ish command has to keep its metacharacters, so it needs --shell and a
# quoted argument; otherwise the agent's own shell would split the line anyway.
# Flags always go before `--`, so the tail is built separately from them.
tail="-- $command"
case "$command" in
    *'|'* | *'&&'* | *';'* | *'>'* | *'<'* | *'`'* | *'$('*)
        tail="--shell -- \"$command\""
        ;;
esac
suggest="ajq submit $tail"
note="ajq: '$command' is classified ${kind} (heavy) and is expected to be slow. Run it through the jobs queue instead: ${suggest}. Then check it with 'ajq status <id> --json', block on it with 'ajq wait <id>', and read the captured output with 'ajq output <id>'."

if [ "$mode" = "block" ]; then
    # Never deny a tool call on a machine whose queue is not answering.
    run "$ajq" daemon status >/dev/null 2>&1 || exit 0
    reason="Blocked by ajq (hooks.guard_mode=block): '$command' is classified ${kind} (heavy). Submit it instead: ajq submit --label <name> [--timeout <seconds>] [--max-output-bytes <n>] $tail — then inspect it with 'ajq status <id> --json' and 'ajq output <id>'."
    esc="$(json_string "$reason")"
    case "$HARNESS" in
        kiro)
            printf '%s\n' "$reason" >&2
            exit 2
            ;;
        *)
            printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$esc"
            exit 0
            ;;
    esac
fi

esc="$(json_string "$note")"
case "$HARNESS" in
    kiro)
        printf '%s\n' "$note"
        ;;
    *)
        printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"%s"}}\n' "$esc"
        ;;
esac
exit 0
