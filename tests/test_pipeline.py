import requests

from fic import connectors, normalise, pipeline, rules_engine
from fic.db import connect, get_meta, rows

from .conftest import fixture_releases, release


def test_collect_real_fixtures_and_dedup(cfg, con):
    rel = {"cf": fixture_releases("cf_tender_sample.json"), "fts": fixture_releases("fts_tender_sample.json")}
    res = pipeline.collect(con, cfg, ["cf", "fts"], releases=rel)
    n = rows(con, "SELECT count(*) n FROM opportunities")[0]["n"]
    assert res["cf/tender"]["new"] > 5 and n > 10
    # FTS sample repeats the same ocid across several releases -> stored once
    ocids = [r["ocid"] for r in rel["fts"]]
    assert len(set(ocids)) < len(ocids)
    assert rows(con, "SELECT count(DISTINCT ocid) n FROM opportunities")[0]["n"] == n
    # re-run: nothing new
    res2 = pipeline.collect(con, cfg, ["cf", "fts"], releases=rel)
    assert res2["cf/tender"]["new"] == 0 and res2["fts/tender"]["new"] == 0
    assert rows(con, "SELECT count(*) n FROM opportunities")[0]["n"] == n
    for o in rows(con, "SELECT url, title, buyer FROM opportunities"):
        assert o["url"].startswith("https://") and o["title"] and o["buyer"]


def test_award_ingest(cfg, con):
    pipeline.collect(con, cfg, ["cf"], releases={"cf": fixture_releases("cf_award_sample.json")})
    a = rows(con, "SELECT * FROM awards")
    assert a and all(x["supplier"] and x["buyer"] for x in a)


def test_cross_source_duplicate(cfg, con):
    a = release("Website redesign for the council", buyer="Exampleshire Council")
    b = release("Website redesign for the council", buyer="Exampleshire Council")
    b["tender"]["tenderPeriod"] = a["tender"]["tenderPeriod"]
    pipeline.collect(con, cfg, ["cf"], releases={"cf": [a]})
    pipeline.collect(con, cfg, ["fts"], releases={"fts": [b]})
    st = {r["status"] for r in rows(con, "SELECT status FROM opportunities")}
    assert "duplicate" in st and rows(con, "SELECT count(*) n FROM opportunities WHERE duplicate_of IS NOT NULL")[0]["n"] == 1


def test_relevance_filtering(cfg):
    web = {"title": "Website redesign and CMS migration", "description": "WordPress build", "cpv": "72413000"}
    clean = {"title": "Office cleaning services", "description": "Daily cleaning", "cpv": "90910000"}
    integ = {"title": "Community Integration programme film project", "description": "", "cpv": "98000000"}
    assert rules_engine.relevance(web, cfg)[0] >= cfg["relevance"]["min_score"]
    assert rules_engine.relevance(clean, cfg)[0] < cfg["relevance"]["min_score"]
    assert rules_engine.relevance(integ, cfg)[0] < cfg["relevance"]["min_score"]  # word-boundary, generic term


def test_eligibility_checks(cfg):
    o = {"title": "Website build", "description": "Suppliers must hold Cyber Essentials Plus. Minimum annual turnover of £1m."}
    unknown = rules_engine.eligibility(o, {})
    assert unknown["status"] == "requires_verification"
    assert {r["key"] for r in unknown["requirements"]} >= {"cyber_essentials_plus", "turnover"}
    assert "cyber_essentials" not in {r["key"] for r in unknown["requirements"]}  # plus does not double count
    no = rules_engine.eligibility(o, {"accreditations": {"cyber_essentials_plus": False}})
    assert no["status"] == "ineligible"
    low_turnover = rules_engine.eligibility(o, {"accreditations": {"cyber_essentials_plus": True}, "finance": {"annual_turnover_gbp": 400000}})
    assert low_turnover["status"] == "ineligible"
    ok = rules_engine.eligibility(o, {"accreditations": {"cyber_essentials_plus": True}, "finance": {"annual_turnover_gbp": 2_000_000}})
    assert ok["status"] == "likely_eligible_unverified"  # never 'eligible'/'confirmed'
    assert rules_engine.eligibility({"title": "Website", "description": "A simple site"}, {})["status"] == "no_blockers_found"


