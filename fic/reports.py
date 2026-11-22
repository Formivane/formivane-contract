"""Daily shortlist / weekly learning reports + analytics (real data only)."""
import json
from pathlib import Path

from . import learning, orchestrator, rules_engine
from .config import ROOT
from .db import rows


def opportunity_card(o):
    sj = json.loads(o["score_json"] or "{}")
    econ, s = sj.get("economics") or {}, sj.get("score") or {}
    el = json.loads(o["eligibility_json"] or "{}")
    cls = o["ai_classification"] or o["classification"]
    risks = [r["req"] + (" (not met)" if r["met"] == "no" else " (unverified)") for r in el.get("requirements", []) if r["met"] != "yes"]
    if econ.get("value_is_ceiling"):
        risks.append("advertised value is a ceiling/framework estimate")
    pr = econ.get("profit_range_gbp")
    return {
        "id": o["id"], "title": o["title"], "buyer": o["buyer"], "source": o["url"],
        "contract_value": o["value_amount"], "deadline": o["deadline"],
        "required_work": f"{(econ.get('category') or '').replace('_', ' ')}: {(o['description'] or '')[:160]}",
        "eligibility_status": o["eligibility_status"], "technical_difficulty": econ.get("difficulty"),
        "est_time_days": econ.get("effort_days"), "est_costs_gbp": econ.get("est_cost_gbp"),
        "potential_profit_gbp": pr, "key_risks": risks[:5], "score": s.get("score"), "uncertainty": s.get("uncertainty"),
        "classification": cls, "classified_by": "AI analyst (QC passed)" if o["ai_classification"] else "rules engine",
        "next_action": rules_engine.next_action(cls, el),
    }


def _gbp(v):
    return "n/a" if v is None else f"£{v:,.0f}"


def daily(con, cfg, write=True):
    opps = rows(con, """SELECT * FROM opportunities WHERE status IN ('queued','analysed','shortlisted') AND duplicate_of IS NULL
                        AND coalesce(ai_classification, classification) IN ('Strong candidate','Possible candidate','Requires investigation')
                        ORDER BY score DESC LIMIT 15""")
    cards = [opportunity_card(o) for o in opps]
    runs = rows(con, "SELECT source, stage, status, fetched, new, errors, finished FROM collection_runs ORDER BY id DESC LIMIT 4")
    b = orchestrator.budget_status(con, cfg)
    md = [f"# FIC daily summary {b['day']}", "",
          "Scores are explainable estimates, not win probabilities. Profit = advertised value minus estimated cost.", "",
          "## Collection", ""] + [f"- {r['source']}/{r['stage']}: {r['status']}, fetched {r['fetched']}, new {r['new']}" + (f", error: {r['errors']}" if r["errors"] else "") for r in runs]
    md += ["", f"## Budget: used {b['used']} of {b['caps']}", "", "## Shortlist", ""]
    if not cards:
        md.append("_No qualifying opportunities right now._")
    for c in cards:
        pr = c["potential_profit_gbp"]
        md += [f"### {c['title']}", f"- Buyer: {c['buyer']} | Source: {c['source']}",
               f"- Advertised value: {_gbp(c['contract_value'])} | Deadline: {c['deadline']}",
               f"- Work: {c['required_work']}", f"- Eligibility: {c['eligibility_status']} | Difficulty: {c['technical_difficulty']}",
               f"- Est. time: {c['est_time_days']} days | Est. cost: {[_gbp(x) for x in c['est_costs_gbp'] or []]}",
               f"- Potential profit: {(_gbp(pr[0]) + ' to ' + _gbp(pr[1])) if pr else 'unknown (no value published)'}",
               f"- Risks: {'; '.join(c['key_risks']) or 'none detected (still read the pack)'}",
               f"- Score: {c['score']} ± {c['uncertainty']} -> **{c['classification']}** ({c['classified_by']})",
               f"- Next: {c['next_action']}", ""]
    text = "\n".join(md)
    if write:
        p = Path(ROOT) / "reports" / f"daily-{b['day']}.md"
        p.parent.mkdir(exist_ok=True)
        p.write_text(text)
    return text, cards


def weekly(con, cfg, result, write=True):
    m = result["metrics_after"]
    md = [f"# FIC weekly learning report {orchestrator.today_london()}", "",
          f"- Labelled cases: {m['labelled_cases']}",
          f"- Rules-engine bid-decision accuracy: {m['rules_engine']}",
          f"- AI qualifier accuracy: {m['ai_qualifier_accuracy']} over {m['ai_labelled']} labelled",
          f"- Candidate rules proposed this run: {result['proposed']}", "", "## Tests", ""]
    for t in result["tested"]:
        md.append(f"- rule {t.get('rule_id')}: {t.get('status')} — {json.dumps(t.get('result', {}).get('before'))} -> {json.dumps(t.get('result', {}).get('after'))}")
    md += ["", "## Awaiting your approval", ""] + [f"- #{r['id']} {r['rule_key']}: {r['rationale']}" for r in result["pending_approval"]]
    md += ["", "## Errors by agent", ""] + [f"- {e['agent']} / {e['kind']}: {e['n']}" for e in m["errors"]]
    text = "\n".join(md)
    if write:
        p = Path(ROOT) / "reports" / f"weekly-{orchestrator.today_london()}.md"
        p.parent.mkdir(exist_ok=True)
        p.write_text(text)
    return text


def analytics(con, cfg):
    o = rows(con, "SELECT * FROM outcomes")
    return {
        "discovered_by_day": rows(con, """SELECT substr(first_seen,1,10) d, count(*) n, sum(status!='filtered_out' AND status!='duplicate') relevant
                                          FROM opportunities GROUP BY d ORDER BY d"""),
        "classification_mix": rows(con, "SELECT coalesce(ai_classification,classification) c, count(*) n FROM opportunities WHERE duplicate_of IS NULL GROUP BY c"),
        "proposals": rows(con, "SELECT status, count(*) n FROM proposals GROUP BY status"),
        "outcomes": {"submitted": sum(x["submitted"] or 0 for x in o), "won": sum(x["won"] or 0 for x in o),
                     "revenue_received": sum(x["revenue_received"] or 0 for x in o),
                     "est_cost": sum(x["est_cost"] or 0 for x in o), "actual_cost": sum(x["actual_cost"] or 0 for x in o),
                     "records": o},
        "learning": learning.metrics(con, cfg),
        "error_rate_by_agent": rows(con, """SELECT agent, count(*) runs, sum(1-qc_passed) qc_fail,
                                            round(1.0*sum(1-qc_passed)/count(*),3) rate FROM agent_runs GROUP BY agent"""),
        "rules_over_time": rows(con, "SELECT substr(created,1,10) d, event, count(*) n FROM rule_events GROUP BY d, event ORDER BY d"),
    }
