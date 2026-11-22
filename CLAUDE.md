# FIC (Formivane Intelligence Centre)
- Everything goes through `python -m fic <cmd>`; never edit `data/fic.db` directly.
- Deterministic work (collection, filtering, eligibility, scoring, rule testing) is Python. Claude subagents only handle `task next` packets and submit JSON.
- Commands: /fic-daily, /fic-weekly, /fic-proposal. Agents in .claude/agents.
- Never submit tenders, email anyone, approve rules or proposals: owner only.
- Tests: `python -m pytest -q`.
