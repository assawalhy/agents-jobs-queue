#!/bin/sh
# ajq: SessionStart hook. Ensures the jobs daemon, then hands the model a short
# statement that heavy work belongs in the queue.
#
# Usage: ajq-session-start.sh <harness>   (payload arrives on stdin)
#
# The stdin payload is drained, never parsed: its shape differs per harness and a
# hook must not depend on it. Like every hook here, this runs inside somebody
# else's agent turn: total work is bounded by `timeout 5` per CLI call and every
# error path exits 0 quietly. Nothing is printed when the queue state cannot be
# read, because a half-true context line is worse than none.
#
# Envelopes (verified upstream):
#   claude/codex -> {"hookSpecificOutput":{"hookEventName":"SessionStart",
#                  "additionalContext":"<text>"}} on stdout
#   kiro         -> plain stdout text, which kiro adds to the agent's context
#
# CLI resolution: $AJQ_BIN, else the literal @AJQ_BIN@ placeholder if this file
# still carries one, else `ajq` from PATH.

set -eu

HARNESS="${1:-unknown}"

if command -v timeout >/dev/null 2>&1; then
    run() { timeout 5 "$@"; }
    drain() { timeout 2 cat >/dev/null 2>&1 || :; }
else
    run() { "$@"; }
    drain() { cat >/dev/null 2>&1 || :; }
fi

# Drain the payload without ever blocking on a TTY or an unclosed pipe.
if [ ! -t 0 ]; then
    drain
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

run "$ajq" daemon ensure >/dev/null 2>&1 || exit 0

listing="$(run "$ajq" list --json 2>/dev/null)" || exit 0

running="$(printf '%s' "$listing" | grep -o '"state": "running"' | wc -l | tr -d '[:space:]')"
queued="$(printf '%s' "$listing" | grep -o '"state": "queued"' | wc -l | tr -d '[:space:]')"
case "$running" in '' | *[!0-9]*) running=0 ;; esac
case "$queued" in '' | *[!0-9]*) queued=0 ;; esac

text="ajq jobs queue is up: ${running} running, ${queued} queued. Builds, tests, typechecks, lints, installs, docker builds and anything else you expect to take more than about 30 seconds must be submitted with 'ajq submit -- <command>' instead of being run in the shell; the daemon caps concurrency, memory, CPU and output size so parallel agents do not exhaust the machine. Track a job with 'ajq status <id> --json', block on it with 'ajq wait <id>', and read its output with 'ajq output <id>'. Fast reads (git status/diff/log, rg, ls, cat, jq) stay inline."

case "$HARNESS" in
    kiro)
        printf '%s\n' "$text"
        ;;
    *)
        # One line of JSON, so the text is escaped here rather than by hand.
        esc="$(printf '%s' "$text" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
        printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$esc"
        ;;
esac
exit 0
