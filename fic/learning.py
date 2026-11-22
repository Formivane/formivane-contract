"""Self-learning engine: record -> review -> diagnose -> propose -> test -> validate -> remember -> rollback.

Rules are executable specs, so testing re-runs the deterministic classifier over every
human-labelled historical case with and without the candidate. No Claude tokens needed.
Nothing that changes decisions goes live without passing tests AND (for high-risk kinds) human approval.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import pipeline, rules_engine
from .config import ROOT
from .db import log, now, one, rows

HIGH_RISK = {"eligibility_gate", "relevance_exclude", "relevance_include", "score_adjust", "guidance"}
TESTABLE = {"eligibility_gate", "relevance_exclude", "relevance_include", "score_adjust"}
BUCKET = {"Strong candidate": "pursue", "Possible candidate": "pursue", "Requires investigation": "check",
          "Unlikely to be suitable": "reject", "Ineligible": "reject"}

# Known UK requirement phrases. Too noisy to gate on by default (often "desirable"), so they only
# become rules when a labelled failure shows they matter and history testing shows no false rejections.
DIAG_REQUIREMENTS = [
    (r"data security and protection toolkit|\bdspt\b|dsp toolkit", "NHS DSPT", "accreditations.dspt"),
    (r"\bpsn\b (compliance|connection|code of connection)|public services network", "PSN compliance", "accreditations.psn"),
    (r"iso\s*14001", "ISO 14001", "accreditations.iso_14001"),
    (r"iso\s*22301", "ISO 22301", "accreditations.iso_22301"),
    (r"iso\s*20000", "ISO 20000", "accreditations.iso_20000"),
    (r"pci[- ]dss", "PCI DSS", "accreditations.pci_dss"),
    (r"crown commercial service|\bccs\b framework|\brm\d{4}\b", "CCS framework membership", "frameworks.ccs_other"),
    (r"constructionline", "Constructionline", "accreditations.constructionline"),
    (r"\bnpcc\b|police vetting|nppv", "Police vetting (NPPV)", "security.nppv_staff"),
    (r"welsh language (standards|scheme)", "Welsh language delivery", "delivery.welsh_language"),
    (r"\bitil\b", "ITIL certification", "accreditations.itil"),
]
MUST_HOLD = re.compile(r"\b(?:must|shall|are required to|is required to)\s+(?:hold|have|possess|be certified to|be accredited to|be registered with)\s+(?:a |an |the |current |valid )*([A-Z][\w+/&\- ]{2,50})")
DIAG_RELEVANCE = ["intranet", "sharepoint", "microsoft 365", "low-code", "no-code", "data migration", "digital accessibility",
                  "web accessibility", "online booking", "case management system", "customer portal", "digital platform",
                  "content design", "website redesign", "web app"]


def _event(con, rule_id, event, detail="", actor="system"):
    con.execute("INSERT INTO rule_events(rule_id,event,detail,actor,created) VALUES(?,?,?,?,?)",
                (rule_id, event, detail if isinstance(detail, str) else json.dumps(detail), actor, now()))


def _rule(con, rule_id):
    r = one(con, "SELECT * FROM rules WHERE id=?", (rule_id,))
    if not r:
        raise SystemExit(f"rule {rule_id} not found")
    r["spec"] = json.loads(r["spec"])
    return r


def _key_of(kind, spec):
    s = spec.get("label") or spec.get("term") or spec.get("pattern") or spec.get("text", "")[:40]
    return f"{kind}:{re.sub(r'[^a-z0-9]+', '_', s.lower()).strip('_')[:50]}"


# ---------------------------------------------------------------- RECORD / REVIEW
def record_feedback(con, cfg, opp_id, label, note="", source="human"):
    if label not in rules_engine.CLASSES:
        raise SystemExit(f"label must be one of {rules_engine.CLASSES}")
    o = one(con, "SELECT * FROM opportunities WHERE id=?", (opp_id,))
    if not o:
        raise SystemExit(f"opportunity {opp_id} not found")
    run = one(con, "SELECT id FROM agent_runs WHERE opp_id=? AND agent='qualification-analyst' ORDER BY id DESC LIMIT 1", (opp_id,))
    fid = con.execute("INSERT INTO feedback(opp_id,agent_run_id,label_classification,note,source,created) VALUES(?,?,?,?,?,?)",
                      (opp_id, run["id"] if run else None, label, note, source, now())).lastrowid
    for who, pred in (("rules_engine", o["classification"]), ("qualification-analyst", o["ai_classification"])):
        if pred and BUCKET[pred] != BUCKET[label]:
            con.execute("INSERT INTO errors(agent,agent_run_id,opp_id,kind,detail,detected_by,created) VALUES(?,?,?,?,?,?,?)",
                        (who, run["id"] if run and who != "rules_engine" else None, opp_id, "misclassification",
                         json.dumps({"predicted": pred, "label": label, "note": note}), "feedback", now()))
    log(con, "human", "feedback", {"opp": opp_id, "label": label})
    con.commit()
    return fid


def ingest_audit(con, cfg, run_id, out):
    for rv in out.get("reviews") or []:
        con.execute("UPDATE agent_runs SET reviewed=1 WHERE id=?", (rv.get("agent_run_id"),))
        if rv.get("verdict") == "error":
            r = one(con, "SELECT agent, opp_id FROM agent_runs WHERE id=?", (rv.get("agent_run_id"),)) or {}
            con.execute("INSERT INTO errors(agent,agent_run_id,opp_id,kind,detail,detected_by,created) VALUES(?,?,?,?,?,?,?)",
                        (r.get("agent"), rv.get("agent_run_id"), r.get("opp_id"), rv.get("error_kind", "unspecified"),
                         rv.get("detail", ""), "learning-auditor", now()))
    for pr in out.get("proposed_rules") or []:
        res = propose(con, pr["kind"], pr.get("agent", "*"), pr["spec"], pr.get("rationale", ""),
                      {"run_ids": pr.get("evidence_run_ids", [])}, "learning-auditor")
        if res.get("rule_id"):
            test_rule(con, cfg, res["rule_id"])


# ---------------------------------------------------------------- DIAGNOSE
def labelled_cases(con):
    return rows(con, """SELECT o.*, f.label_classification label FROM feedback f JOIN opportunities o ON o.id=f.opp_id
                        WHERE f.id IN (SELECT max(id) FROM feedback GROUP BY opp_id)""")


def diagnose(con, cfg):
    """Turn unexplained misclassifications into candidate rules (deterministic, zero tokens)."""
    active = pipeline.active_rules(con)
    gated = {r["spec"]["pattern"] for r in active if r["kind"] == "eligibility_gate"}
    errs = rows(con, "SELECT * FROM errors WHERE kind='misclassification' AND rule_id IS NULL")
    proposed = []
    for e in errs:
        d = json.loads(e["detail"])
        o = one(con, "SELECT * FROM opportunities WHERE id=?", (e["opp_id"],))
        text = rules_engine.text_of(o)
        cands = []
        if BUCKET[d["label"]] == "reject" and BUCKET[d["predicted"]] != "reject":
            # false acceptance: look for a requirement we don't gate on
            for pat, label, path in DIAG_REQUIREMENTS:
                if pat not in gated and re.search(pat, text, re.I):
                    cands.append(("eligibility_gate", {"pattern": pat, "label": label, "profile_key": path, "effect": "ineligible"},
                                  f"Missed mandatory requirement '{label}' on opp {o['id']} (labelled {d['label']})."))
            if not cands:
                for m in MUST_HOLD.finditer(text):
                    phrase = m.group(1).strip()
                    cands.append(("eligibility_gate", {"pattern": re.escape(phrase.lower()), "label": phrase, "effect": "requires_verification"},
                                  f"Opp {o['id']}: text states suppliers must hold '{phrase}'."))
                    break
        elif BUCKET[d["label"]] != "reject" and BUCKET[d["predicted"]] == "reject":
            low = text.lower()
            for term in DIAG_RELEVANCE:
                if term in low and term not in cfg["relevance"]["include_terms"]:
                    cands.append(("relevance_include", {"term": term}, f"Opp {o['id']} labelled {d['label']} but filtered; contains '{term}'."))
        for kind, spec, why in cands:
            res = propose(con, kind, "qualification-analyst", spec, why, {"error_ids": [e["id"]], "opp_ids": [o["id"]]}, "learning-diagnoser")
            if res.get("rule_id"):
                con.execute("UPDATE errors SET rule_id=? WHERE id=?", (res["rule_id"], e["id"]))
                proposed.append(res["rule_id"])
            elif res.get("existing_id"):
                con.execute("UPDATE errors SET rule_id=? WHERE id=?", (res["existing_id"], e["id"]))
    con.commit()
    return proposed


# ---------------------------------------------------------------- PROPOSE
def propose(con, kind, agent, spec, rationale, evidence, created_by, rule_key=None):
    if kind not in HIGH_RISK | {"observation"}:
        return {"error": f"unknown kind {kind}"}
    if kind in TESTABLE:
        field = "term" if kind.startswith("relevance") else "pattern"
        if not spec.get(field):
            return {"error": f"spec.{field} required"}
        if field == "pattern":
            try:
                re.compile(spec["pattern"])
            except re.error as e:
                return {"error": f"invalid regex: {e}"}
    if kind == "score_adjust" and (spec.get("criterion") not in ("profitability", "feasibility", "eligibility", "competition", "time", "strategic")
                                   or abs(float(spec.get("delta", 0))) > 0.5):
        return {"error": "score_adjust needs a valid criterion and |delta| <= 0.5"}
    key = rule_key or _key_of(kind, spec)
    norm = json.dumps(spec, sort_keys=True).lower()
    live = rows(con, "SELECT * FROM rules WHERE status IN ('candidate','pending_approval','active')")
    for r in live:
        if r["kind"] == kind and json.dumps(json.loads(r["spec"]), sort_keys=True).lower() == norm:
            return {"duplicate": True, "existing_id": r["id"]}
    s_val = (spec.get("pattern") or spec.get("term") or "").lower()
    opposite = {"relevance_include": "relevance_exclude", "relevance_exclude": "relevance_include"}
    for r in live:
        rs = json.loads(r["spec"])
        if s_val and (rs.get("pattern") or rs.get("term") or "").lower() == s_val and r["kind"] == opposite.get(kind):
            return {"conflict": True, "existing_id": r["id"], "detail": f"conflicts with {r['kind']} rule {r['id']}"}
    prev = one(con, "SELECT id, version FROM rules WHERE rule_key=? ORDER BY version DESC LIMIT 1", (key,))
    risk = "high" if kind in HIGH_RISK else "low"
    rid = con.execute("""INSERT INTO rules(rule_key,version,parent_id,agent,kind,spec,rationale,risk,status,created_by,created,evidence)
                         VALUES(?,?,?,?,?,?,?,?,'candidate',?,?,?)""",
                      (key, (prev["version"] + 1) if prev else 1, prev["id"] if prev else None, agent, kind, json.dumps(spec),
                       rationale, risk, created_by, now(), json.dumps(evidence))).lastrowid
    _event(con, rid, "proposed", rationale, created_by)
    con.commit()
    return {"rule_id": rid, "rule_key": key}


# ---------------------------------------------------------------- TEST / VALIDATE
def evaluate(cases, cfg, rules):
    res = {"n": len(cases), "correct": 0, "exact": 0, "false_accept": 0, "false_reject": 0, "per_case": {}}
    for c in cases:
        today = datetime.fromisoformat((c["published"] or c["first_seen"]).replace("Z", "+00:00"))
        if today.tzinfo is None:
            today = today.replace(tzinfo=timezone.utc)
        pred = rules_engine.classify(c, cfg, cfg["profile"], rules, today=today)["classification"]
        pb, lb = BUCKET[pred], BUCKET[c["label"]]
        res["correct"] += pb == lb
        res["exact"] += pred == c["label"]
        res["false_accept"] += lb == "reject" and pb == "pursue"
        res["false_reject"] += lb != "reject" and pb == "reject"
        res["per_case"][c["id"]] = pred
    res["accuracy"] = round(res["correct"] / res["n"], 3) if res["n"] else None
    return res


def test_rule(con, cfg, rule_id):
    r = _rule(con, rule_id)
    if r["status"] not in ("candidate", "pending_approval"):
        return {"error": f"rule is {r['status']}"}
    lc = cfg["learning"]
    if r["kind"] not in TESTABLE:
        result = {"testable": False, "note": "natural-language rule; not machine-testable"}
        status = "active" if (r["risk"] == "low" and lc["auto_approve_low_risk"]) else "pending_approval"
    else:
        base = [x for x in pipeline.active_rules(con) if x["rule_key"] != r["rule_key"]]
        cases = labelled_cases(con)
        before = evaluate(cases, cfg, base + [x for x in pipeline.active_rules(con) if x["rule_key"] == r["rule_key"]])
        after = evaluate(cases, cfg, base + [r])
        changed = [cid for cid in after["per_case"] if after["per_case"][cid] != before["per_case"][cid]]
        improved = (after["correct"] > before["correct"]) or (after["correct"] == before["correct"] and after["false_accept"] < before["false_accept"])
        safe = after["false_reject"] - before["false_reject"] <= lc["max_false_rejection_increase"]
        passed = bool(cases) and improved and safe
        result = {"testable": True, "cases": len(cases), "low_evidence": len(cases) < lc["min_labelled_cases"],
                  "before": {k: before[k] for k in ("accuracy", "correct", "false_accept", "false_reject")},
                  "after": {k: after[k] for k in ("accuracy", "correct", "false_accept", "false_reject")},
                  "changed_cases": changed, "passed": passed,
                  "reason": "improves accuracy without new false rejections" if passed else
                            ("no labelled cases" if not cases else "no improvement" if not improved else "adds false rejections")}
        status = "pending_approval" if passed else "rejected"
        if passed and r["risk"] == "low" and lc["auto_approve_low_risk"] and not result["low_evidence"]:
            status = "active"
    con.execute("UPDATE rules SET status=?, test_result=? WHERE id=?", (status, json.dumps(result), rule_id))
    _event(con, rule_id, "tested", result)
    if status == "rejected":
        con.execute("UPDATE rules SET decided_at=?, decided_by='test' WHERE id=?", (now(), rule_id))
        _event(con, rule_id, "rejected", "failed validation", "test")
    if status == "active":
        _activate(con, cfg, r, "auto(low-risk)")
    con.commit()
    return {"rule_id": rule_id, "status": status, "result": result}


# ---------------------------------------------------------------- REMEMBER / APPROVE / ROLLBACK
def _activate(con, cfg, r, actor):
    for old in rows(con, "SELECT id FROM rules WHERE rule_key=? AND status='active' AND id!=?", (r["rule_key"], r["id"])):
        con.execute("UPDATE rules SET status='retired' WHERE id=?", (old["id"],))
        _event(con, old["id"], "retired", f"superseded by {r['id']}", actor)
    con.execute("UPDATE rules SET status='active', decided_at=?, decided_by=? WHERE id=?", (now(), actor, r["id"]))
    _event(con, r["id"], "activated", "", actor)
    con.commit()
    pipeline.qualify(con, cfg)  # re-apply to live queue
    export_knowledge(con)


def approve(con, cfg, rule_id, actor="human"):
    r = _rule(con, rule_id)
    if r["status"] != "pending_approval":
        raise SystemExit(f"rule {rule_id} is '{r['status']}'; only tested rules awaiting approval can be approved")
    _activate(con, cfg, r, actor)
    log(con, actor, "approve rule", {"rule": rule_id})
    con.commit()
    return _rule(con, rule_id)


def reject(con, cfg, rule_id, actor="human", reason=""):
    r = _rule(con, rule_id)
    if r["status"] not in ("candidate", "pending_approval"):
        raise SystemExit(f"rule {rule_id} is '{r['status']}'")
    con.execute("UPDATE rules SET status='rejected', decided_at=?, decided_by=? WHERE id=?", (now(), actor, rule_id))
    _event(con, rule_id, "rejected", reason, actor)
    log(con, actor, "reject rule", {"rule": rule_id})
    con.commit()
    export_knowledge(con)
    return _rule(con, rule_id)


def rollback(con, cfg, rule_id, actor="human"):
    """Deactivate an active rule version; restore the version it replaced (if any)."""
    r = _rule(con, rule_id)
    if r["status"] != "active":
        raise SystemExit(f"rule {rule_id} is '{r['status']}', only active rules can be rolled back")
    con.execute("UPDATE rules SET status='rolled_back', decided_at=?, decided_by=? WHERE id=?", (now(), actor, rule_id))
    _event(con, rule_id, "rolled_back", "", actor)
    restored = None
    if r["parent_id"]:
        p = one(con, "SELECT id, status FROM rules WHERE id=?", (r["parent_id"],))
        if p and p["status"] == "retired":
            con.execute("UPDATE rules SET status='active' WHERE id=?", (p["id"],))
            _event(con, p["id"], "restored", f"rollback of {rule_id}", actor)
            restored = p["id"]
    log(con, actor, "rollback rule", {"rule": rule_id, "restored": restored})
    con.commit()
    pipeline.qualify(con, cfg)
    export_knowledge(con)
    return {"rolled_back": rule_id, "restored": restored}


def export_knowledge(con, root=None):
    """Write approved rules to git-tracked Markdown (the human-readable versioned knowledge base)."""
    root = Path(root or ROOT) / "knowledge" / "rules"
    root.mkdir(parents=True, exist_ok=True)
    act = rows(con, "SELECT * FROM rules WHERE status='active' ORDER BY agent, kind, id")
    lines = ["# Active learned rules", "", "Generated by `fic`. Do not edit by hand; use approve/reject/rollback.", ""]
    for r in act:
        lines += [f"## {r['rule_key']} (v{r['version']}, id {r['id']})", f"- agent: {r['agent']} | kind: {r['kind']} | risk: {r['risk']}",
                  f"- spec: `{r['spec']}`", f"- why: {r['rationale']}", f"- approved: {r['decided_at']} by {r['decided_by']}", ""]
    (root / "active_rules.md").write_text("\n".join(lines if act else lines + ["_No active rules yet._", ""]))
    ev = rows(con, "SELECT e.*, r.rule_key, r.version FROM rule_events e JOIN rules r ON r.id=e.rule_id ORDER BY e.id")
    hist = ["# Rule history", "", "| when | rule | v | event | actor |", "|---|---|---|---|---|"]
    hist += [f"| {e['created']} | {e['rule_key']} | {e['version']} | {e['event']} | {e['actor']} |" for e in ev]
    (root / "history.md").write_text("\n".join(hist) + "\n")


# ---------------------------------------------------------------- weekly cycle + metrics
def metrics(con, cfg):
    cases = labelled_cases(con)
    det = evaluate(cases, cfg, pipeline.active_rules(con)) if cases else None
    ai = [c for c in cases if c["ai_classification"]]
    ai_correct = sum(BUCKET[c["ai_classification"]] == BUCKET[c["label"]] for c in ai)
    err = rows(con, "SELECT agent, kind, count(*) n FROM errors GROUP BY agent, kind ORDER BY n DESC")
    runs = rows(con, "SELECT agent, count(*) n, sum(qc_passed) ok, sum(est_tokens) tok FROM agent_runs GROUP BY agent")
    return {"labelled_cases": len(cases),
            "rules_engine": {k: det[k] for k in ("accuracy", "false_accept", "false_reject")} if det else None,
            "ai_qualifier_accuracy": round(ai_correct / len(ai), 3) if ai else None, "ai_labelled": len(ai),
            "errors": err, "runs": runs}


def weekly(con, cfg):
    before = metrics(con, cfg)
    new = diagnose(con, cfg)
    tested = [test_rule(con, cfg, r["id"]) for r in rows(con, "SELECT id FROM rules WHERE status='candidate'")]
    after = metrics(con, cfg)
    con.commit()
    return {"proposed": new, "tested": tested, "metrics_before": before, "metrics_after": after,
            "pending_approval": rows(con, "SELECT id, rule_key, kind, rationale FROM rules WHERE status='pending_approval'")}
