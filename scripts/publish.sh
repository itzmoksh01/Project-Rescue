#!/bin/sh
# Publish this folder as a PUBLIC open-source repo on your GitHub (needs: git, gh, and `gh auth login` done once).
set -eu
OWNER="${1:-itzmoksh01}"
NAME="${2:-rescue-backup}"
command -v gh >/dev/null 2>&1 || { echo "Install the GitHub CLI first: https://cli.github.com"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Run:  gh auth login   (then run this script again)"; exit 1; }
cd "$(dirname "$0")/.."
[ -d .git ] || { git init -q -b main && git add -A && git commit -qm "Project Rescue 1.0.0"; }
gh repo create "$OWNER/$NAME" --public --source=. --remote=origin --push \
  --description "Project Rescue: live backup + private GitHub mirror + one-command rescue for every project you build with AI coding agents"
gh repo edit "$OWNER/$NAME" --add-topic claude-code --add-topic backup --add-topic ai-agents --add-topic developer-tools --add-topic git --add-topic disaster-recovery >/dev/null 2>&1 || true
echo "Done: https://github.com/$OWNER/$NAME"
