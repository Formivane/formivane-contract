import json
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from fic import learning, orchestrator, pipeline
from fic.db import one, rows

from .conftest import release

DSPT = ("NHS patient booking website", "Build an accessible WordPress website with online appointment booking. "
        "Suppliers must complete the Data Security and Protection Toolkit (DSPT) to Standards Met before award.")


def ingest(con, cfg, rels):
    pipeline.collect(con, cfg, ["cf"], releases={"cf": rels})
    return {o["title"]: o for o in rows(con, "SELECT * FROM opportunities")}


def good_qualify(opp, cls="Possible candidate"):
    return {"opp_id": opp["id"], "classification": cls, "eligibility": {"status": "no_blockers_found", "requirements": []},
            "feasibility_1to5": 4, "effort_days": [10, 20], "risks": [], "reasoning": "fits", "sources": [opp["url"]]}


def test_routing_budget_and_followups(cfg, con):
    opps = ingest(con, cfg, [release("Website redesign", "WordPress CMS website", buyer="A Council"),
                             release("Website accessibility fixes", "WordPress website", buyer="A Council"),
                             release("Office cleaning", "cleaning", cpv="90910000")])
    made = orchestrator.plan_daily(con, cfg)
    kinds = [k for k, _ in made]
    assert kinds.count("qualify") == 2  # cleaning not routed
    assert kinds.count("buyer_research") <= 1  # one per buyer, not per opportunity
    assert orchestrator.plan_daily(con, cfg) == []  # no duplicate work
    t = orchestrator.next_task(con, cfg, kind="qualify")
    assert t["status"] == "OK" and "description" in t["packet"]["opportunity"] and "output_schema" in t["packet"]
    opp = one(con, "SELECT * FROM opportunities WHERE id=?", (t["packet"]["opportunity"]["opp_id"],))
    r = orchestrator.submit(con, cfg, t["packet"]["task_id"], good_qualify(opp))
    assert r["qc_passed"] and r["follow_up"] == ["tech_scope"]  # only positive results get scoped
    # budget exhaustion
    cfg["budget"]["daily_ai_tasks"] = 1
    assert orchestrator.next_task(con, cfg)["status"] == "BUDGET_EXHAUSTED"


def test_audit_never_uses_reserve(cfg, con):
    cfg["budget"].update(daily_ai_tasks=10, audit_pct=0, business_pct=90, reserve_pct=10)
    orchestrator.enqueue(con, "audit", None, {"agent_run_ids": []}, dedupe="a1")
    con.commit()
    assert orchestrator.next_task(con, cfg, kind="audit")["status"] == "BUDGET_EXHAUSTED"


def test_qc_catches_fabrication(cfg, con):
    o = list(ingest(con, cfg, [release("Website build", "WordPress website. Suppliers must hold Cyber Essentials.")]).values())[0]
    orchestrator.plan_daily(con, cfg)
    t = orchestrator.next_task(con, cfg, kind="qualify")["packet"]
    bad = good_qualify(o, "Strong candidate")
    bad["eligibility"] = {"status": "confirmed", "requirements": [{"req": "ISO", "met": "unknown", "evidence": "holds ISO 27001 and SC clearance"}]}
    r = orchestrator.submit(con, cfg, t["task_id"], bad)
    issues = " ".join(r["qc_issues"])
    assert not r["qc_passed"] and "evidence not found" in issues and "Strong candidate with unverified" in issues and "confirmed" in issues
    assert one(con, "SELECT ai_classification FROM opportunities WHERE id=?", (o["id"],))["ai_classification"] is None  # failed output not applied
    assert rows(con, "SELECT count(*) n FROM errors WHERE kind='qc_violation'")[0]["n"] >= 3
    # proposal claiming credentials not in profile
    orchestrator.enqueue(con, "proposal", o["id"]); con.commit()
    pt = orchestrator.next_task(con, cfg, kind="proposal")["packet"]
    r = orchestrator.submit(con, cfg, pt["task_id"], {"opp_id": o["id"], "title": "x", "draft_md": "We are ISO 27001 certified and have delivered 50 sites.",
                                                       "missing_info": [], "claims_needing_verification": []})
    assert not r["qc_passed"] and rows(con, "SELECT count(*) n FROM proposals")[0]["n"] == 0


