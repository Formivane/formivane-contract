#!/usr/bin/env bash
# 09:07 Europe/London: AI evaluation via Claude Code headless (uses your Claude subscription login, no API key).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
exec 9>logs/.ai.lock; flock -n 9 || { echo "$(date -Is) AI job already running" >> logs/daily.log; exit 0; }
echo "=== $(date -Is) daily" >> logs/daily.log
# Budget is enforced inside `python -m fic task next` (exit 3 when exhausted), so this cannot loop.
claude -p "/fic-daily" --allowedTools "Bash(python -m fic:*)" "Bash(python3 -m fic:*)" "Agent" "Read" "WebFetch" "WebSearch" >> logs/daily.log 2>&1 || echo "claude exit $?" >> logs/daily.log
python3 -m fic report daily > /dev/null
