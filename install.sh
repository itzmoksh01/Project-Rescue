#!/bin/sh
# Project Rescue installer:  curl -fsSL https://raw.githubusercontent.com/itzmoksh01/project-rescue/main/install.sh | sh
set -eu
RAW="https://raw.githubusercontent.com/itzmoksh01/project-rescue/main/project_rescue.py"
command -v git >/dev/null 2>&1 || { echo "Project Rescue needs git. Install git first."; exit 1; }
PY=$(command -v python3 || command -v python || true)
[ -n "$PY" ] || { echo "Project Rescue needs Python 3.8+. Install Python first."; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || { echo "Python 3.8+ required."; exit 1; }
DIR="$HOME/.project-rescue"
mkdir -p "$DIR"
HERE=$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo "")
if [ -n "$HERE" ] && [ -f "$HERE/project_rescue.py" ]; then
  cp "$HERE/project_rescue.py" "$DIR/project_rescue.py"
else
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$RAW" -o "$DIR/project_rescue.py"
  elif command -v wget >/dev/null 2>&1; then wget -qO "$DIR/project_rescue.py" "$RAW"
  else echo "Need curl or wget to download project_rescue.py"; exit 1; fi
fi
exec "$PY" "$DIR/project_rescue.py" install
