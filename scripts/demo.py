"""SIMULATED demonstration of the learning loop. Writes to data/demo.db only, never your real DB.

  python scripts/demo.py && FIC_DB=data/demo.db python -m fic dashboard

Uses real notices from tests/fixtures plus synthetic labelled cases. The DSPT tender is a
deliberately introduced classification error; the run shows detection -> rule proposal ->
history test -> pending approval -> approval -> rollback.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "demo.db"
os.environ["FIC_DB"] = str(DB)

from fic import config, learning, orchestrator, pipeline  # noqa: E402
from fic.db import connect, log, one  # noqa: E402
from tests.conftest import release  # noqa: E402


def main():
    for p in DB.parent.glob("demo.db*"):
        p.unlink()
    learning.ROOT = ROOT / "data" / "demo"  # keep demo rules out of the real git-tracked knowledge/
    cfg = config.load()
    cfg["profile"].setdefault("accreditations", {})["dspt"] = False  # demo assumption, stated in output
    con = connect(str(DB))
    log(con, "demo", "SIMULATED DEMO DATA", "This database is a demonstration, not live operation.")
    fx = lambda n: json.loads((ROOT / "tests/fixtures" / n).read_text())["releases"]
    pipeline.collect(con, cfg, ["cf", "fts"], releases={"cf": fx("cf_tender_sample.json"), "fts": fx("fts_tender_sample.json")})
    pipeline.collect(con, cfg, ["cf"], releases={"cf": fx("cf_award_sample.json")})
    syn = [release("NHS patient booking website", "Build an accessible WordPress website with online appointment booking. Suppliers must "
                   "complete the Data Security and Protection Toolkit (DSPT) to Standards Met before award.", "NHS Trust (demo)", 60000),
           release("Council website redesign", "WordPress website redesign with CMS training", "Demo Borough Council", 30000),
           release("CRM implementation", "HubSpot CRM implementation and workflow automation", "Demo College", 45000),
           release("Booking system for leisure centre", "Online appointment booking website and dashboard", "Demo Leisure Trust", 20000)]
    pipeline.collect(con, cfg, ["cf"], releases={"cf": syn})
    ids = {o["title"]: o["id"] for o in con.execute("SELECT id, title FROM opportunities").fetchall()}
    for t, lab in [("Council website redesign", "Strong candidate"), ("CRM implementation", "Possible candidate"),
                   ("Booking system for leisure centre", "Strong candidate")]:
        learning.record_feedback(con, cfg, ids[t], lab, "SIMULATED owner label")
    tgt = ids["NHS patient booking website"]
    print("1. Deliberate error: DSPT tender classified as", one(con, "SELECT classification c FROM opportunities WHERE id=?", (tgt,))["c"])
    learning.record_feedback(con, cfg, tgt, "Ineligible", "SIMULATED: requires DSPT, which we don't hold")
    res = learning.weekly(con, cfg)
    r = one(con, "SELECT * FROM rules WHERE id=?", (res["proposed"][0],))
    print("2. Auditor proposed rule", r["id"], r["rule_key"], "->", r["status"])
    print("   test:", json.loads(r["test_result"])["before"], "->", json.loads(r["test_result"])["after"])
    print("3. Still not applied before approval:", one(con, "SELECT classification c FROM opportunities WHERE id=?", (tgt,))["c"])
    bad = learning.propose(con, "eligibility_gate", "qualification-analyst", {"pattern": "website", "effect": "ineligible"},
                           "SIMULATED harmful rule", {"demo": True}, "demo")
    print("4. Harmful rule test ->", learning.test_rule(con, cfg, bad["rule_id"])["status"])
    orchestrator.plan_daily(con, cfg)
    print("5. Leaving rule", r["id"], "pending so you can approve/roll back it in the dashboard Learning Centre.")
    print("   Run: FIC_DB=data/demo.db python -m fic dashboard")
    con.commit()


if __name__ == "__main__":
    main()
