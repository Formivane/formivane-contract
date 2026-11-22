"""Dashboard: stdlib HTTP server + JSON API + one static page. Binds to localhost only by default."""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import learning, orchestrator, reports, rules_engine
from .db import connect, log, now, one, rows

STATIC = Path(__file__).parent / "static" / "index.html"


def api_get(con, cfg, path, qs):
    if path == "/api/overview":
        return {
            "counts": one(con, """SELECT count(*) total,
                     sum(first_seen >= datetime('now','-1 day')) new_24h,
                     sum(status IN ('queued','analysed') AND coalesce(ai_classification,classification) IN ('Strong candidate','Possible candidate','Requires investigation')) qualified,
                     sum(CASE WHEN status IN ('queued','analysed') AND coalesce(ai_classification,classification) IN ('Strong candidate','Possible candidate') THEN value_amount END) potential_value
                     FROM opportunities WHERE duplicate_of IS NULL"""),
            "deadlines": rows(con, """SELECT id, title, buyer, deadline, coalesce(ai_classification,classification) cls FROM opportunities
                                      WHERE status IN ('queued','analysed') AND deadline >= ? AND duplicate_of IS NULL
                                      AND coalesce(ai_classification,classification) IN ('Strong candidate','Possible candidate','Requires investigation')
                                      ORDER BY deadline LIMIT 8""", (now(),)),
            "tasks": rows(con, "SELECT status, count(*) n FROM tasks GROUP BY status"),
            "active_tasks": rows(con, "SELECT id, kind, agent, opp_id, status FROM tasks WHERE status IN ('queued','in_progress') ORDER BY priority DESC LIMIT 10"),
            "activity": rows(con, "SELECT actor, action, detail, created FROM activity ORDER BY id DESC LIMIT 15"),
            "runs": rows(con, "SELECT source, stage, status, fetched, new, duplicates, errors, finished FROM collection_runs ORDER BY id DESC LIMIT 6"),
            "budget": orchestrator.budget_status(con, cfg),
            "pending_rules": one(con, "SELECT count(*) n FROM rules WHERE status='pending_approval'")["n"],
            "draft_proposals": one(con, "SELECT count(*) n FROM proposals WHERE status='draft'")["n"],
        }
    if path == "/api/opportunities":
        q = "SELECT id, title, buyer, value_amount, deadline, score, coalesce(ai_classification,classification) cls, eligibility_status, category, source, status FROM opportunities WHERE duplicate_of IS NULL"
        args = []
        if qs.get("q"):
            q += " AND (title LIKE ? OR buyer LIKE ?)"; args += [f"%{qs['q']}%"] * 2
        if qs.get("cls"):
            q += " AND coalesce(ai_classification,classification)=?"; args.append(qs["cls"])
        if qs.get("scope", "active") == "active":
            q += " AND status IN ('queued','analysed','shortlisted')"
        return rows(con, q + " ORDER BY score DESC LIMIT 300", args)
    m = re.match(r"^/api/opportunity/(\d+)$", path)
    if m:
        o = one(con, "SELECT * FROM opportunities WHERE id=?", (int(m[1]),))
        if not o:
            return None
        return {"card": reports.opportunity_card(o), "eligibility": json.loads(o["eligibility_json"] or "{}"),
                "score": json.loads(o["score_json"] or "{}"), "relevance_terms": json.loads(o["relevance_terms"] or "[]"),
                "description": o["description"], "requirements_text": o["requirements_text"], "method": o["procurement_method"],
                "sme_suitable": o["sme_suitable"], "cpv": o["cpv_all"], "notice_id": o["notice_id"], "status": o["status"],
                "runs": [dict(r, output=json.loads(r["output"]), qc_issues=json.loads(r["qc_issues"] or "[]"))
                         for r in rows(con, "SELECT id, agent, decision, qc_passed, qc_issues, output, created FROM agent_runs WHERE opp_id=? ORDER BY id", (o["id"],))],
                "feedback": rows(con, "SELECT label_classification, note, source, created FROM feedback WHERE opp_id=? ORDER BY id DESC", (o["id"],)),
                "buyer_awards": rows(con, "SELECT title, supplier, value, award_date, contract_end, url FROM awards WHERE buyer=? ORDER BY award_date DESC LIMIT 10", (o["buyer"],)),
                "classes": rules_engine.CLASSES}
    if path == "/api/agents":
        out = []
        for name, role in orchestrator.AGENTS.items():
            st = one(con, """SELECT count(*) runs, sum(qc_passed) ok, max(created) last, sum(est_tokens) tok FROM agent_runs WHERE agent=?""", (name,))
            busy = one(con, "SELECT count(*) n FROM tasks WHERE agent=? AND status='in_progress'", (name,))["n"]
            queued = one(con, "SELECT count(*) n FROM tasks WHERE agent=? AND status='queued'", (name,))["n"]
            if name == "opportunity-scout":
                cr = one(con, "SELECT count(*) runs, sum(status='ok') ok, max(finished) last FROM collection_runs")
                st = {"runs": cr["runs"], "ok": cr["ok"], "last": cr["last"], "tok": 0}
            out.append({"name": name, "role": role, "status": "working" if busy else ("queued" if queued else "idle"), "queued": queued,
                        **st, "mistakes": rows(con, "SELECT kind, detail, created FROM errors WHERE agent=? ORDER BY id DESC LIMIT 4", (name,)),
                        "rules": rows(con, "SELECT id, rule_key, version FROM rules WHERE status='active' AND (agent=? OR agent='*')", (name,))})
        return out
    if path == "/api/learning":
        rs = rows(con, "SELECT * FROM rules ORDER BY id DESC")
        for r in rs:
            for k in ("spec", "test_result", "evidence"):
                r[k] = json.loads(r[k]) if r[k] else None
        return {"rules": rs, "errors": rows(con, "SELECT * FROM errors ORDER BY id DESC LIMIT 100"),
                "events": rows(con, "SELECT e.*, r.rule_key, r.version FROM rule_events e JOIN rules r ON r.id=e.rule_id ORDER BY e.id DESC LIMIT 100"),
                "metrics": learning.metrics(con, cfg)}
    if path == "/api/proposals":
        return rows(con, """SELECT p.*, o.title opp_title, o.deadline, o.url FROM proposals p LEFT JOIN opportunities o ON o.id=p.opp_id ORDER BY p.id DESC""")
    if path == "/api/analytics":
        return reports.analytics(con, cfg)
    return None


