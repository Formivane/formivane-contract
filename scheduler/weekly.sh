#!/usr/bin/env bash
# Sunday 18:07 Europe/London: learning cycle.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
exec 9>logs/.ai.lock; flock -n 9 || { echo "$(date -Is) AI job already running" >> logs/weekly.log; exit 0; }
echo "=== $(date -Is) weekly" >> logs/weekly.log
python3 -m fic learn >> logs/weekly.log 2>&1   # zero-token part always runs
claude -p "/fic-weekly" --allowedTools "Bash(python -m fic:*)" "Bash(python3 -m fic:*)" "Agent" "Read" >> logs/weekly.log 2>&1 || echo "claude exit $?" >> logs/weekly.log
