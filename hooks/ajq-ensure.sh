#!/bin/sh
# ajq: start the jobs daemon if needed and report the queue in one line.
#
# Usage: ajq-ensure.sh <harness>
#
# This runs inside somebody else's agent turn. It must never wedge a session and
# must never fail loudly: every error path exits 0 with no output. Total work is
# bounded by `timeout 5` per CLI call.
#
# CLI resolution: $AJQ_BIN, else the literal @AJQ_BIN@ placeholder if this file
# still carries one, else `ajq` from PATH.

set -eu

# The harness name is accepted for interface symmetry with the other two hooks;
# the queue line is identical everywhere.
HARNESS="${1:-unknown}"

if command -v timeout >/dev/null 2>&1; then
    run() { timeout 5 "$@"; }
else
    run() { "$@"; }
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

printf 'ajq: queue up  running %s  queued %s  (%s)\n' "$running" "$queued" "$HARNESS"
exit 0
