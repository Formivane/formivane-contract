# Formivane Intelligence Centre (FIC)

Finds UK public-sector digital work, qualifies it, helps draft bids, and learns from verified mistakes and real results.

Design rule: **Python does everything repetitive and checkable; Claude only does judgement**, through Claude Code subagents on your Pro subscription. No Anthropic API key, no paid services.

```
 every 4h (no Claude)        09:07 daily (Claude)              Sun 18:07 (mostly no Claude)
 ┌──────────────────┐       ┌────────────────────────┐        ┌─────────────────────────────┐
 │ collect (OCDS)   │──────▶│ plan -> only needed    │──────▶ │ feedback -> diagnose ->     │
 │ dedupe, filter,  │ queue │ subagents -> QC'd JSON │ outputs│ propose rule -> test on     │
 │ eligibility,     │       │ -> shortlist report    │        │ labelled history -> YOU     │
 │ score            │       └────────────────────────┘        │ approve / reject / rollback │
 └──────────────────┘                                          └─────────────────────────────┘
                     all state in data/fic.db (SQLite) + knowledge/ (git)
```

## Setup (your machine)

Needs Python 3.11+ and Claude Code logged in with your Pro account.

```bash
git clone <this repo> && cd formivane-contract
python -m pip install -r requirements.txt      # only 'requests' (+ pytest for tests)
python -m pytest -q                            # 19 tests
python -m fic init
python -m fic collect                          # first live pull: last 7 days, ~20s
python -m fic dashboard                        # http://127.0.0.1:8765
```

**Then fill in `config/company_profile.toml`.** Until you do, every detected requirement is "requires verification". Mark only what you can evidence. `false` makes matching tenders Ineligible.

Want to see the learning loop before you have real feedback? `python scripts/demo.py` then `FIC_DB=data/demo.db python -m fic dashboard` (simulated, separate DB, clearly labelled).

## Data sources (verified 22 Nov 2026)

| Source | Status | How |
|---|---|---|
| Find a Tender | **Live** | `GET /api/1.0/ocdsReleasePackages` (updatedFrom/updatedTo, stages, cursor). Also carries above-threshold Scottish and Welsh notices. |
| Contracts Finder | **Live** | `GET /Published/Notices/OCDS/Search` (publishedFrom/publishedTo, stages, cursor) |
| Award notices (both) | **Live** | same APIs, `stages=award` -> buyer/supplier/value/contract-end intelligence |
| Public Contracts Scotland API | Not integrated | `api.publiccontractsscotland.gov.uk` timed out from the build environment. Above-threshold PCS notices still arrive via Find a Tender. |
| Sell2Wales API | Not integrated | `api.sell2wales.gov.wales/v1/Notices` returned HTTP 500 for every parameter form tried. Not scraped. Above-threshold notices still arrive via Find a Tender. |

Both live APIs are keyless, OGL-licensed. The collector pages with `links.next`, waits 1.5s between requests, retries 429/5xx with backoff, and only advances its watermark after a fully successful run.

## Daily use

| You want to | Do |
|---|---|
| See today's shortlist | Dashboard Overview, or `python -m fic report daily` |
| Run the AI evaluation now | In Claude Code in this folder: `/fic-daily` |
| Teach the system | Open an opportunity -> "Your verdict" -> pick the correct class + why. Or `python -m fic feedback <id> "Ineligible" --note "needs DSPT"` |
| Get a bid drafted | Opportunity -> "Request proposal draft", then `/fic-proposal`. Review in Proposals. You submit, never the system. |
| Record results | `python -m fic outcome <id> --submitted 1 --won 1 --contract-value 30000 --revenue 15000 --est-cost 9000 --actual-cost 11000` |
| Review learning | Dashboard Learning Centre: approve / reject / roll back. Or `python -m fic rules list --status pending_approval` |
| Award intelligence | `python -m fic awards` (top buyers, incumbent suppliers, contracts ending within 12 months) |
| Private prospects | `python -m fic prospect add --name ... --source-url <public evidence>` (research only; no outreach) |

