#!/usr/bin/env bash
# Every 4 hours: deterministic collection. No Claude usage.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
exec 9>logs/.collect.lock; flock -n 9 || { echo "$(date -Is) collect already running" >> logs/collect.log; exit 0; }
echo "=== $(date -Is) collect" >> logs/collect.log
python3 -m fic collect >> logs/collect.log 2>&1 || echo "$(date -Is) collect exit $? (partial failure; watermark held, next run retries)" >> logs/collect.log
