"""Relevant-memory retrieval. Agents get only what applies to them, capped in size."""
import json

from .db import rows


def for_agent(con, agent, opp_id=None, max_rules=12, max_errors=5):
    rules = rows(con, """SELECT rule_key, kind, spec, version FROM rules WHERE status='active' AND (agent=? OR agent='*')
                         ORDER BY kind='guidance' DESC, id DESC LIMIT ?""", (agent, max_rules))
    out = {"rules": [], "recent_mistakes": []}
    for r in rules:
        s = json.loads(r["spec"])
        text = s.get("text") or (f"Check for '{s.get('label', s.get('pattern'))}' -> {s.get('effect')}" if r["kind"] == "eligibility_gate"
                                 else f"{r['kind']}: {s.get('term') or s.get('pattern')}")
        out["rules"].append(f"[{r['rule_key']} v{r['version']}] {text}")
    errs = rows(con, """SELECT kind, detail, count(*) n FROM errors WHERE agent=? GROUP BY kind, substr(detail,1,80)
                        ORDER BY max(id) DESC LIMIT ?""", (agent, max_errors))
    out["recent_mistakes"] = [f"{e['kind']} x{e['n']}: {e['detail'][:140]}" for e in errs]
    if opp_id:
        fb = rows(con, "SELECT label_classification, note FROM feedback WHERE opp_id=? ORDER BY id DESC LIMIT 1", (opp_id,))
        if fb:
            out["human_feedback_on_this_opportunity"] = fb[0]
    return out
