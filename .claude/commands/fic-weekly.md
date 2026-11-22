---
description: FIC weekly learning cycle. Deterministic diagnosis + rule testing first, then one bounded Learning Auditor batch.
allowed-tools: Bash(python -m fic:*), Bash(python3 -m fic:*), Agent, Read
---
You are the FIC orchestrator running the weekly learning cycle.

1. `python -m fic learn` (zero-token: diagnoses misclassifications from feedback, proposes and tests candidate rules on labelled history).
2. `python -m fic plan weekly`. If an audit task was created, spawn ONE `learning-auditor` subagent:
   "Run `python -m fic task next --kind audit`. If EMPTY or BUDGET_EXHAUSTED, stop. Review the runs in the packet and submit JSON via
   python -m fic task submit <task_id> <<'JSON' ... JSON
   Reply with: runs reviewed, errors found, rules proposed (ids from the submit result)."
3. `python -m fic learn` again so auditor-proposed rules get tested.
4. Tell the user (max 12 lines): accuracy before/after, rules awaiting approval (id + one-line why), rules auto-rejected by tests, and remind them to approve/reject in the dashboard Learning Centre or with `python -m fic rules approve|reject <id>`.
Never approve rules yourself.