def api_post(con, cfg, path, body):
    m = re.match(r"^/api/rules/(\d+)/(approve|reject|rollback)$", path)
    if m:
        rid, act = int(m[1]), m[2]
        fn = {"approve": learning.approve, "reject": learning.reject, "rollback": learning.rollback}[act]
        return fn(con, cfg, rid, "human(dashboard)") if act != "reject" else learning.reject(con, cfg, rid, "human(dashboard)", body.get("reason", ""))
    if path == "/api/feedback":
        return {"feedback_id": learning.record_feedback(con, cfg, int(body["opp_id"]), body["label"], body.get("note", ""))}
    m = re.match(r"^/api/proposals/(\d+)/(approve|reject)$", path)
    if m:
        st = "approved" if m[2] == "approve" else "rejected"
        con.execute("UPDATE proposals SET status=?, updated=? WHERE id=?", (st, now(), int(m[1])))
        log(con, "human(dashboard)", f"proposal {st}", {"proposal": int(m[1])})
        con.commit()
        return {"status": st}
    m = re.match(r"^/api/opportunity/(\d+)/request-proposal$", path)
    if m:
        tid = orchestrator.enqueue(con, "proposal", int(m[1]), priority=90)
        log(con, "human(dashboard)", "proposal requested", {"opp": int(m[1])})
        con.commit()
        return {"task_id": tid}
    return None


def make_handler(cfg):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._send(200, STATIC.read_bytes(), "text/html; charset=utf-8")
            con = connect(cfg["db_path"])
            try:
                res = api_get(con, cfg, u.path, {k: v[0] for k, v in parse_qs(u.query).items()})
                self._send(200 if res is not None else 404, res if res is not None else {"error": "not found"})
            finally:
                con.close()

        def do_POST(self):
            # Custom header forces a CORS preflight, so other websites can't drive these actions.
            if self.headers.get("X-FIC") != "1":
                return self._send(403, {"error": "missing X-FIC header"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "bad json"})
            con = connect(cfg["db_path"])
            try:
                res = api_post(con, cfg, urlparse(self.path).path, body)
                self._send(200 if res is not None else 404, res if res is not None else {"error": "not found"})
            except SystemExit as e:  # domain errors raised as SystemExit(msg) by learning.*
                self._send(409, {"error": str(e)})
            except (KeyError, ValueError) as e:
                self._send(400, {"error": str(e)})
            finally:
                con.close()
    return H


def serve(cfg, host="127.0.0.1", port=8765):
    srv = ThreadingHTTPServer((host, port), make_handler(cfg))
    print(f"FIC dashboard on http://{host}:{port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
