#!/usr/bin/env bash
# Build assets/ajq-demo.gif from tools/demo-gif/demo.html.
#
# demo.html renders its timeline as a pure function of `t` (seconds), so frames
# are captured in order at a fixed viewport and assembled with ffmpeg. The whole
# build is deterministic: no CSS keyframe animations, no wall-clock dependence,
# so re-running this produces the same GIF.
#
#   tools/demo-gif/build.sh [out.gif] [--preview]
#
# Requires playwright-cli (bundled chromium) and ffmpeg. Both are optional dev
# tools; the committed GIF is what the README shows.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$ROOT/assets/ajq-demo.gif"
PREVIEW=0
for arg in "$@"; do
  case "$arg" in
    --preview) PREVIEW=1 ;;
    *)         OUT="$arg" ;;
  esac
done

WIDTH="${AJQ_DEMO_W:-960}"
HEIGHT="${AJQ_DEMO_H:-540}"
FPS="${AJQ_DEMO_FPS:-12}"
DURATION="${AJQ_DEMO_SECONDS:-23.6}"   # keep in sync with demo.html's last scene
PORT="${AJQ_DEMO_PORT:-8791}"

for tool in playwright-cli ffmpeg python3; do
  command -v "$tool" >/dev/null 2>&1 || { echo "$tool is required" >&2; exit 1; }
done

FRAMES="$(mktemp -d)"
cleanup() {
  [ -n "${SRV_PID:-}" ] && kill "$SRV_PID" 2>/dev/null || true
  playwright-cli close-all >/dev/null 2>&1 || true
  if [ "$PREVIEW" = 1 ]; then
    printf 'frames kept in %s\n' "$FRAMES"
  else
    rm -rf "$FRAMES"
  fi
}
trap cleanup EXIT

TOTAL=$(awk -v d="$DURATION" -v f="$FPS" 'BEGIN{printf "%d", d*f}')
echo "==> ${TOTAL} frames · ${WIDTH}x${HEIGHT} · ${FPS}fps · ${DURATION}s"

( cd "$HERE" && exec python3 -m http.server "$PORT" >/dev/null 2>&1 ) &
SRV_PID=$!
sleep 1

# one browser session for the whole capture; a session per frame would be 500x slower
if ! playwright-cli open "http://127.0.0.1:$PORT/demo.html" --browser chromium; then
  echo "could not open the page on port $PORT (already in use?)" >&2
  exit 1
fi
playwright-cli resize "$WIDTH" "$HEIGHT" >/dev/null 2>&1

i=0
while [ "$i" -lt "$TOTAL" ]; do
  t=$(awk -v i="$i" -v f="$FPS" 'BEGIN{printf "%.4f", i/f}')
  playwright-cli eval "() => { window.renderFrame($t); return 'ok'; }" >/dev/null 2>&1
  if ! playwright-cli screenshot --filename "$(printf '%s/frame-%05d.png' "$FRAMES" "$i")" >/dev/null; then
    echo "frame $i (t=$t) failed to capture" >&2
    exit 1
  fi
  i=$((i + 1))
  if [ $((i % 50)) -eq 0 ]; then printf '    %d/%d\n' "$i" "$TOTAL"; fi
done

mkdir -p "$(dirname "$OUT")"
echo "==> encoding"
ffmpeg -y -loglevel error -framerate "$FPS" -i "$FRAMES/frame-%05d.png" \
  -vf "fps=$FPS,scale=$WIDTH:$HEIGHT:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" \
  -loop 0 "$OUT"

printf '==> %s (%s)\n' "$OUT" "$(du -h "$OUT" | cut -f1)"