"""`python -m fic <command>` — the single entry point for scripts, schedulers and Claude agents."""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, learning, orchestrator, pipeline, reports, rules_engine
from .db import connect, log, now, one, rows


def out(obj):
    print(json.dumps(obj, indent=1, default=str, ensure_ascii=False))


def _load_json(a):
    if a.file:
        return json.loads(Path(a.file).read_text())
    if a.json:
        return json.loads(a.json)
    return json.loads(sys.stdin.read())


def award_intel(con):
    horizon = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
    return {
        "top_buyers": rows(con, """SELECT buyer, count(*) awards, round(avg(value)) avg_value, round(sum(value)) total
                                   FROM awards WHERE relevant=1 GROUP BY buyer ORDER BY awards DESC, total DESC LIMIT 15"""),
        "top_suppliers": rows(con, """SELECT supplier, count(*) wins, round(sum(value)) total FROM awards WHERE relevant=1
                                      GROUP BY supplier ORDER BY wins DESC LIMIT 15"""),
        "contracts_ending_12m": rows(con, """SELECT buyer, title, supplier, value, contract_end, url FROM awards
                                             WHERE relevant=1 AND contract_end BETWEEN ? AND ? ORDER BY contract_end LIMIT 25""", (now(), horizon)),
        "note": "Contract end dates are evidence of possible re-procurement, not confirmation of a renewal tender.",
    }


