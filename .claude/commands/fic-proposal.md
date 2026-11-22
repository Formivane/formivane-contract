---
description: Draft proposals for opportunities the owner requested (queued 'proposal' tasks).
allowed-tools: Bash(python -m fic:*), Bash(python3 -m fic:*), Agent
---
1. `python -m fic task list` — if no queued `proposal` task, tell the user to request one (dashboard button or `python -m fic proposal request <opp_id>`) and stop.
2. Spawn ONE `proposal-specialist` subagent: "Run `python -m fic task next --kind proposal`, draft the response from the packet, submit JSON with `python -m fic task submit <id> <<'JSON' ... JSON`. Report proposal title, missing_info count, qc result. Process at most 2 tasks."
3. Tell the user the draft is in the dashboard Proposals tab for review. Nothing is submitted to any buyer.
