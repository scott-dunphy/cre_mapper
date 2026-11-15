"""SQLite storage shared by the capture addon (writer) and the dashboard (reader)."""
from __future__ import annotations

import json
import os
import sqlite3

DATA_DIR = os.path.expanduser(os.environ.get("AITI_HOME", "~/.ai-traffic-inspector"))
DEFAULT_DB = os.path.join(DATA_DIR, "traffic.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS flows (
    id INTEGER PRIMARY KEY,
    ts REAL, app TEXT, client TEXT, mode TEXT,
    method TEXT, url TEXT, host TEXT, path TEXT, status INTEGER,
    req_ct TEXT, resp_ct TEXT, req_headers TEXT, resp_headers TEXT,
    req_body BLOB, resp_body BLOB, req_size INTEGER, resp_size INTEGER,
    ttfb REAL, ttft REAL, duration REAL, is_chat INTEGER DEFAULT 0, error TEXT
);
CREATE INDEX IF NOT EXISTS flows_app ON flows(app, ts);
CREATE TABLE IF NOT EXISTS exchanges (
    id INTEGER PRIMARY KEY,
    flow_id INTEGER, ts REAL, app TEXT, client TEXT, model TEXT,
    prompt TEXT, prompt_norm TEXT, response_text TEXT,
    search_queries TEXT, sources TEXT, n_sources INTEGER, n_domains INTEGER,
    ttft REAL, duration REAL, parser TEXT
);
CREATE INDEX IF NOT EXISTS exchanges_ts ON exchanges(ts);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS audits (id INTEGER PRIMARY KEY, ts REAL, url TEXT, result TEXT);
"""

REDACT = {"authorization", "cookie", "set-cookie", "x-api-key", "proxy-authorization", "openai-sentinel-token",
          "openai-sentinel-proof-token", "x-csrf-token", "x-xsrf-token"}


def connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=15, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def headers_json(headers) -> str:
    return json.dumps([[k, "[redacted]" if k.lower() in REDACT else v] for k, v in headers])


def insert_flow(con, **row) -> int:
    cols = ", ".join(row)
    cur = con.execute(f"INSERT INTO flows ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    con.commit()
    return cur.lastrowid


def insert_exchange(con, flow_id: int, ts: float, app: str, client: str, ttft, duration, analysis: dict) -> int:
    from providers import normalize_prompt
    sources = analysis["sources"]
    cur = con.execute(
        """INSERT INTO exchanges (flow_id, ts, app, client, model, prompt, prompt_norm, response_text,
           search_queries, sources, n_sources, n_domains, ttft, duration, parser)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (flow_id, ts, app, client, analysis["model"], analysis["prompt"], normalize_prompt(analysis["prompt"]),
         analysis["response_text"], json.dumps(analysis["search_queries"]), json.dumps(sources),
         len(sources), len({s["domain"] for s in sources}), ttft, duration, analysis["parser"]))
    con.commit()
    return cur.lastrowid


def get_setting(con, key, default=None):
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_setting(con, key, value) -> None:
    con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
    con.commit()