## Agents

`.claude/agents/`: opportunity-scout (haiku, failures only), qualification-analyst, buyer-researcher, technical-architect, proposal-specialist, learning-auditor. The orchestrator is the `/fic-daily`, `/fic-weekly`, `/fic-proposal` commands plus `fic/orchestrator.py`.

How a task flows: `plan daily` picks the top N queued opportunities and creates only the tasks they need (buyer research once per buyer per 30 days; tech scoping only after a positive qualification; proposals only when you ask). A subagent runs `python -m fic task next --kind X`, gets a ~1k-token packet (truncated description, extracted requirement sentences, buyer award history, relevant learned rules and past mistakes), and submits JSON. `submit` runs deterministic QC: evidence quotes must exist in the source text, "Strong" can't have unverified requirements, eligibility can't be "confirmed", proposals can't claim credentials your profile doesn't have. Failed output is stored as an error and not applied.

## Token use

- Collection, dedupe, filtering, eligibility, scoring, rule testing and reports: zero Claude.
- Budget: `[budget]` in `config/fic.toml`, 30 AI tasks/day split 70% business / 20% audit / 10% reserve. `task next` refuses with `BUDGET_EXHAUSTED` (exit 3) when a category is spent; audit work never borrows reserve, so optional reviews pause first. A Pro plan has no published token allowance, so this budgets tasks; each task's estimated tokens are logged (Agent Centre) so you can tune the number after a week.
- Subagents fetch their own packets, so the orchestrator's context stays small. Recursion capped at depth 2.
- Plugins: **Ponytail** is installed in your account; it's a coding-style skill (minimal diffs, short replies) used while building this, not a runtime token saver. **Caveman** was not found in this environment, so nothing depends on it. FIC already forces terse JSON outputs, which is the bulk of what a terse-output plugin would give you.

## Scheduling

**Linux:** `crontab -e`, paste `scheduler/crontab.txt` (fix the path). Uses `CRON_TZ=Europe/London`.
**macOS:** cron ignores `CRON_TZ`; set the Mac to UK time or convert to launchd.
**Windows:** run `scheduler/windows_tasks.ps1` in PowerShell from the repo; set the PC timezone to London.

The machine must be on. The AI jobs call `claude -p "/fic-daily"` headless, which uses your Claude Code login (run `claude` once interactively first). Each job holds a lock file so runs can't overlap, and the budget makes it impossible to loop. Check it's really running: `python -m fic status` -> `last_runs` timestamps, and `logs/*.log`.

**Claude Code cloud scheduled tasks** are available on your account, but a cloud run starts from a fresh clone, so it would not see your local `data/fic.db`. Making that work means committing data to the repo, which I didn't set up. The local schedule is the working option today.

## Safety

- Never submits tenders, emails anyone, signs, buys or changes payment details. Proposals are drafts; approval just marks them ready.
- Dashboard binds to 127.0.0.1; POSTs require an `X-FIC` header (blocks cross-site form posts).
- `.claude/settings.json` allows agents only `python -m fic ...`; denies reading `.env`, `curl`, `rm`, `sqlite3`, `git push`.
- No credentials needed. `.env` is gitignored if you add one.
- Named contact details in notices are not stored.

## Layout

```
fic/            collector, rules engine, orchestrator, learning, memory, reports, dashboard server
fic/static/     dashboard (single page, no build step)
config/         fic.toml (weights, thresholds, budget), company_profile.toml (YOUR eligibility facts)
.claude/        agents, commands, permissions
knowledge/      global SOPs + generated active rules and rule history (git-versioned)
scheduler/      cron / Windows Task Scheduler scripts
tests/          19 tests, real API fixtures + synthetic edge cases
scripts/demo.py simulated learning-loop demo (separate DB)
```
