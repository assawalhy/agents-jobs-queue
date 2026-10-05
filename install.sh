#!/usr/bin/env bash
# ajq installer — jobs-queue daemon for heavy agent tasks.
#
# Installs:
#   * the `ajq` CLI as a stdlib-only zipapp (no build step, no dependencies)
#   * the daemon as a systemd user unit (Linux) or a launchd agent (macOS),
#     enabled at login/boot and started now
#   * a user-owned ~/.config/ajq/config.json (seeded only when absent)
#   * the `ajq` skill into every detected harness
#   * hooks: opencode plugin, claude/codex JSON hook entries, kiro hooks file,
#     pi extension — merged non-destructively, never clobbering other tooling
#
# Usage: ./install.sh [--all|--target a,b] [--no-daemon] [--uninstall]
#                     [--purge] [--force-config] [--dry-run] [--help]

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$SCRIPT_DIR/MANIFEST.txt"
PLUGIN="ajq"
# must match the config default (daemon.unit) in src/ajq/config.py
UNIT_NAME="ajqd.service"

XDG_DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
XDG_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
PREFIX="$XDG_DATA/$PLUGIN"
BIN_DIR="$HOME/.local/bin"
HOOK_DIR="$PREFIX/hooks"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/$PLUGIN"
CONFIG_FILE="$XDG_CONFIG/$PLUGIN/config.json"
REGISTRY="$PREFIX/installed.txt"
PLIST="$HOME/Library/LaunchAgents/io.$PLUGIN.ajqd.plist"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/$UNIT_NAME"
LABEL="io.$PLUGIN.ajqd"
# Identifies our own hook entries inside JSON files we do not own. Matched as a
# regex, so it must be anchored to our script names, not the install prefix.
MARKER='ajq-(session-start|pre-tool-use|ensure)\.sh'

PYTHON="$(command -v python3 || true)"
AJQ_BIN="$BIN_DIR/$PLUGIN"
DRY_RUN=0
PURGE=0
FORCE_CONFIG=0
WITH_DAEMON=1

log()  { printf '%s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
run()  { [ "$DRY_RUN" = 1 ] && { printf '    would run: %s\n' "$*"; return 0; }; "$@"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
  # the header comment block, up to the first blank line
  sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --all            install into every detected harness (non-interactive)
  --target a,b     install into the listed harnesses
  --no-daemon      install the CLI and integrations only; do not touch the unit
  --uninstall      remove installed files, hook entries and the daemon unit
  --purge          with --uninstall: also delete state, config and estimate cache
  --force-config   overwrite an existing ~/.config/ajq/config.json
  --dry-run        print what would happen, change nothing
  --help           this message
EOF
}

platform_id() {
  case "$(uname -s)" in
    Linux)  echo linux ;;
    Darwin) echo macos ;;
    *)      echo unsupported ;;
  esac
}

PLATFORM="$(platform_id)"

# ---- harness table ----------------------------------------------------------
ALL_IDS=(opencode claude codex pi kiro kilo kimi deepseek cursor)

harness_installed() {
  case "$1" in
    opencode) [ -d "$HOME/.config/opencode" ] || command -v opencode >/dev/null 2>&1 && echo 1 ;;
    claude)   [ -d "$HOME/.claude" ]        || command -v claude   >/dev/null 2>&1 && echo 1 ;;
    codex)    [ -d "$HOME/.codex" ]        || command -v codex    >/dev/null 2>&1 && echo 1 ;;
    pi)       [ -d "$HOME/.pi/agent" ]     || command -v pi       >/dev/null 2>&1 && echo 1 ;;
    kiro)     [ -d "$HOME/.kiro" ]         || command -v kiro     >/dev/null 2>&1 && echo 1 ;;
    kilo)     [ -d "$HOME/.kilocode" ]     || command -v kilo     >/dev/null 2>&1 && echo 1 ;;
    kimi)     [ -d "$HOME/.kimi-code" ] && echo 1 ;;
    deepseek) [ -d "$HOME/.deepseek" ]     || command -v deepseek >/dev/null 2>&1 && echo 1 ;;
    cursor)   [ -d "$HOME/.cursor" ]       || command -v cursor   >/dev/null 2>&1 && echo 1 ;;
    *)        echo "" ;;
  esac
}