def test_stale_task_recovery(cfg, con):
    orchestrator.enqueue(con, "qualify", 1, dedupe="s1")
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    con.execute("UPDATE tasks SET status='in_progress', started=?, attempts=1", (old,)); con.commit()
    assert orchestrator.recover_stale(con, cfg) == 1
    assert one(con, "SELECT status FROM tasks")["status"] == "queued"


def _labelled_history(con, cfg):
    """Realistic mini history: the DSPT tender is the deliberately introduced classification error."""
    opps = ingest(con, cfg, [release(*DSPT, buyer="NHS Trust X", value=60000),
                             release("Council website redesign", "WordPress website redesign with CMS training", buyer="B Council", value=30000),
                             release("CRM implementation", "HubSpot CRM implementation and workflow automation", buyer="C College", value=45000),
                             release("Grounds maintenance", "grounds maintenance and cleaning", cpv="77310000", value=90000),
                             release("Booking system for leisure centre", "Online appointment booking website and dashboard", buyer="D Council", value=20000)])
    for t, lab in [("Council website redesign", "Strong candidate"), ("CRM implementation", "Possible candidate"),
                   ("Grounds maintenance", "Unlikely to be suitable"), ("Booking system for leisure centre", "Strong candidate")]:
        learning.record_feedback(con, cfg, opps[t]["id"], lab, "owner review")
    return opps


def test_self_learning_loop_with_deliberate_error(cfg, con):
    cfg["profile"]["accreditations"]["dspt"] = False  # owner has declared they don't hold DSPT
    opps = _labelled_history(con, cfg)
    target = opps[DSPT[0]]
    assert target["classification"] in ("Strong candidate", "Possible candidate")  # the deliberate error: DSPT not gated
    # human feedback reveals the error
    learning.record_feedback(con, cfg, target["id"], "Ineligible", "Requires DSPT, which we don't hold")
    assert one(con, "SELECT count(*) n FROM errors WHERE kind='misclassification' AND opp_id=?", (target["id"],))["n"] == 1
    # auditor diagnoses and proposes, then tests on history
    res = learning.weekly(con, cfg)
    assert res["proposed"], "no rule proposed"
    rule = one(con, "SELECT * FROM rules WHERE id=?", (res["proposed"][0],))
    assert rule["kind"] == "eligibility_gate" and "dspt" in rule["spec"].lower()
    tr = json.loads(rule["test_result"])
    assert tr["passed"] and tr["after"]["correct"] > tr["before"]["correct"] and tr["after"]["false_reject"] == tr["before"]["false_reject"]
    # high-risk: NOT active until the owner approves
    assert rule["status"] == "pending_approval"
    assert one(con, "SELECT classification FROM opportunities WHERE id=?", (target["id"],))["classification"] != "Ineligible"
    learning.approve(con, cfg, rule["id"])
    assert one(con, "SELECT classification FROM opportunities WHERE id=?", (target["id"],))["classification"] == "Ineligible"
    assert learning.metrics(con, cfg)["rules_engine"]["accuracy"] == 1.0
    # rollback restores previous behaviour and is recorded
    learning.rollback(con, cfg, rule["id"])
    assert one(con, "SELECT status FROM rules WHERE id=?", (rule["id"],))["status"] == "rolled_back"
    assert one(con, "SELECT classification FROM opportunities WHERE id=?", (target["id"],))["classification"] != "Ineligible"
    events = [e["event"] for e in rows(con, "SELECT event FROM rule_events WHERE rule_id=? ORDER BY id", (rule["id"],))]
    assert events == ["proposed", "tested", "activated", "rolled_back"]


