"""Collection + deterministic qualification. Runs without Claude."""
import json

from . import connectors, normalise, rules_engine
from .db import get_meta, log, now, one, rows, set_meta

OPP_FIELDS = ["ocid", "source", "notice_id", "title", "description", "requirements_text", "buyer", "buyer_id", "region",
              "cpv", "cpv_all", "value_amount", "value_currency", "deadline", "published", "updated",
              "procurement_method", "sme_suitable", "url", "fingerprint", "raw_hash"]


def active_rules(con, kinds=None):
    rs = rows(con, "SELECT * FROM rules WHERE status='active'")
    for r in rs:
        r["spec"] = json.loads(r["spec"])
    return [r for r in rs if not kinds or r["kind"] in kinds]


def upsert_opportunity(con, rec):
    """Returns 'new' | 'updated' | 'duplicate' | 'unchanged'."""
    ts = now()
    cur = one(con, "SELECT id, raw_hash, updated FROM opportunities WHERE ocid=?", (rec["ocid"],))
    if cur:
        if cur["raw_hash"] == rec["raw_hash"]:
            con.execute("UPDATE opportunities SET last_seen=? WHERE id=?", (ts, cur["id"]))
            return "unchanged", cur["id"]
        if (rec["updated"] or "") < (cur["updated"] or ""):
            return "unchanged", cur["id"]  # older release of same process
        sets = ",".join(f"{f}=?" for f in OPP_FIELDS if f != "ocid")
        con.execute(f"UPDATE opportunities SET {sets}, last_seen=? WHERE id=?",
                    [rec[f] for f in OPP_FIELDS if f != "ocid"] + [ts, cur["id"]])
        return "updated", cur["id"]
    # cross-source duplicate: same normalised title+buyer and same deadline day
    dup = one(con, "SELECT id FROM opportunities WHERE fingerprint=? AND substr(coalesce(deadline,''),1,10)=? AND duplicate_of IS NULL",
              (rec["fingerprint"], (rec["deadline"] or "")[:10]))
    cols = OPP_FIELDS + ["first_seen", "last_seen", "status", "duplicate_of"]
    vals = [rec[f] for f in OPP_FIELDS] + [ts, ts, "duplicate" if dup else "new", dup["id"] if dup else None]
    cid = con.execute(f"INSERT INTO opportunities({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals).lastrowid
    return ("duplicate" if dup else "new"), cid


def upsert_award(con, a, cfg, rules):
    rel, _ = rules_engine.relevance({"title": a["title"], "description": a.get("description", ""), "cpv": a["cpv"]}, cfg, rules)
    a = {k: v for k, v in a.items() if k != "description"}
    a["relevant"] = int(rel >= cfg["relevance"]["min_score"])
    cols = list(a) + ["first_seen"]
    cur = con.execute(f"INSERT OR IGNORE INTO awards({','.join(cols)}) VALUES({','.join('?' * len(cols))})", list(a.values()) + [now()])
    return cur.rowcount


def buyer_award_count(con, buyer):
    r = one(con, "SELECT count(*) n FROM awards WHERE buyer=? AND relevant=1", (buyer,))
    return r["n"] if r else 0


def qualify(con, cfg, opp_ids=None, rules=None):
    """Deterministic pre-qualification. Stores score/eligibility/classification; queues relevant ones."""
    rules = active_rules(con) if rules is None else rules
    q = "SELECT * FROM opportunities WHERE duplicate_of IS NULL"
    opps = rows(con, q + (f" AND id IN ({','.join('?' * len(opp_ids))})" if opp_ids else " AND status IN ('new','queued','filtered_out')"), opp_ids or ())
    queued = 0
    for o in opps:
        r = rules_engine.classify(o, cfg, cfg["profile"], rules, buyer_awards=buyer_award_count(con, o["buyer"]))
        relevant = r["relevance"] >= cfg["relevance"]["min_score"]
        status = o["status"]
        if status in ("new", "queued", "filtered_out"):
            status = "queued" if relevant and r["classification"] != "Ineligible" and not r["score"]["gate"] else "filtered_out"
        queued += status == "queued"
        con.execute("""UPDATE opportunities SET category=?, relevance=?, relevance_terms=?, eligibility_status=?, eligibility_json=?,
                       classification=?, score=?, score_json=?, status=? WHERE id=?""",
                    (r["category"], r["relevance"], json.dumps(r["relevance_terms"]), r["eligibility"]["status"],
                     json.dumps(r["eligibility"]), r["classification"], r["score"]["score"],
                     json.dumps({"score": r["score"], "economics": r["economics"]}), status, o["id"]))
    return queued


def ingest_release(con, source, rel, stats, cfg, rules):
    tag = rel.get("tag") or []
    if "award" in tag or rel.get("awards"):
        for a in normalise.awards(source, rel):
            stats["awards"] += upsert_award(con, a, cfg, rules)
    if rel.get("tender") and ("tender" in tag or "tenderUpdate" in tag or "planning" in tag):
        res, oid = upsert_opportunity(con, normalise.opportunity(source, rel))
        stats[res] += 1
        if res in ("new", "updated"):
            stats["ids"].append(oid)


def collect(con, cfg, sources=None, session=None, releases=None):
    """Fetch from official APIs (or ingest given releases for tests). Returns per-source stats."""
    rules = active_rules(con)
    out = {}
    cc = cfg["collection"]
    stages = ["tender"] + (["award"] if cc.get("collect_awards") else [])
    for src in sources or cc["sources"]:
        for stage in stages:
            stats = {"fetched": 0, "new": 0, "updated": 0, "duplicate": 0, "unchanged": 0, "awards": 0, "ids": []}
            run_id = con.execute("INSERT INTO collection_runs(source,stage,started,status) VALUES(?,?,?,?)",
                                 (src, stage, now(), "running")).lastrowid
            con.commit()
            wm_key = f"watermark:{src}:{stage}"
            since, until = connectors.window(get_meta(con, wm_key), cc["initial_lookback_days"])
            err, status = None, "ok"
            try:
                it = ((r, 1) for r in releases[src]) if releases is not None else connectors.fetch_releases(src, stage, since, until, cc, session)
                for rel, _page in it:
                    stats["fetched"] += 1
                    ingest_release(con, src, rel, stats, cfg, rules)
                if releases is None:
                    set_meta(con, wm_key, until.isoformat())  # only advance on full success
            except connectors.SourceError as e:
                err, status = str(e), "error"  # keep partial results; watermark not advanced -> retried next run
            stats["queued"] = qualify(con, cfg, stats["ids"], rules) if stats["ids"] else 0
            con.execute("""UPDATE collection_runs SET finished=?, fetched=?, new=?, updated=?, duplicates=?, queued=?,
                           errors=?, status=?, watermark=? WHERE id=?""",
                        (now(), stats["fetched"], stats["new"], stats["updated"], stats["duplicate"], stats["queued"],
                         err, status, until.isoformat(), run_id))
            log(con, "opportunity-scout", f"collect {src}/{stage}",
                {k: v for k, v in stats.items() if k != "ids"} | ({"error": err} if err else {}))
            con.commit()
            out[f"{src}/{stage}"] = {k: v for k, v in stats.items() if k != "ids"} | {"status": status, "error": err}
            if releases is not None:
                break  # test fixtures are not split by stage
    return out


def expire_old(con):
    n = con.execute("""UPDATE opportunities SET status='expired' WHERE deadline IS NOT NULL AND deadline < ?
                       AND status IN ('new','queued','filtered_out','analysed')""", (now(),)).rowcount
    con.commit()
    return n
