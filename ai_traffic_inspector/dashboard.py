"""Local web dashboard (127.0.0.1 only) that reads the capture database."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import visibility  # noqa: E402

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _exchange(row, full=False) -> dict:
    d = dict(row)
    d["sources"] = json.loads(d["sources"] or "[]")
    d["search_queries"] = json.loads(d["search_queries"] or "[]")
    d["domains"] = sorted({s["domain"] for s in d["sources"]})
    if not full:
        d["response_preview"] = (d.pop("response_text") or "")[:300]
        d.pop("sources")
    return d


def _body_text(blob, ct: str):
    if blob is None:
        return None
    text = blob.decode("utf-8", "replace") if isinstance(blob, bytes) else str(blob)
    if "json" in (ct or ""):
        try:
            return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    return text


def compare(con, ids: list[int]) -> dict:
    rows = {r["id"]: _exchange(r, full=True) for r in
            con.execute(f"SELECT * FROM exchanges WHERE id IN ({','.join('?' * len(ids))})", ids)}
    exs = [rows[i] for i in ids if i in rows]
    dom_sets = [set(e["domains"]) for e in exs]
    url_sets = [{s["url"] for s in e["sources"]} for e in exs]
    all_domains: dict[str, list] = {}
    for i, e in enumerate(exs):
        for s in e["sources"]:
            cell = all_domains.setdefault(s["domain"], [[] for _ in exs])[i]
            cell.append({"url": s["url"], "title": s["title"]})
    matrix = sorted(({"domain": d, "cells": cells, "count": sum(1 for c in cells if c)} for d, cells in all_domains.items()),
                    key=lambda r: (-r["count"], r["domain"]))

    def jac(a, b):
        return round(len(a & b) / len(a | b), 3) if a | b else None

    sim = [[jac(dom_sets[i], dom_sets[j]) for j in range(len(exs))] for i in range(len(exs))]
    url_sim = [[jac(url_sets[i], url_sets[j]) for j in range(len(exs))] for i in range(len(exs))]
    shared = set.intersection(*dom_sets) if dom_sets else set()
    return {"exchanges": exs, "matrix": matrix, "domain_similarity": sim, "url_similarity": url_sim,
            "shared_domains": sorted(shared),
            "unique_domains": [sorted(s - set().union(*(dom_sets[:i] + dom_sets[i + 1:]))) for i, s in enumerate(dom_sets)]}


class Handler(BaseHTTPRequestHandler):
    db_path = store.DEFAULT_DB
    on_shutdown = None
    status = {}
    asker = None

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else (json.dumps(body, default=str) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _local_only(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._local_only():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        con = store.connect(self.db_path)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html")
            if p == "/api/status":
                last = con.execute("SELECT (SELECT max(id) FROM flows), (SELECT max(id) FROM exchanges), "
                                   "(SELECT count(*) FROM flows), (SELECT count(*) FROM exchanges)").fetchone()
                return self._send(200, {"last_flow": last[0], "last_exchange": last[1], "flows": last[2],
                                        "exchanges": last[3], **self.status})
            if p == "/api/apps":
                rows = con.execute("SELECT app, count(*) n FROM flows GROUP BY app ORDER BY n DESC").fetchall()
                return self._send(200, [dict(r) for r in rows])
            if p == "/api/exchanges":
                sql, args = "SELECT * FROM exchanges WHERE 1=1", []
                if q.get("app"):
                    sql += " AND app=?"
                    args.append(q["app"])
                if q.get("q"):
                    sql += " AND (prompt LIKE ? OR response_text LIKE ? OR sources LIKE ?)"
                    args += [f"%{q['q']}%"] * 3
                sql += " ORDER BY ts DESC LIMIT ?"
                args.append(int(q.get("limit", 300)))
                return self._send(200, [_exchange(r) for r in con.execute(sql, args)])
            m = re.fullmatch(r"/api/exchanges/(\d+)", p)
            if m:
                row = con.execute("SELECT * FROM exchanges WHERE id=?", (int(m[1]),)).fetchone()
                return self._send(200, _exchange(row, True)) if row else self._send(404, {"error": "not found"})
            if p == "/api/groups":
                rows = con.execute("SELECT id, ts, app, prompt, prompt_norm FROM exchanges ORDER BY ts DESC LIMIT 600").fetchall()
                groups = [{**g, "apps": sorted(g["apps"])} for g in visibility.group_similar(rows) if len(g["ids"]) > 1]
                groups.sort(key=lambda g: (-len(g["apps"]), -g["ts"]))
                return self._send(200, groups)
            if p == "/api/ask":
                if not self.asker or not self.status.get("capturing"):
                    return self._send(200, {"available": False, "reason": "Start capture to use Ask all."})
                return self._send(200, {"available": True, **self.asker.info()})
            m = re.fullmatch(r"/api/runs/(\d+)", p)
            if m:
                run = self.asker.runs.get(int(m[1])) if self.asker else None
                if not run:
                    return self._send(404, {"error": "not found"})
                clean = {**run, "apps": {a: {k: v for k, v in st.items() if not k.startswith("_")}
                                         for a, st in run["apps"].items()}}
                return self._send(200, clean)
            if p == "/api/settings":
                return self._send(200, {"my_domains": store.get_setting(con, "my_domains", []),
                                        "competitors": store.get_setting(con, "competitors", [])})
            if p == "/api/visibility":
                exs = [_exchange(r, True) for r in con.execute(
                    "SELECT * FROM exchanges ORDER BY ts DESC LIMIT ?", (int(q.get("limit", 2000)),))]
                if q.get("app"):
                    exs = [e for e in exs if e["app"] == q["app"]]
                return self._send(200, visibility.compute(exs, store.get_setting(con, "my_domains", []),
                                                          store.get_setting(con, "competitors", [])))
            if p == "/api/audits":
                rows = con.execute("SELECT id, ts, url FROM audits ORDER BY id DESC LIMIT 30").fetchall()
                return self._send(200, [dict(r) for r in rows])
            m = re.fullmatch(r"/api/audits/(\d+)", p)
            if m:
                r = con.execute("SELECT result FROM audits WHERE id=?", (int(m[1]),)).fetchone()
                return self._send(200, json.loads(r[0])) if r else self._send(404, {"error": "not found"})
            if p == "/api/compare":
                ids = [int(x) for x in q.get("ids", "").split(",") if x.strip().isdigit()]
                return self._send(200, compare(con, ids))
            if p == "/api/export.csv":
                ids = [int(x) for x in q.get("ids", "").split(",") if x.strip().isdigit()]
                sql = "SELECT * FROM exchanges" + (f" WHERE id IN ({','.join('?' * len(ids))})" if ids else "") + " ORDER BY ts"
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["exchange_id", "time", "app", "client", "model", "prompt", "search_queries", "rank", "domain", "url", "title"])
                for r in con.execute(sql, ids):
                    e = _exchange(r, True)
                    for rank, s in enumerate(e["sources"] or [{"domain": "", "url": "", "title": ""}], 1):
                        w.writerow([e["id"], e["ts"], e["app"], e["client"], e["model"], e["prompt"],
                                    " | ".join(e["search_queries"]), rank if s["url"] else "", s["domain"], s["url"], s["title"] or ""])
                return self._send(200, buf.getvalue(), "text/csv",
                                  {"Content-Disposition": "attachment; filename=ai-sources.csv"})
            if p == "/api/hosts":
                sql = ("SELECT app, host, count(*) n, sum(req_size) up, sum(resp_size) down, max(ts) last, "
                       "sum(is_chat) chats FROM flows" + (" WHERE app=?" if q.get("app") else "") +
                       " GROUP BY app, host ORDER BY n DESC")
                return self._send(200, [dict(r) for r in con.execute(sql, [q["app"]] if q.get("app") else [])])
            if p == "/api/flows":
                sql, args = ("SELECT id, ts, app, client, method, host, path, status, resp_ct, req_size, resp_size, "
                             "duration, is_chat, error FROM flows WHERE 1=1"), []
                for key in ("app", "host"):
                    if q.get(key):
                        sql += f" AND {key}=?"
                        args.append(q[key])
                if q.get("chat") == "1":
                    sql += " AND is_chat=1"
                if q.get("q"):
                    sql += " AND (url LIKE ? OR CAST(req_body AS TEXT) LIKE ? OR CAST(resp_body AS TEXT) LIKE ?)"
                    args += [f"%{q['q']}%"] * 3
                sql += " ORDER BY id DESC LIMIT ?"
                args.append(int(q.get("limit", 400)))
                return self._send(200, [dict(r) for r in con.execute(sql, args)])
            m = re.fullmatch(r"/api/flows/(\d+)", p)
            if m:
                r = con.execute("SELECT * FROM flows WHERE id=?", (int(m[1]),)).fetchone()
                if not r:
                    return self._send(404, {"error": "not found"})
                d = dict(r)
                d["req_headers"] = json.loads(d["req_headers"] or "[]")
                d["resp_headers"] = json.loads(d["resp_headers"] or "[]")
                d["req_body"] = _body_text(d["req_body"], d["req_ct"])
                d["resp_body"] = _body_text(d["resp_body"], d["resp_ct"])
                ex = con.execute("SELECT id FROM exchanges WHERE flow_id=?", (d["id"],)).fetchone()
                d["exchange_id"] = ex[0] if ex else None
                return self._send(200, d)
            return self._send(404, {"error": "not found"})
        except Exception as exc:  # surface errors to the UI instead of hanging
            return self._send(500, {"error": str(exc)})
        finally:
            con.close()

    def do_POST(self):
        if not self._local_only() or self.headers.get("X-AITI") != "1":
            return self._send(403, {"error": "forbidden"})
        p = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length > 100_000:
            return self._send(413, {"error": "too large"})
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if p in ("/api/ask", "/api/ask/signin"):
            if not self.asker or not self.status.get("capturing"):
                return self._send(400, {"error": "Start capture to use Ask all."})
            try:
                if p == "/api/ask/signin":
                    self.asker.open_signin(data.get("apps") or None)
                    return self._send(200, {"ok": True})
                return self._send(200, {"run_id": self.asker.start_run(str(data.get("prompt", "")), list(data.get("apps") or []))})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
        if p == "/api/settings":
            con = store.connect(self.db_path)
            store.set_setting(con, "my_domains", visibility.clean_domains(data.get("my_domains")))
            store.set_setting(con, "competitors", visibility.clean_domains(data.get("competitors")))
            con.close()
            return self._send(200, {"ok": True})
        if p == "/api/audit":
            import audit
            url = (data.get("url") or "").strip() or None
            compare_urls = [u.strip() for u in data.get("compare") or [] if isinstance(u, str) and u.strip()]
            if not url and not compare_urls:
                return self._send(400, {"error": "give a URL to audit"})
            try:
                result = audit.run_audit(url, [str(x) for x in data.get("queries") or []][:20], compare_urls)
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            con = store.connect(self.db_path)
            cur = con.execute("INSERT INTO audits (ts, url, result) VALUES (?,?,?)",
                              (time.time(), url or compare_urls[0], json.dumps(result)))
            con.commit()
            con.close()
            result["id"] = cur.lastrowid
            return self._send(200, result)
        if p == "/api/clear":
            con = store.connect(self.db_path)
            con.execute("DELETE FROM exchanges")
            con.execute("DELETE FROM flows")
            con.commit()
            con.close()
            return self._send(200, {"ok": True})
        if p == "/api/shutdown" and self.on_shutdown:
            self._send(200, {"ok": True})
            threading.Thread(target=self.on_shutdown, daemon=True).start()
            return
        return self._send(404, {"error": "not found"})


def serve(db_path=store.DEFAULT_DB, port=8765, on_shutdown=None, status=None, asker=None) -> ThreadingHTTPServer:
    store.connect(db_path).close()
    Handler.db_path = db_path
    Handler.on_shutdown = on_shutdown
    Handler.asker = asker
    Handler.status = status or {"capturing": False}
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    import time
    serve(sys.argv[1] if len(sys.argv) > 1 else store.DEFAULT_DB)
    print("Dashboard on http://127.0.0.1:8765")
    while True:
        time.sleep(3600)