def test_rejected_rules_are_never_adopted(cfg, con):
    _labelled_history(con, cfg)
    res = learning.propose(con, "eligibility_gate", "qualification-analyst", {"pattern": "website", "effect": "ineligible"}, "bad idea", {}, "test")
    t = learning.test_rule(con, cfg, res["rule_id"])
    assert t["status"] == "rejected" and t["result"]["after"]["false_reject"] > t["result"]["before"]["false_reject"]
    assert not pipeline.active_rules(con)
    with pytest.raises(SystemExit):
        learning.approve(con, cfg, res["rule_id"])
    # manual reject of a pending rule also stays out
    res2 = learning.propose(con, "observation", "buyer-researcher", {"text": "NHS buyers often use Atamis"}, "obs", {}, "test")
    assert learning.test_rule(con, cfg, res2["rule_id"])["status"] == "active"  # low-risk memory auto-saved
    res3 = learning.propose(con, "guidance", "qualification-analyst", {"text": "be stricter"}, "g", {}, "test")
    assert learning.test_rule(con, cfg, res3["rule_id"])["status"] == "pending_approval"
    learning.reject(con, cfg, res3["rule_id"])
    assert res3["rule_id"] not in [r["id"] for r in pipeline.active_rules(con)]


def test_duplicate_and_conflicting_rules(cfg, con):
    a = learning.propose(con, "relevance_include", "*", {"term": "intranet"}, "x", {}, "t")
    assert learning.propose(con, "relevance_include", "*", {"term": "intranet"}, "x", {}, "t")["duplicate"]
    assert learning.propose(con, "relevance_exclude", "*", {"term": "intranet"}, "x", {}, "t")["conflict"]
    assert "invalid regex" in learning.propose(con, "eligibility_gate", "*", {"pattern": "(("}, "x", {}, "t")["error"]
    assert a["rule_id"]


def test_rule_versioning_rollback_restores_parent(cfg, con):
    _labelled_history(con, cfg)
    cfg["profile"]["accreditations"]["dspt"] = False
    learning.record_feedback(con, cfg, one(con, "SELECT id FROM opportunities WHERE title=?", (DSPT[0],))["id"], "Ineligible", "DSPT")
    v1 = learning.propose(con, "eligibility_gate", "qualification-analyst", {"pattern": "dspt", "label": "DSPT", "profile_key": "accreditations.dspt"}, "v1", {}, "t", rule_key="gate:dspt")
    learning.test_rule(con, cfg, v1["rule_id"]); learning.approve(con, cfg, v1["rule_id"])
    v2 = learning.propose(con, "eligibility_gate", "qualification-analyst", {"pattern": "dspt|data security and protection toolkit", "label": "DSPT", "profile_key": "accreditations.dspt"}, "v2", {}, "t", rule_key="gate:dspt")
    t2 = learning.test_rule(con, cfg, v2["rule_id"])
    assert t2["status"] == "rejected"  # no improvement over v1 -> not adopted
    r2 = one(con, "SELECT version, parent_id FROM rules WHERE id=?", (v2["rule_id"],))
    assert r2["version"] == 2 and r2["parent_id"] == v1["rule_id"]


def test_dashboard_api(cfg, con):
    from http.server import ThreadingHTTPServer
    from fic import server
    _labelled_history(con, cfg)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cfg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert b"<title>Formivane Intelligence Centre</title>" in urllib.request.urlopen(base + "/").read()
        for p in ("/api/overview", "/api/opportunities?scope=all", "/api/agents", "/api/learning", "/api/proposals", "/api/analytics"):
            assert urllib.request.urlopen(base + p).status == 200
        opp = json.loads(urllib.request.urlopen(base + "/api/opportunities?scope=all").read())[0]
        assert json.loads(urllib.request.urlopen(base + f"/api/opportunity/{opp['id']}").read())["card"]["title"]
        req = urllib.request.Request(base + "/api/feedback", data=json.dumps({"opp_id": opp["id"], "label": "Ineligible"}).encode(), method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403  # CSRF guard
        req.add_header("X-FIC", "1"); req.add_header("Content-Type", "application/json")
        assert urllib.request.urlopen(req).status == 200
        bad = urllib.request.Request(base + "/api/rules/999/approve", data=b"{}", method="POST", headers={"X-FIC": "1"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad)
        assert e.value.code == 409
    finally:
        srv.shutdown()
