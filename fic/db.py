"""SQLite persistence. One file, WAL mode, survives restarts."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS opportunities(
  id INTEGER PRIMARY KEY,
  ocid TEXT UNIQUE NOT NULL,
  source TEXT, notice_id TEXT,
  title TEXT, description TEXT, requirements_text TEXT,
  buyer TEXT, buyer_id TEXT, region TEXT,
  cpv TEXT, cpv_all TEXT,
  value_amount REAL, value_currency TEXT,
  deadline TEXT, published TEXT, updated TEXT,
  procurement_method TEXT, sme_suitable INTEGER,
  url TEXT, fingerprint TEXT, raw_hash TEXT,
  category TEXT, relevance REAL, relevance_terms TEXT,
  eligibility_status TEXT, eligibility_json TEXT,
  classification TEXT, score REAL, score_json TEXT,
  ai_classification TEXT,
  status TEXT DEFAULT 'new',
  duplicate_of INTEGER,
  first_seen TEXT, last_seen TEXT
);
CREATE INDEX IF NOT EXISTS ix_opp_fp ON opportunities(fingerprint);
CREATE INDEX IF NOT EXISTS ix_opp_status ON opportunities(status);

CREATE TABLE IF NOT EXISTS awards(
  id INTEGER PRIMARY KEY,
  award_key TEXT UNIQUE NOT NULL,
  ocid TEXT, source TEXT, title TEXT, buyer TEXT, buyer_id TEXT,
  supplier TEXT, supplier_id TEXT, value REAL, award_date TEXT,
  contract_start TEXT, contract_end TEXT, cpv TEXT, url TEXT,
  relevant INTEGER DEFAULT 0, first_seen TEXT
);
CREATE INDEX IF NOT EXISTS ix_awards_buyer ON awards(buyer);

CREATE TABLE IF NOT EXISTS collection_runs(
  id INTEGER PRIMARY KEY, source TEXT, stage TEXT, started TEXT, finished TEXT,
  fetched INTEGER DEFAULT 0, new INTEGER DEFAULT 0, updated INTEGER DEFAULT 0,
  duplicates INTEGER DEFAULT 0, queued INTEGER DEFAULT 0, errors TEXT, status TEXT, watermark TEXT
);

CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY, kind TEXT, agent TEXT, opp_id INTEGER, category TEXT,
  status TEXT DEFAULT 'queued', priority REAL DEFAULT 0, depth INTEGER DEFAULT 0,
  payload TEXT, dedupe_key TEXT, created TEXT, started TEXT, finished TEXT,
  attempts INTEGER DEFAULT 0, error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_task_dedupe ON tasks(dedupe_key) WHERE status IN ('queued','in_progress');

CREATE TABLE IF NOT EXISTS agent_runs(
  id INTEGER PRIMARY KEY, task_id INTEGER, agent TEXT, opp_id INTEGER,
  output TEXT, decision TEXT, qc_passed INTEGER, qc_issues TEXT,
  est_tokens INTEGER, reviewed INTEGER DEFAULT 0, created TEXT
);

CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY, opp_id INTEGER, agent_run_id INTEGER,
  label_classification TEXT, note TEXT, source TEXT DEFAULT 'human', created TEXT
);

CREATE TABLE IF NOT EXISTS errors(
  id INTEGER PRIMARY KEY, agent TEXT, agent_run_id INTEGER, opp_id INTEGER,
  kind TEXT, detail TEXT, detected_by TEXT, rule_id INTEGER, created TEXT
);

CREATE TABLE IF NOT EXISTS rules(
  id INTEGER PRIMARY KEY, rule_key TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  parent_id INTEGER, agent TEXT, kind TEXT, spec TEXT, rationale TEXT, risk TEXT,
  status TEXT, created_by TEXT, created TEXT, decided_at TEXT, decided_by TEXT,
  test_result TEXT, evidence TEXT
);

CREATE TABLE IF NOT EXISTS rule_events(
  id INTEGER PRIMARY KEY, rule_id INTEGER, event TEXT, detail TEXT, actor TEXT, created TEXT
);

CREATE TABLE IF NOT EXISTS proposals(
  id INTEGER PRIMARY KEY, opp_id INTEGER, agent_run_id INTEGER, title TEXT, body_md TEXT,
  missing_info TEXT, verify TEXT, status TEXT DEFAULT 'draft', created TEXT, updated TEXT
);

CREATE TABLE IF NOT EXISTS outcomes(
  id INTEGER PRIMARY KEY, opp_id INTEGER UNIQUE, submitted INTEGER DEFAULT 0, submitted_at TEXT,
  won INTEGER, contract_value REAL, revenue_received REAL, est_cost REAL, actual_cost REAL,
  note TEXT, updated TEXT
);

CREATE TABLE IF NOT EXISTS prospects(
  id INTEGER PRIMARY KEY, name TEXT, website TEXT, sector TEXT, source_url TEXT, notes TEXT,
  subscriber_type TEXT DEFAULT 'unknown', consent TEXT DEFAULT 'none', status TEXT DEFAULT 'research',
  created TEXT
);

CREATE TABLE IF NOT EXISTS budget_ledger(
  id INTEGER PRIMARY KEY, day TEXT, category TEXT, task_id INTEGER, units INTEGER DEFAULT 1, created TEXT
);

CREATE TABLE IF NOT EXISTS activity(
  id INTEGER PRIMARY KEY, actor TEXT, action TEXT, detail TEXT, created TEXT
);
"""


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


def log(con, actor, action, detail=""):
    con.execute("INSERT INTO activity(actor,action,detail,created) VALUES(?,?,?,?)",
                (actor, action, detail if isinstance(detail, str) else json.dumps(detail), now()))


def rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def one(con, sql, args=()):
    r = con.execute(sql, args).fetchone()
    return dict(r) if r else None


def get_meta(con, key, default=None):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_meta(con, key, value):
    con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