def test_scoring_gates_and_weights(cfg):
    o = {"title": "Website redesign", "description": "Must hold ISO 27001.", "value_amount": 40000, "cpv": "72413000",
         "deadline": "2099-01-01T00:00:00Z", "sme_suitable": 1, "procurement_method": "open"}
    r = rules_engine.classify(o, cfg, {"accreditations": {"iso_27001": False}})
    assert r["classification"] == "Ineligible" and r["score"]["score"] <= 15 and r["score"]["gate"]
    good = rules_engine.classify(dict(o, description="WordPress website"), cfg, {})
    assert good["score"]["score"] > 50 and set(good["score"]["criteria"]) == set(cfg["scoring"]["weights"])
    assert good["economics"]["profit_range_gbp"][0] < good["economics"]["profit_range_gbp"][1] < 40000
    past = rules_engine.classify(dict(o, description="WordPress website", deadline="2001-01-01T00:00:00Z"), cfg, {})
    assert past["score"]["score"] == 0
    cfg["scoring"]["weights"] = {k: (1.0 if k == "time" else 0.0) for k in cfg["scoring"]["weights"]}
    assert rules_engine.classify(dict(o, description="WordPress website"), cfg, {})["score"]["score"] == 100


def test_persistence_across_restart(cfg, con):
    pipeline.collect(con, cfg, ["cf"], releases={"cf": [release("Website build", "WordPress")]})
    con.close()
    con2 = connect(cfg["db_path"])
    assert rows(con2, "SELECT count(*) n FROM opportunities")[0]["n"] == 1
    con2.close()


class FakeResp:
    def __init__(self, code, data=None):
        self.status_code, self._d, self.headers = code, data or {}, {}

    def json(self):
        return self._d


class FakeSession:
    def __init__(self, seq):
        self.seq, self.calls, self.headers = list(seq), [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        r = self.seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_connector_pagination_and_retry(cfg, monkeypatch):
    monkeypatch.setattr(connectors.time, "sleep", lambda s: None)
    s = FakeSession([FakeResp(503), FakeResp(200, {"releases": [{"ocid": "a"}], "links": {"next": "https://x/next?cursor=1"}}),
                     requests.ConnectionError(), FakeResp(200, {"releases": [{"ocid": "b"}], "links": {}})])
    from datetime import datetime
    got = [r["ocid"] for r, _ in connectors.fetch_releases("fts", "tender", datetime(2026, 1, 1), datetime(2026, 1, 2), cfg["collection"], s)]
    assert got == ["a", "b"] and s.calls[-1][0] == "https://x/next?cursor=1" and s.calls[-1][1] is None
    assert s.calls[0][1]["updatedFrom"] == "2026-01-01T00:00:00"


def test_collection_failure_keeps_partial_and_holds_watermark(cfg, con, monkeypatch):
    def boom(*a, **k):
        yield release("Website redesign", "WordPress"), 1
        raise connectors.SourceError("HTTP 500")
    monkeypatch.setattr(connectors, "fetch_releases", boom)
    cfg["collection"]["collect_awards"] = False
    res = pipeline.collect(con, cfg, ["cf"])
    assert res["cf/tender"]["status"] == "error" and res["cf/tender"]["new"] == 1
    assert get_meta(con, "watermark:cf:tender") is None  # retried from same point next run
    assert rows(con, "SELECT status FROM collection_runs")[0]["status"] == "error"


def test_normalise_strips_reference_codes_in_fingerprint():
    assert normalise.fingerprint("RA361520 - Website build", "X Council") == normalise.fingerprint("RA999 - Website build", "X Council")


def test_value_window_gates_out_of_range(cfg):
    cfg["relevance"].update(value_min=300, value_max=15000)
    base = {"title": "Website redesign and WordPress development", "description": "website development", "cpv": "72000000",
            "deadline": "2099-01-01T12:00:00Z"}
    profile = cfg["profile"]
    inside = rules_engine.classify(dict(base, value_amount=8000), cfg, profile)
    over = rules_engine.classify(dict(base, value_amount=40000), cfg, profile)
    under = rules_engine.classify(dict(base, value_amount=100), cfg, profile)
    unknown = rules_engine.classify(dict(base, value_amount=None), cfg, profile)
    assert inside["score"]["gate"] is None and unknown["score"]["gate"] is None
    assert "outside" in over["score"]["gate"] and "outside" in under["score"]["gate"]
    assert over["classification"] == "Unlikely to be suitable"