skill_dir() {
  case "$1" in
    opencode) echo "$HOME/.config/opencode/skills" ;;
    claude)   echo "$HOME/.claude/skills" ;;
    codex)    echo "$HOME/.codex/skills" ;;
    pi)       echo "$HOME/.pi/agent/skills" ;;
    kiro)     echo "$HOME/.kiro/skills" ;;
    kilo)     echo "$HOME/.kilocode/skills" ;;
    kimi)     echo "$HOME/.kimi-code/skills" ;;
    deepseek) echo "$HOME/.deepseek/skills" ;;
    cursor)   echo "$HOME/.cursor/skills" ;;
    *)        echo "" ;;
  esac
}

harness_label() {
  case "$1" in
    opencode) echo "OpenCode" ;;
    claude)   echo "Claude Code" ;;
    codex)    echo "Codex" ;;
    pi)       echo "Pi" ;;
    kiro)     echo "Kiro" ;;
    kilo)     echo "Kilo Code" ;;
    kimi)     echo "Kimi Code" ;;
    deepseek) echo "DeepSeek" ;;
    cursor)   echo "Cursor" ;;
    *)        echo "$1" ;;
  esac
}

# Only these harnesses have a verified hook surface. The rest get the skill only.
has_hooks() {
  case "$1" in opencode|claude|codex|kiro|pi) echo 1 ;; *) echo "" ;; esac
}

# ---- manifest ---------------------------------------------------------------
manifest_current() {
  local rp va vr
  [ -f "$MANIFEST" ] || die "MANIFEST.txt not found at $MANIFEST"
  while IFS='|' read -r rp va vr; do
    [ -z "$rp" ] && continue
    case "$rp" in \#*) continue ;; esac
    [ "${vr:--}" = "-" ] && echo "$rp"
  done < "$MANIFEST"
}

manifest_removed() {
  local rp va vr
  while IFS='|' read -r rp va vr; do
    [ -z "$rp" ] && continue
    case "$rp" in \#*) continue ;; esac
    [ "${vr:--}" != "-" ] && echo "$rp"
  done < "$MANIFEST"
}

registry_key() { echo "$1"; }
registry_get()  { grep -F "$1|" "$REGISTRY" 2>/dev/null | head -1; }
registry_set()  {
  # merge into the existing entry: one harness installs a skill AND hooks, and
  # those are written by separate steps
  local key="$1" root="$2"; shift 2
  mkdir -p "$(dirname "$REGISTRY")"
  local line="" existing_root="" existing_files=""
  line="$(registry_get "$key")"
  if [ -n "$line" ]; then
    existing_root="${line#*|}"; existing_root="${existing_root%|*}"
    existing_files="${line##*|}"
    [ -z "$root" ] && root="$existing_root"
  fi
  local files=" " item
  for item in $existing_files "$@"; do
    [ -z "$item" ] && continue
    case "$files" in *" $item "*) ;; *) files="$files$item " ;; esac
  done
  local tmp; tmp="$(mktemp)"
  grep -vF "$key|" "$REGISTRY" 2>/dev/null > "$tmp" || : > "$tmp"
  printf '%s|%s|%s\n' "$key" "$root" "${files% }" >> "$tmp"
  mv "$tmp" "$REGISTRY"
}
registry_drop() {
  mkdir -p "$(dirname "$REGISTRY")"
  local tmp; tmp="$(mktemp)"
  grep -vF "$1|" "$REGISTRY" 2>/dev/null > "$tmp" || : > "$tmp"
  mv "$tmp" "$REGISTRY"
  # leave no empty registry behind once the last target is gone
  [ -s "$REGISTRY" ] || rm -f "$REGISTRY"
}
registry_keys() { [ -f "$REGISTRY" ] && cut -d'|' -f1 "$REGISTRY" || true; }