def main(argv=None):
    p = argparse.ArgumentParser(prog="fic", description="Formivane Intelligence Centre")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    c = sub.add_parser("collect"); c.add_argument("--source", choices=["fts", "cf"], action="append")
    sub.add_parser("qualify")
    sub.add_parser("status")
    l = sub.add_parser("list"); l.add_argument("--cls"); l.add_argument("--limit", type=int, default=20); l.add_argument("--all", action="store_true")
    s = sub.add_parser("show"); s.add_argument("opp_id", type=int)
    pl = sub.add_parser("plan"); pl.add_argument("which", choices=["daily", "weekly"])
    t = sub.add_parser("task"); t.add_argument("action", choices=["next", "submit", "fail", "list"])
    t.add_argument("task_id", type=int, nargs="?"); t.add_argument("--agent"); t.add_argument("--kind")
    t.add_argument("--file"); t.add_argument("--json"); t.add_argument("--error", default="agent failure")
    sub.add_parser("budget")
    f = sub.add_parser("feedback"); f.add_argument("opp_id", type=int); f.add_argument("label", choices=rules_engine.CLASSES); f.add_argument("--note", default="")
    r = sub.add_parser("rules"); r.add_argument("action", choices=["list", "propose", "test", "approve", "reject", "rollback", "history"])
    r.add_argument("rule_id", type=int, nargs="?"); r.add_argument("--status"); r.add_argument("--kind"); r.add_argument("--agent", default="qualification-analyst")
    r.add_argument("--spec"); r.add_argument("--rationale", default=""); r.add_argument("--reason", default="")
    sub.add_parser("learn")
    pr = sub.add_parser("proposal"); pr.add_argument("action", choices=["request", "list", "show", "approve", "reject"]); pr.add_argument("id", type=int, nargs="?")
    o = sub.add_parser("outcome"); o.add_argument("opp_id", type=int)
    for k in ("--submitted", "--won"):
        o.add_argument(k, type=int, choices=[0, 1])
    for k in ("--contract-value", "--revenue", "--est-cost", "--actual-cost"):
        o.add_argument(k, type=float)
    o.add_argument("--note")
    pp = sub.add_parser("prospect"); pp.add_argument("action", choices=["add", "list"]); pp.add_argument("--name"); pp.add_argument("--website")
    pp.add_argument("--sector"); pp.add_argument("--source-url"); pp.add_argument("--notes", default="")
    pp.add_argument("--subscriber-type", choices=["corporate", "individual", "unknown"], default="unknown")
    sub.add_parser("awards")
    rp = sub.add_parser("report"); rp.add_argument("which", choices=["daily"])
    d = sub.add_parser("dashboard"); d.add_argument("--port", type=int, default=8765); d.add_argument("--host", default="127.0.0.1")
    sub.add_parser("export-knowledge")
    a = p.parse_args(argv)

    cfg = config.load()
    con = connect(cfg["db_path"])

    if a.cmd == "init":
        learning.export_knowledge(con)
        out({"db": cfg["db_path"], "ok": True})
    elif a.cmd == "collect":
        res = pipeline.collect(con, cfg, a.source)
        pipeline.expire_old(con)
        out(res)
        if any(v["status"] == "error" for v in res.values()):
            sys.exit(2)  # partial failure -> scheduler logs it; next run retries from old watermark
    elif a.cmd == "qualify":
        out({"queued": pipeline.qualify(con, cfg)}); con.commit()
    elif a.cmd == "status":
        out({"opportunities": rows(con, "SELECT status, count(*) n FROM opportunities GROUP BY status"),
             "classes": rows(con, "SELECT classification, count(*) n FROM opportunities WHERE status IN ('queued','analysed') GROUP BY classification"),
             "tasks": rows(con, "SELECT status, count(*) n FROM tasks GROUP BY status"),
             "rules": rows(con, "SELECT status, count(*) n FROM rules GROUP BY status"),
             "last_runs": rows(con, "SELECT source, stage, status, fetched, new, duplicates, errors, finished FROM collection_runs ORDER BY id DESC LIMIT 4"),
             "budget": orchestrator.budget_status(con, cfg)})
    elif a.cmd == "list":
        q = "SELECT id, coalesce(ai_classification,classification) cls, score, value_amount value, deadline, title, buyer FROM opportunities WHERE duplicate_of IS NULL"
        args = []
        if not a.all:
            q += " AND status IN ('queued','analysed','shortlisted')"
        if a.cls:
            q += " AND coalesce(ai_classification,classification)=?"; args.append(a.cls)
        out(rows(con, q + " ORDER BY score DESC LIMIT ?", args + [a.limit]))
    elif a.cmd == "show":
        o = one(con, "SELECT * FROM opportunities WHERE id=?", (a.opp_id,))
        out(reports.opportunity_card(o) | {"runs": rows(con, "SELECT id, agent, decision, qc_passed, qc_issues FROM agent_runs WHERE opp_id=?", (a.opp_id,))} if o else {"error": "not found"})
    elif a.cmd == "plan":
        out({"created": orchestrator.plan_daily(con, cfg)} if a.which == "daily" else {"audit_task": orchestrator.plan_weekly_audit(con, cfg)})
    elif a.cmd == "task":
        if a.action == "next":
            res = orchestrator.next_task(con, cfg, a.agent, a.kind)
            out(res)
            if res["status"] == "BUDGET_EXHAUSTED":
                sys.exit(3)
        elif a.action == "submit":
            out(orchestrator.submit(con, cfg, a.task_id, _load_json(a)))
        elif a.action == "fail":
            orchestrator.fail(con, a.task_id, a.error); out({"ok": True})
        else:
            out(rows(con, "SELECT id, kind, agent, opp_id, status, priority, attempts FROM tasks WHERE status IN ('queued','in_progress') ORDER BY priority DESC"))
    elif a.cmd == "budget":
        out(orchestrator.budget_status(con, cfg))
    elif a.cmd == "feedback":
        out({"feedback_id": learning.record_feedback(con, cfg, a.opp_id, a.label, a.note)})
    elif a.cmd == "rules":
        if a.action == "list":
            q, args = "SELECT id, rule_key, version, kind, agent, status, risk, rationale, test_result FROM rules", []
            if a.status:
                q += " WHERE status=?"; args.append(a.status)
            out(rows(con, q + " ORDER BY id DESC", args))
        elif a.action == "propose":
            res = learning.propose(con, a.kind, a.agent, json.loads(a.spec), a.rationale, {"manual": True}, "human")
            if res.get("rule_id"):
                res["test"] = learning.test_rule(con, cfg, res["rule_id"])
            out(res)
        elif a.action == "test":
            out(learning.test_rule(con, cfg, a.rule_id))
        elif a.action == "approve":
            out(learning.approve(con, cfg, a.rule_id))
        elif a.action == "reject":
            out(learning.reject(con, cfg, a.rule_id, reason=a.reason))
        elif a.action == "rollback":
            out(learning.rollback(con, cfg, a.rule_id))
        else:
            out(rows(con, "SELECT e.created, e.rule_id, r.rule_key, r.version, e.event, e.actor FROM rule_events e JOIN rules r ON r.id=e.rule_id ORDER BY e.id DESC LIMIT 50"))
    elif a.cmd == "learn":
        res = learning.weekly(con, cfg)
        out({"report": reports.weekly(con, cfg, res), "pending_approval": res["pending_approval"]})
    elif a.cmd == "proposal":
        if a.action == "request":  # human-initiated only
            tid = orchestrator.enqueue(con, "proposal", a.id, priority=90); con.commit()
            log(con, "human", "proposal requested", {"opp": a.id}); con.commit()
            out({"task_id": tid, "note": "Run /fic-proposal in Claude Code to draft it."})
        elif a.action == "list":
            out(rows(con, "SELECT id, opp_id, title, status, created FROM proposals ORDER BY id DESC"))
        elif a.action == "show":
            out(one(con, "SELECT * FROM proposals WHERE id=?", (a.id,)))
        else:
            st = "approved" if a.action == "approve" else "rejected"
            con.execute("UPDATE proposals SET status=?, updated=? WHERE id=?", (st, now(), a.id))
            log(con, "human", f"proposal {st}", {"proposal": a.id}); con.commit()
            out({"id": a.id, "status": st, "note": "Approval marks it ready. Submission is always done by you on the buyer's portal."})
    elif a.cmd == "outcome":
        vals = {"submitted": a.submitted, "won": a.won, "contract_value": a.contract_value, "revenue_received": a.revenue,
                "est_cost": a.est_cost, "actual_cost": a.actual_cost, "note": a.note}
        vals = {k: v for k, v in vals.items() if v is not None}
        con.execute("INSERT INTO outcomes(opp_id, updated) VALUES(?,?) ON CONFLICT(opp_id) DO NOTHING", (a.opp_id, now()))
        if vals:
            con.execute(f"UPDATE outcomes SET {','.join(k + '=?' for k in vals)}, updated=? WHERE opp_id=?", list(vals.values()) + [now(), a.opp_id])
        if a.won:  # a win proves it was winnable; a loss alone doesn't prove the classification wrong
            learning.record_feedback(con, cfg, a.opp_id, "Strong candidate", "won contract", "outcome")
        con.commit()
        out(one(con, "SELECT * FROM outcomes WHERE opp_id=?", (a.opp_id,)))
    elif a.cmd == "prospect":
        if a.action == "add":
            if not a.name or not a.source_url:
                sys.exit("--name and --source-url (public evidence) are required")
            con.execute("""INSERT INTO prospects(name,website,sector,source_url,notes,subscriber_type,created) VALUES(?,?,?,?,?,?,?)""",
                        (a.name, a.website, a.sector, a.source_url, a.notes, a.subscriber_type, now()))
            con.commit()
            out({"ok": True, "pecr": "No outreach is sent by FIC. Individual subscribers (sole traders/partnerships) need prior consent for marketing email; corporate subscribers need an opt-out in every message."})
        else:
            out(rows(con, "SELECT * FROM prospects ORDER BY id DESC"))
    elif a.cmd == "awards":
        out(award_intel(con))
    elif a.cmd == "report":
        text, _ = reports.daily(con, cfg)
        print(text)
    elif a.cmd == "dashboard":
        from . import server
        server.serve(cfg, a.host, a.port)
    elif a.cmd == "export-knowledge":
        learning.export_knowledge(con); out({"ok": True})
    con.commit()


if __name__ == "__main__":
    main()
