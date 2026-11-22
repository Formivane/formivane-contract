"""Central orchestrator: task queue, routing, budgets, compact task packets, QC on agent outputs.

Claude subagents never read the DB directly. They get a compact packet from `task next`
and hand back JSON via `task submit`. All checks here are deterministic.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import pipeline, rules_engine
from .db import log, now, one, rows

AGENTS = {
    "opportunity-scout": "Monitors official sources, dedupes, categorises (script-driven).",
    "qualification-analyst": "Decides whether Formivane can realistically bid; checks eligibility gates.",
    "buyer-researcher": "Investigates buyers' procurement history with cited sources.",
    "technical-architect": "Scopes tech, effort, risks, and where human experts are needed.",
    "proposal-specialist": "Drafts accurate tender responses for human review.",
    "learning-auditor": "Reviews outputs vs evidence, diagnoses errors, proposes testable rules.",
}
KIND_AGENT = {"qualify": "qualification-analyst", "buyer_research": "buyer-researcher", "tech_scope": "technical-architect",
              "proposal": "proposal-specialist", "audit": "learning-auditor"}
CREDENTIAL_CLAIMS = [  # phrases a proposal must not assert unless the profile backs them
    (r"cyber essentials plus", "accreditations.cyber_essentials_plus"), (r"cyber essentials(?! plus)", "accreditations.cyber_essentials"),
    (r"iso\s*27001", "accreditations.iso_27001"), (r"iso\s*9001", "accreditations.iso_9001"),
    (r"g-?cloud", "frameworks.g_cloud"), (r"\bsc[- ]cleared|security cleared", "security.sc_clearance_staff"),
]


# ---------------------------------------------------------------- budget
def today_london():
    return datetime.now(ZoneInfo("Europe/London")).date().isoformat()


def budget_status(con, cfg, day=None):
    b = cfg["budget"]
    day = day or today_london()
    caps = {"business": round(b["daily_ai_tasks"] * b["business_pct"] / 100),
            "audit": round(b["daily_ai_tasks"] * b["audit_pct"] / 100),
            "reserve": round(b["daily_ai_tasks"] * b["reserve_pct"] / 100)}
    used = {r["category"]: r["n"] for r in rows(con, "SELECT category, sum(units) n FROM budget_ledger WHERE day=? GROUP BY category", (day,))}
    return {"day": day, "caps": caps, "used": {k: used.get(k, 0) for k in caps},
            "remaining": {k: caps[k] - used.get(k, 0) for k in caps}}


def _charge(con, cfg, category, task_id):
    """Spend one unit from category; overflow from business into reserve. Audit never uses reserve (optional work pauses)."""
    st = budget_status(con, cfg)
    cat = category if st["remaining"][category] > 0 else ("reserve" if category == "business" and st["remaining"]["reserve"] > 0 else None)
    if cat:
        con.execute("INSERT INTO budget_ledger(day,category,task_id,created) VALUES(?,?,?,?)", (st["day"], cat, task_id, now()))
    return cat


# ---------------------------------------------------------------- queue
def enqueue(con, kind, opp_id=None, payload=None, priority=0, depth=0, category=None, dedupe=None):
    category = category or ("audit" if kind == "audit" else "business")
    key = dedupe or f"{kind}:{opp_id}"
    try:
        return con.execute("""INSERT INTO tasks(kind,agent,opp_id,category,priority,depth,payload,dedupe_key,created)
                              VALUES(?,?,?,?,?,?,?,?,?)""",
                           (kind, KIND_AGENT[kind], opp_id, category, priority, depth, json.dumps(payload or {}), key, now())).lastrowid
    except Exception as e:  # unique dedupe index -> identical task already pending
        if "UNIQUE" in str(e):
            return None
        raise


def recover_stale(con, cfg):
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=cfg["budget"]["stale_task_minutes"])).replace(microsecond=0).isoformat()
    stale = rows(con, "SELECT id, attempts FROM tasks WHERE status='in_progress' AND started < ?", (cutoff,))
    for t in stale:
        st = "queued" if t["attempts"] < cfg["budget"]["max_attempts"] else "failed"
        con.execute("UPDATE tasks SET status=?, error=coalesce(error,'')||'stale; ' WHERE id=?", (st, t["id"]))
    con.commit()
    return len(stale)


def plan_daily(con, cfg):
    """Pick the strongest queued opportunities and create only the tasks they need."""
    oc = cfg["orchestrator"]
    pipeline.expire_old(con)
    recover_stale(con, cfg)
    opps = rows(con, """SELECT id, buyer, score FROM opportunities WHERE status='queued' AND duplicate_of IS NULL
                        AND classification IN ('Strong candidate','Possible candidate','Requires investigation')
                        ORDER BY score DESC LIMIT ?""", (oc["daily_shortlist_size"],))
    made = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=oc["buyer_research_cooldown_days"])).isoformat()
    for o in opps:
        if enqueue(con, "qualify", o["id"], priority=o["score"]):
            made.append(("qualify", o["id"]))
        if o["score"] >= oc["buyer_research_min_score"] and o["buyer"]:
            recent = one(con, """SELECT 1 FROM agent_runs ar JOIN opportunities op ON op.id=ar.opp_id
                                 WHERE ar.agent='buyer-researcher' AND op.buyer=? AND ar.created>?""", (o["buyer"], cutoff))
            if not recent and enqueue(con, "buyer_research", o["id"], priority=o["score"] - 1, dedupe=f"buyer_research:{o['buyer']}"):
                made.append(("buyer_research", o["id"]))
    con.commit()
    log(con, "orchestrator", "plan_daily", {"tasks": len(made)})
    con.commit()
    return made


def plan_weekly_audit(con, cfg):
    """One batched audit task covering unreviewed runs (QC failures and feedback mismatches first)."""
    runs = rows(con, """SELECT ar.id FROM agent_runs ar LEFT JOIN feedback f ON f.opp_id=ar.opp_id
                        WHERE ar.reviewed=0 AND ar.agent!='learning-auditor'
                        GROUP BY ar.id ORDER BY (ar.qc_passed=0) DESC, (count(f.id)>0) DESC, ar.id DESC LIMIT ?""",
                (cfg["learning"]["weekly_audit_batch"],))
    if not runs:
        return None
    tid = enqueue(con, "audit", None, {"agent_run_ids": [r["id"] for r in runs]}, dedupe=f"audit:{today_london()}")
    con.commit()
    return tid


def _short(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n] + " …[truncated]"


def packet(con, cfg, task):
    """Smallest useful context for the agent."""
    from . import memory
    oc = cfg["orchestrator"]
    p = {"task_id": task["id"], "kind": task["kind"], "agent": task["agent"], "payload": json.loads(task["payload"] or "{}")}
    if task["opp_id"]:
        o = one(con, "SELECT * FROM opportunities WHERE id=?", (task["opp_id"],))
        sj = json.loads(o["score_json"] or "{}")
        p["opportunity"] = {
            "opp_id": o["id"], "title": o["title"], "buyer": o["buyer"], "url": o["url"], "source": o["source"],
            "value_gbp": o["value_amount"], "deadline": o["deadline"], "cpv": o["cpv"], "method": o["procurement_method"],
            "sme_suitable": o["sme_suitable"], "category": o["category"],
            "description": _short(o["description"], oc["packet_description_chars"]),
            "requirements_extract": _short(o["requirements_text"], oc["packet_requirements_chars"]),
            "pre_check": {"classification": o["classification"], "score": o["score"],
                          "eligibility": json.loads(o["eligibility_json"] or "{}"),
                          "economics": sj.get("economics")},
        }
        p["buyer_history"] = rows(con, """SELECT title, supplier, value, award_date, contract_end, url FROM awards
                                          WHERE buyer=? ORDER BY award_date DESC LIMIT 5""", (o["buyer"],))
        prior = rows(con, "SELECT agent, output FROM agent_runs WHERE opp_id=? AND qc_passed=1 ORDER BY id", (o["id"],))
        p["prior_outputs"] = {r["agent"]: json.loads(r["output"]) for r in prior}
    if task["kind"] == "audit":
        ids = p["payload"].get("agent_run_ids", [])
        p["runs"] = [dict(r, output=json.loads(r["output"]), qc_issues=json.loads(r["qc_issues"] or "[]"),
                          feedback=rows(con, "SELECT label_classification, note FROM feedback WHERE opp_id=?", (r["opp_id"],)),
                          evidence_url=(one(con, "SELECT url FROM opportunities WHERE id=?", (r["opp_id"],)) or {}).get("url"))
                     for r in rows(con, f"SELECT id, agent, opp_id, output, qc_issues FROM agent_runs WHERE id IN ({','.join('?' * len(ids))})", ids)]
    if task["kind"] == "proposal":
        p["company_profile"] = cfg["profile"]
    p["memory"] = memory.for_agent(con, task["agent"], task["opp_id"])
    p["output_schema"] = SCHEMAS[task["kind"]]
    return p


def next_task(con, cfg, agent=None, kind=None):
    recover_stale(con, cfg)
    q = "SELECT * FROM tasks WHERE status='queued'"
    args = []
    if agent:
        q += " AND agent=?"; args.append(agent)
    if kind:
        q += " AND kind=?"; args.append(kind)
    t = one(con, q + " ORDER BY priority DESC, id LIMIT 1", args)
    if not t:
        return {"status": "EMPTY"}
    cat = _charge(con, cfg, t["category"], t["id"])
    if not cat:
        con.commit()
        return {"status": "BUDGET_EXHAUSTED", "category": t["category"], "budget": budget_status(con, cfg)}
    con.execute("UPDATE tasks SET status='in_progress', started=?, attempts=attempts+1 WHERE id=?", (now(), t["id"]))
    con.commit()
    return {"status": "OK", "packet": packet(con, cfg, t)}


# ---------------------------------------------------------------- QC
SCHEMAS = {
    "qualify": {"opp_id": "int", "classification": rules_engine.CLASSES,
                "eligibility": {"status": ["ineligible", "requires_verification", "likely_eligible_unverified", "no_blockers_found"],
                                "requirements": [{"req": "str", "met": "yes|no|unknown", "evidence": "verbatim quote from packet text"}]},
                "feasibility_1to5": "int", "effort_days": "[low, high]", "risks": ["str"], "reasoning": "<=600 chars", "sources": ["url"]},
    "buyer_research": {"buyer": "str", "findings": [{"claim": "str", "source_url": "url"}], "procurement_pattern": "str",
                       "upcoming_signals": [{"signal": "str", "evidence_url": "url"}]},
    "tech_scope": {"opp_id": "int", "stack": ["str"], "effort_days": "[low, high]", "maintenance_days_per_month": "number",
                   "claude_assist_saving_pct": "[low, high]", "saving_evidence": "str", "human_specialists_needed": ["str"],
                   "security": ["str"], "dependencies": ["str"], "risks": ["str"]},
    "proposal": {"opp_id": "int", "title": "str", "draft_md": "markdown", "missing_info": ["str"], "claims_needing_verification": ["str"]},
    "audit": {"reviews": [{"agent_run_id": "int", "verdict": "ok|error", "error_kind": "factual|missing_requirement|weak_reasoning|wasted_effort|unsupported_claim",
                           "detail": "str"}],
              "proposed_rules": [{"kind": "eligibility_gate|relevance_include|relevance_exclude|score_adjust|observation|guidance",
                                  "agent": "str", "spec": "object (see README)", "rationale": "str", "evidence_run_ids": ["int"]}]},
}


def _norm(s):
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def _urlish(u):
    return isinstance(u, str) and u.startswith(("http://", "https://"))


def qc(con, cfg, task, out):
    issues = []
    kind = task["kind"]
    opp = one(con, "SELECT * FROM opportunities WHERE id=?", (task["opp_id"],)) if task["opp_id"] else None
    src_text = _norm(rules_engine.text_of(opp)) if opp else ""
    if kind == "qualify":
        if out.get("classification") not in rules_engine.CLASSES:
            issues.append("invalid classification")
        el = out.get("eligibility") or {}
        if el.get("status") not in SCHEMAS["qualify"]["eligibility"]["status"]:
            issues.append("invalid or 'confirmed' eligibility status (eligibility is never confirmed by the system)")
        reqs = el.get("requirements") or []
        if out.get("classification") == "Strong candidate" and any(r.get("met") != "yes" for r in reqs):
            issues.append("Strong candidate with unverified/unmet requirements")
        for r in reqs:
            ev = _norm(r.get("evidence"))
            if ev and ev not in src_text:
                issues.append(f"evidence not found in source text: {r.get('evidence', '')[:80]}")
        pre = json.loads(opp["eligibility_json"] or "{}") if opp else {}
        if pre.get("status") == "ineligible" and out.get("classification") in rules_engine.POSITIVE:
            issues.append("contradicts deterministic eligibility gate without explanation")
        if not any(_urlish(u) for u in out.get("sources") or []):
            issues.append("no source URL")
        if len(out.get("reasoning") or "") > 900:
            issues.append("reasoning too long (token waste)")
    elif kind == "buyer_research":
        f = out.get("findings") or []
        if not f:
            issues.append("no findings")
        issues += [f"finding without source: {x.get('claim', '')[:60]}" for x in f if not _urlish(x.get("source_url"))]
        issues += [f"signal without evidence: {x.get('signal', '')[:60]}" for x in out.get("upcoming_signals") or [] if not _urlish(x.get("evidence_url"))]
    elif kind == "tech_scope":
        e = out.get("effort_days") or []
        if len(e) != 2 or e[0] > e[1]:
            issues.append("effort_days must be [low, high]")
        s = out.get("claude_assist_saving_pct") or [0, 0]
        if (s and max(s) > 0 and not out.get("saving_evidence")) or (s and max(s) > 60):
            issues.append("Claude-assist saving claim unsupported or implausible (>60%)")
    elif kind == "proposal":
        draft = (out.get("draft_md") or "").lower()
        for pat, path in CREDENTIAL_CLAIMS:
            if re.search(pat, draft) and rules_engine._profile(cfg["profile"], path) is not True:
                issues.append(f"draft mentions credential not held per profile: /{pat}/ (must be removed or marked as gap)")
        if re.search(r"\b(we have|we've) (delivered|worked with|supported)\b", draft) and not out.get("claims_needing_verification"):
            issues.append("past-experience claims present but none flagged for verification")
    elif kind == "audit":
        for pr in out.get("proposed_rules") or []:
            if not pr.get("evidence_run_ids"):
                issues.append("proposed rule without evidence")
    return issues


def submit(con, cfg, task_id, out):
    from . import learning
    t = one(con, "SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise SystemExit(f"task {task_id} not found")
    if t["status"] != "in_progress":
        raise SystemExit(f"task {task_id} is {t['status']}, not in_progress")
    issues = qc(con, cfg, t, out)
    est = len(json.dumps(packet(con, cfg, t))) // 4 + len(json.dumps(out)) // 4
    decision = out.get("classification") or out.get("verdict") or t["kind"]
    rid = con.execute("""INSERT INTO agent_runs(task_id,agent,opp_id,output,decision,qc_passed,qc_issues,est_tokens,created)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
                      (task_id, t["agent"], t["opp_id"], json.dumps(out), decision, int(not issues), json.dumps(issues), est, now())).lastrowid
    for i in issues:
        con.execute("INSERT INTO errors(agent,agent_run_id,opp_id,kind,detail,detected_by,created) VALUES(?,?,?,?,?,?,?)",
                    (t["agent"], rid, t["opp_id"], "qc_violation", i, "orchestrator.qc", now()))
    con.execute("UPDATE tasks SET status='done', finished=? WHERE id=?", (now(), task_id))
    follow = []
    if t["kind"] == "qualify" and not issues:
        con.execute("UPDATE opportunities SET ai_classification=?, status='analysed' WHERE id=?", (out["classification"], t["opp_id"]))
        if out["classification"] in rules_engine.POSITIVE and t["depth"] < cfg["budget"]["max_task_depth"]:
            if enqueue(con, "tech_scope", t["opp_id"], priority=50, depth=t["depth"] + 1):
                follow.append("tech_scope")
    if t["kind"] == "proposal" and not issues:
        con.execute("""INSERT INTO proposals(opp_id,agent_run_id,title,body_md,missing_info,verify,status,created,updated)
                       VALUES(?,?,?,?,?,?,'draft',?,?)""",
                    (t["opp_id"], rid, out.get("title"), out.get("draft_md"), json.dumps(out.get("missing_info", [])),
                     json.dumps(out.get("claims_needing_verification", [])), now(), now()))
    if t["kind"] == "audit":
        learning.ingest_audit(con, cfg, rid, out)
    log(con, t["agent"], f"submit {t['kind']}", {"task": task_id, "opp": t["opp_id"], "qc_issues": len(issues), "follow_up": follow})
    con.commit()
    return {"agent_run_id": rid, "qc_passed": not issues, "qc_issues": issues, "follow_up": follow}


def fail(con, task_id, error):
    con.execute("UPDATE tasks SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'queued' END, error=? WHERE id=?", (error, task_id))
    con.commit()