# ---- shared files (hooks + runtime) -----------------------------------------
install_shared_files() {
  local rp src dest installed=()
  mkdir -p "$HOOK_DIR"
  while IFS= read -r rp; do
    [ -z "$rp" ] && continue
    case "$rp" in hooks/*) ;; *) continue ;; esac
    src="$SCRIPT_DIR/$rp"
    [ -f "$src" ] || continue
    dest="$PREFIX/$rp"        # keeps hooks/ so the path matches $HOOK_DIR
    mkdir -p "$(dirname "$dest")"
    if [ "$DRY_RUN" = 1 ]; then
      printf '    would install %s -> %s\n' "$rp" "$dest"
    else
      cp "$src" "$dest"
      chmod 0755 "$dest"
    fi
    installed+=("$rp")
  done < <(manifest_current)
  # prune hooks dropped from a newer release
  local rp2
  while IFS= read -r rp2; do
    case "$rp2" in hooks/*) rm -f "$PREFIX/$rp2" ;; esac
  done < <(manifest_removed)
  [ ${#installed[@]} -gt 0 ] && registry_set "shared" "$PREFIX" "${installed[@]}"
  return 0
}

uninstall_shared_files() {
  local line; line="$(registry_get shared)"
  local f
  for f in ${line##*|}; do
    rm -f "$PREFIX/$f"
  done
  for f in $(manifest_removed); do
    case "$f" in hooks/*) rm -f "$PREFIX/$f" ;; esac
  done
  registry_drop shared
}

# ---- CLI zipapp -------------------------------------------------------------
build_zipapp() {
  [ -n "$PYTHON" ] || die "python3 not found; ajq requires Python 3.11+"
  "$PYTHON" - <<'PY' || die "python3 >= 3.11 is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
  mkdir -p "$BIN_DIR"
  local stage; stage="$(mktemp -d)"
  mkdir -p "$stage/ajq_pkg"
  cp -R "$SCRIPT_DIR/src/ajq" "$stage/ajq_pkg/ajq"
  find "$stage" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null
  find "$stage" -name '*.pyc' -delete 2>/dev/null
  if [ "$DRY_RUN" = 1 ]; then
    printf '    would build zipapp %s -> %s (interpreter %s)\n' \
      "$stage/ajq_pkg" "$AJQ_BIN" "$("$PYTHON" -c 'import sys;print(sys.executable)')"
    rm -rf "$stage"
    return 0
  fi
  "$PYTHON" -m zipapp "$stage/ajq_pkg" \
      -m "ajq.cli:main" \
      -p "$("$PYTHON" -c 'import sys;print(sys.executable)')" \
      -o "$AJQ_BIN" || die "zipapp build failed"
  chmod 0755 "$AJQ_BIN"
  rm -rf "$stage"
  log "  + cli: $AJQ_BIN"
}

# ---- config -----------------------------------------------------------------
install_config() {
  mkdir -p "$(dirname "$CONFIG_FILE")"
  if [ -f "$CONFIG_FILE" ] && [ "$FORCE_CONFIG" = 0 ]; then
    log "  = config: kept existing $CONFIG_FILE"
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    printf '    would write %s\n' "$CONFIG_FILE"
    return 0
  fi
  if [ "$FORCE_CONFIG" = 1 ]; then
    "$AJQ_BIN" config --seed --force >/dev/null 2>&1 || seed_config_fallback
  else
    "$AJQ_BIN" config --seed >/dev/null 2>&1 || seed_config_fallback
  fi
  log "  + config: $CONFIG_FILE"
}

# Used when the freshly built CLI cannot seed the file itself (e.g. a broken
# interpreter path in the zipapp shebang). Same defaults, no daemon needed.
seed_config_fallback() {
  "$PYTHON" - <<PY
import json, os, sys
sys.path.insert(0, "$SCRIPT_DIR/src")
from ajq.config import DEFAULTS
path = "$CONFIG_FILE"
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w", encoding="utf-8") as handle:
    json.dump(DEFAULTS, handle, indent=2)
    handle.write("\n")
PY
}

# ---- daemon: systemd (linux) / launchd (macos) ------------------------------
unit_path() { echo "$UNIT"; }

write_unit() {
  local interp; interp="$("$PYTHON" -c 'import sys;print(sys.executable)')"
  # The systemd user manager on this machine has a PATH without ~/.local/bin, so
  # ExecStart is absolute. PATH is passed through explicitly so jobs can find
  # npm/cargo/mise-installed tools.
  cat > "$UNIT" <<EOF
[Unit]
Description=ajq jobs-queue daemon (heavy agent tasks)
After=default.target

[Service]
Type=simple
ExecStart=$AJQ_BIN daemon serve
Environment=PATH=$PATH
Restart=always
RestartSec=2
KillMode=mixed
TimeoutStopSec=60

[Install]
WantedBy=default.target
EOF
  sed -i "s|^Environment=PATH=.*|Environment=PATH=$(printf '%s' "$PATH" | sed 's/[&|]/\\&/g')|" \
    "$UNIT"
  log "  + unit: $UNIT (python: $interp)"
}

install_systemd() {
  mkdir -p "$UNIT_DIR"
  if [ "$DRY_RUN" = 1 ]; then
    printf '    would write unit %s and enable+start it\n' "$UNIT"
  else
    write_unit
    systemctl --user daemon-reload >/dev/null 2>&1
    systemctl --user enable "$UNIT_NAME" >/dev/null 2>&1 \
      || warn "could not enable the user unit"
    systemctl --user restart "$UNIT_NAME" >/dev/null 2>&1 \
      || warn "could not start the user unit; 'ajq daemon ensure' will start it lazily"
  fi
  # Without linger the user manager stops at logout and there is no boot start.
  if ! loginctl show-user "$(id -un)" 2>/dev/null | grep -q "Linger=yes"; then
    if [ "$DRY_RUN" = 1 ]; then
      printf '    would enable linger for %s\n' "$(id -un)"
    elif loginctl enable-linger "$(id -un)" 2>/dev/null; then
      log "  + linger: enabled for $(id -un) (daemon now starts at boot)"
    else
      warn "could not enable linger; the daemon starts on first agent session instead"
    fi
  fi
}

write_plist() {
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$AJQ_BIN</string>
    <string>daemon</string>
    <string>serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$PATH</string>
  </dict>
  <key>StandardOutPath</key><string>$STATE_DIR/ajqd.log</string>
  <key>StandardErrorPath</key><string>$STATE_DIR/ajqd.log</string>
</dict>
</plist>
EOF
  log "  + launchd agent: $PLIST"
}

install_launchd() {
  mkdir -p "$STATE_DIR"
  if [ "$DRY_RUN" = 1 ]; then
    printf '    would write %s and bootstrap it\n' "$PLIST"
    return 0
  fi
  write_plist
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1
  launchctl bootstrap "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 \
    || launchctl kickstart "gui/$(id -u)/$LABEL" >/dev/null 2>&1 \
    || warn "could not bootstrap the launch agent; 'ajq daemon ensure' will start it lazily"
}

install_daemon() {
  [ "$WITH_DAEMON" = 1 ] || { log "  = daemon: skipped (--no-daemon)"; return 0; }
  case "$PLATFORM" in
    linux) install_systemd ;;
    macos) install_launchd ;;
    *) warn "unsupported platform $(uname -s); skipping the boot service" ;;
  esac
}

uninstall_daemon() {
  case "$PLATFORM" in
    linux)
      systemctl --user disable --now "$UNIT_NAME" >/dev/null 2>&1
      rm -f "$UNIT"
      systemctl --user daemon-reload >/dev/null 2>&1
      log "  - unit removed"
      ;;
    macos)
      launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1
      rm -f "$PLIST"
      log "  - launch agent removed"
      ;;
  esac
}

# ---- JSON hook merge (claude settings.json, codex hooks.json) ---------------
# Removes only entries carrying our marker, then appends ours. Everything else
# in the file (other plugins, other hooks) is preserved byte-for-byte.
json_merge() {
  local file="$1" event_start_cmd="$2" event_guard_cmd="$3" mode="${4:-install}"
  [ -n "$file" ] || return 0
  command -v jq >/dev/null 2>&1 || { warn "jq is required to edit $file; skipped"; return 0; }
  if [ "$mode" = install ]; then
    mkdir -p "$(dirname "$file")"
    [ -f "$file" ] || echo '{}' > "$file"
  else
    [ -f "$file" ] || return 0
  fi
  local backup="$file.$PLUGIN-bak"
  [ "$DRY_RUN" = 1 ] || cp "$file" "$backup"
  local tmp; tmp="$(mktemp)"
  local start_arg guard_arg
  if [ "$mode" = install ]; then
    start_arg="$event_start_cmd"; guard_arg="$event_guard_cmd"
  else
    start_arg="null"; guard_arg="null"   # scrub-only branch; jq needs them defined
  fi
  local -a args=(--arg marker "$MARKER" --arg start_cmd "$start_arg" --arg guard_cmd "$guard_arg")
  if ! jq "${args[@]}" '
        def scrub:
          if (type == "object" and has("hooks") and (.hooks | type == "object")) then
            .hooks |= with_entries(
              .value |= (if type == "array" then
                map(
                  if (type == "object" and has("hooks")) then
                    .hooks = ((.hooks // [])
                              | map(select(((.command? // "") | test($marker)) | not)))
                    | select((.hooks | length) > 0)
                  else . end)
                else . end)
            )
          else . end;
        scrub
        | if $start_cmd == null then .
          else
            .hooks = (.hooks // {})
            | .hooks.SessionStart = ((.hooks.SessionStart // [])
                + [{matcher: "startup|resume|clear|compact|fork",
                    hooks: [{type: "command", command: $start_cmd, timeout: 5}]}])
            | .hooks.PreToolUse = ((.hooks.PreToolUse // [])
                + [{matcher: "Bash",
                    hooks: [{type: "command", command: $guard_cmd, timeout: 5}]}])
          end' "$file" > "$tmp" 2>/dev/null; then
    warn "jq failed on $file; leaving it untouched"
    rm -f "$tmp"
    return 0
  fi
  if ! jq empty "$tmp" 2>/dev/null; then
    warn "merged JSON for $file did not validate; restoring the backup"
    rm -f "$tmp"
    return 0
  fi
  [ "$DRY_RUN" = 1 ] && { printf '    would merge hooks into %s\n' "$file"; rm -f "$tmp"; return 0; }
  mv "$tmp" "$file"
  log "  $( [ "$mode" = install ] && echo '+' || echo '-' ) hooks merged: $file"
}

hook_commands() {
  START_CMD="$HOOK_DIR/ajq-session-start.sh"
  GUARD_CMD="$HOOK_DIR/ajq-pre-tool-use.sh"
}

# ---- per-harness install ----------------------------------------------------
install_skill() {
  local id="$1" dir src="$SCRIPT_DIR/skills/ajq/SKILL.md"
  dir="$(skill_dir "$id")"
  [ -n "$dir" ] || return 0
  [ -f "$src" ] || { warn "skills/ajq/SKILL.md missing"; return 0; }
  if [ "$DRY_RUN" = 1 ]; then
    printf '    would install skill -> %s\n' "$dir/ajq/SKILL.md"
    return 0
  fi
  mkdir -p "$dir/ajq"
  cp "$src" "$dir/ajq/SKILL.md"
  registry_set "$id" "$dir" "skills/ajq/SKILL.md"
}

install_harness_hooks() {
  local id="$1" root dest installed=()
  hook_commands
  case "$id" in
    opencode)
      root="$HOME/.config/opencode/plugins"
      dest="$root/$PLUGIN.js"
      [ -f "$SCRIPT_DIR/harnesses/opencode/$PLUGIN.js" ] || return 0
      [ "$DRY_RUN" = 1 ] && { printf '    would install plugin -> %s\n' "$dest"; return 0; }
      mkdir -p "$root"; cp "$SCRIPT_DIR/harnesses/opencode/$PLUGIN.js" "$dest"
      installed+=("harnesses/opencode/$PLUGIN.js")
      ;;
    pi)
      root="$HOME/.pi/agent/extensions"
      dest="$root/$PLUGIN.ts"
      [ -f "$SCRIPT_DIR/harnesses/pi/$PLUGIN.ts" ] || return 0
      [ "$DRY_RUN" = 1 ] && { printf '    would install extension -> %s\n' "$dest"; return 0; }
      mkdir -p "$root"; cp "$SCRIPT_DIR/harnesses/pi/$PLUGIN.ts" "$dest"
      installed+=("harnesses/pi/$PLUGIN.ts")
      ;;
    kiro)
      root="$HOME/.kiro/hooks"
      dest="$root/$PLUGIN.json"
      local tmpl="$SCRIPT_DIR/harnesses/kiro/$PLUGIN.json.in"
      [ -f "$tmpl" ] || return 0
      if [ "$DRY_RUN" = 1 ]; then
        printf '    would install hooks -> %s\n' "$dest"; return 0
      fi
      mkdir -p "$root"
      sed "s|@AJQ_BIN_HOOKS@|$HOOK_DIR|g" "$tmpl" > "$dest"
      jq empty "$dest" 2>/dev/null || warn "generated $dest is not valid JSON"
      installed+=("harnesses/kiro/$PLUGIN.json.in")
      ;;
    claude)
      root="$HOME/.claude"
      json_merge "$root/settings.json" "$START_CMD claude" "$GUARD_CMD claude" install
      installed+=("hooks:claude")
      ;;
    codex)
      root="$HOME/.codex"
      json_merge "$root/hooks.json" "$START_CMD codex" "$GUARD_CMD codex" install
      installed+=("hooks:codex")
      ;;
  esac
  [ ${#installed[@]} -gt 0 ] && {
    local prev; prev="$(registry_get "$id")"
    local root_saved="${prev#*|}"; root_saved="${root_saved%|*}"
    [ -n "$root_saved" ] && root="$root_saved"
    registry_set "$id" "$root" "${installed[@]}"
  }
  return 0
}

uninstall_harness() {
  local id="$1" line root f n=0
  line="$(registry_get "$id")"
  [ -n "$line" ] || { log "  - $(harness_label "$id"): nothing registered"; return 0; }
  root="${line#*|}"; root="${root%|*}"
  for f in ${line##*|}; do
    case "$f" in
      skills/*) dest="$(skill_dir "$id")/${f#skills/}" ;;
      harnesses/opencode/*) dest="$HOME/.config/opencode/plugins/$PLUGIN.js" ;;
      harnesses/pi/*)       dest="$HOME/.pi/agent/extensions/$PLUGIN.ts" ;;
      harnesses/kiro/*)     dest="$HOME/.kiro/hooks/$PLUGIN.json" ;;
      hooks:claude)         json_merge "$HOME/.claude/settings.json" "" "" uninstall
                          rm -f "$HOME/.claude/settings.json.$PLUGIN-bak"; continue ;;
      hooks:codex)          json_merge "$HOME/.codex/hooks.json" "" "" uninstall
                          rm -f "$HOME/.codex/hooks.json.$PLUGIN-bak"; continue ;;
      *) continue ;;
    esac
    [ -n "$dest" ] && [ -f "$dest" ] && rm -f "$dest" && n=$((n + 1))
  done
  # prune empty skill dir we created
  [ -d "$(skill_dir "$id")/ajq" ] && rmdir "$(skill_dir "$id")/ajq" 2>/dev/null
  registry_drop "$id"
  log "  - $(harness_label "$id"): removed $n file(s)"
}

install_harness() {
  local id="$1"
  log "  + $(harness_label "$id")"
  install_skill "$id"
  [ -n "$(has_hooks "$id")" ] && install_harness_hooks "$id"
  return 0
}

# ---- actions ----------------------------------------------------------------
do_install() {
  local targets=("$@")
  log "== ajq: install =="
  build_zipapp
  install_shared_files
  install_config
  install_daemon
  local id
  for id in "${targets[@]}"; do
    install_harness "$id"
  done
  log ""
  log "Installed. Next:"
  log "  ajq doctor                 # platform, limits, daemon state"
  log "  ajq submit -- pytest -q    # queue a task"
  log "  ajq stats                  # estimate-cache accuracy"
}

do_uninstall() {
  log "== ajq: uninstall =="
  local id
  for id in $(registry_keys); do
    [ "$id" = shared ] && continue
    uninstall_harness "$id"
  done
  uninstall_shared_files
  [ "$WITH_DAEMON" = 1 ] && uninstall_daemon
  rm -f "$AJQ_BIN"
  log "  - cli: $AJQ_BIN"
  [ -d "$HOOK_DIR" ] && rmdir "$HOOK_DIR" 2>/dev/null
  if [ "$PURGE" = 1 ]; then
    rm -rf "$PREFIX" "$STATE_DIR" "$XDG_CONFIG/$PLUGIN"
    log "  - purged state, config and estimate cache"
  else
    log "  = kept $STATE_DIR (add --purge to delete jobs and the estimate cache)"
  fi
  log "Done."
}

main() {
  local mode=install targets=""
  while [ $# -gt 0 ]; do
    case "$1" in
      install|uninstall|--install) mode="install" ;;
      --uninstall) mode="uninstall" ;;
      --all) targets="ALL" ;;
      --target) targets="$2"; shift ;;
      --no-daemon) WITH_DAEMON=0 ;;
      --purge) PURGE=1 ;;
      --force-config) FORCE_CONFIG=1 ;;
      --dry-run) DRY_RUN=1 ;;
      --help|-h) usage; exit 0 ;;
      *) printf 'unknown arg: %s\n\n' "$1" >&2; usage; exit 1 ;;
    esac
    shift
  done
  [ -f "$MANIFEST" ] || die "MANIFEST.txt not found next to install.sh"

  if [ "$mode" = uninstall ]; then
    do_uninstall
    return 0
  fi

  local chosen=()
  if [ "$targets" = ALL ]; then
    local id
    for id in "${ALL_IDS[@]}"; do
      [ -n "$(harness_installed "$id")" ] && chosen+=("$id")
    done
  elif [ -n "$targets" ]; then
    IFS=',' read -ra chosen <<< "$targets"
  else
    local id
    for id in "${ALL_IDS[@]}"; do
      [ -n "$(harness_installed "$id")" ] && chosen+=("$id")
    done
    [ ${#chosen[@]} -eq 0 ] && die "no supported harnesses detected; use --target"
  fi
  do_install "${chosen[@]}"
}

main "$@"