# FIC build report — 22 Nov 2026

What exists, what was tested, and what doesn't exist yet. No feature below is claimed unless it ran.

## Implemented and tested

| Area | What | Evidence |
|---|---|---|
| Collection | Find a Tender + Contracts Finder OCDS connectors, tender + award stages, pagination, rate limiting, retry/backoff, watermarks | Live run 22 Nov: 82 tenders, 756 awards in ~18s. Tests: pagination/retry, failure holds watermark |
| Dedupe | Same process (ocid) across releases; cross-source duplicates (normalised title + buyer + deadline day) | Live: 9 duplicates caught. Tests on real FTS fixture + synthetic |
| Filtering | Word-boundary keyword + CPV relevance, exclusions; configurable | Live: 82 -> 3 relevant this week. Tests |
| Eligibility | 15 requirement detectors (Cyber Essentials/Plus, ISO 27001/9001, SC/BPSS, G-Cloud/DOS, turnover, insurance, years, references, on-site) compared to your declared profile; never "confirmed" | Tests incl. numeric turnover |
| Scoring | Your 6 weighted criteria, configurable, per-criterion confidence, ± uncertainty, eligibility/deadline/relevance gates, value-based effort/cost/profit ranges, ceiling-value flag | Tests: gate caps, weights, deadline |
| Orchestrator | Task queue with dedupe, routing (only needed agents), buyer-research cooldown, depth cap, stale-task recovery, daily budget 70/20/10, BUDGET_EXHAUSTED stop | Tests. Live: 2 real qualification tasks run end-to-end, QC passed |
| Agents | 6 subagent definitions + 3 orchestrator commands + restricted permissions | Packet/submit contract exercised live; full headless `/fic-daily` run not executed here (needs your login) |
| QC | Verbatim-evidence check, unverified "Strong", "confirmed" eligibility, missing sources, credential claims in proposals, unsupported Claude-savings claims | Tests: fabricated evidence and fake ISO claim both rejected and logged |
| Learning | Feedback -> misclassification errors -> deterministic diagnosis -> candidate rules -> test on labelled history (before/after accuracy, false accepts/rejects) -> pending approval -> approve / reject / rollback with version parents and event log; duplicate + conflict detection; low-risk observations auto-saved; Markdown export to `knowledge/rules/` | Deliberate-error test: DSPT tender misclassified -> detected -> gate proposed -> accuracy 0.75 -> 1.0 with 0 new false rejections -> held for approval -> applied on approve -> reverted on rollback. Harmful rule auto-rejected and cannot be approved |
| Memory | SQLite (WAL) + per-agent retrieval of active rules, recent mistakes, buyer history | Restart persistence test |
| Dashboard | Overview, Opportunities (search/filter/evidence drawer), Agent Centre, Learning Centre (approve/reject/rollback), Proposals, Analytics (real data only) | API tests; Playwright screenshots desktop + 390px, no JS errors, no horizontal scroll |
| Reports | Daily shortlist Markdown with every field you listed; weekly learning report | Generated from live data |
| Scheduling | cron (Europe/London), Windows Task Scheduler script, lock files | Files only. **Not installed or verified running** — needs your machine |
| Business tracking | Outcomes (submitted/won/revenue/est vs actual cost), proposals approval, prospects with PECR fields, award intelligence (top buyers, incumbents, contracts ending in 12 months) | CLI smoke-tested |

## Not done / blocked

- **Sell2Wales and Public Contracts Scotland direct APIs**: blocked (500 errors / timeouts from the build environment). Above-threshold notices from both come through Find a Tender anyway; below-threshold Welsh/Scottish ones are missed.
- **Scheduled jobs are not active.** Nothing has been installed on your machine. Follow README "Scheduling", then check `python -m fic status`.
- **Cloud scheduled tasks**: available on your account but not configured (fresh clone per run = no access to the local DB).
- **Full headless `/fic-daily` run** not executed in this build session; the task contract it relies on was exercised manually with two live tasks.
- **Tender documents** behind buyer portals (MultiQuote, PCS-Tender, Atamis…) are not fetched. Eligibility checks only see the notice text, which is why "no blockers found" never means eligible.
- **Win probability**: deliberately not modelled. Competition score is a labelled proxy until you have real outcomes.
- **Private-sector research** is a manual prospects table + the buyer-researcher agent; no automated discovery or outreach.

## Next steps that would add the most value

1. Fill in `config/company_profile.toml` (biggest accuracy gain, zero cost).
2. Install the schedule; let it collect for a week.
3. Label 10–20 opportunities in the dashboard. Learning has nothing to learn from until you do; tests flag rules as "low evidence" under 5 labelled cases.
4. After a week, tune `[relevance]` terms and `[budget] daily_ai_tasks` using the Agent Centre token estimates.
5. Retry the Sell2Wales/PCS APIs from your machine; if they work, add connectors following `fic/connectors.py`.
