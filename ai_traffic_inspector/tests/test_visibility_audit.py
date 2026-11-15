import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audit  # noqa: E402
import visibility  # noqa: E402

ROBOTS = """User-agent: GPTBot
Disallow: /

User-agent: Claude-SearchBot
Disallow: /private/

User-agent: *
Allow: /
"""

GOOD = """<!doctype html><html lang="en"><head><title>Chicago Industrial Vacancy Q3 2026</title>
<meta name="description" content="Vacancy and rents"><link rel="canonical" href="/good">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Article","dateModified":"2026-10-01"}</script>
</head><body><h1>Chicago industrial vacancy</h1><h2>Vacancy rate</h2><p>""" + "Industrial vacancy in Chicago rose. " * 120 + """</p>
<h2>Rents</h2><p>Rents were flat.</p></body></html>"""

SPA = """<!doctype html><html><head><title>App</title><script src="/a.js"></script><script src="/b.js"></script>
<script src="/c.js"></script></head><body><div id="root"></div></body></html>"""


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        ua = self.headers.get("User-Agent", "")
        routes = {"/robots.txt": (200, "text/plain", ROBOTS), "/good": (200, "text/html", GOOD),
                  "/spa": (200, "text/html", SPA),
                  "/private/x": (200, "text/html", GOOD.replace('href="/good"', 'href="/private/x"'))}
        code, ctype, body = routes.get(self.path, (404, "text/plain", "nope"))
        if "PerplexityBot" in ua and self.path == "/good":
            code, body = 403, "blocked"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode())


@pytest.fixture(scope="module")
def site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _bot(a, token):
    return next(b for b in a["robots"]["bots"] if b["token"] == token)


def test_training_vs_search_split(site):
    a = audit.audit_page(site + "/good", ["chicago industrial vacancy rate"])
    assert a["status"] == 200
    assert _bot(a, "GPTBot")["allowed"] is False          # training blocked
    assert _bot(a, "OAI-SearchBot")["allowed"] is True     # search still allowed
    assert _bot(a, "Claude-SearchBot")["allowed"] is True  # only /private/ blocked
    blocks = [f for f in a["findings"] if f["level"] == "block"]
    assert not blocks, blocks                              # a training-only block is not a blocker
    gpt = [f for f in a["findings"] if "GPTBot" in f["message"]]
    assert gpt and gpt[0]["level"] == "info"
    assert a["schema_types"] == ["Article"] and a["dates"]
    assert a["coverage"]["headings"] == 1.0
    assert any("PerplexityBot" in f["message"] and f["level"] == "warn" for f in a["findings"])  # indicative probe


def test_search_bot_block_is_flagged(site):
    a = audit.audit_page(site + "/private/x", [], probe_bots=False)
    assert _bot(a, "Claude-SearchBot")["allowed"] is False
    assert any(f["level"] == "block" and "Claude-SearchBot" in f["message"] for f in a["findings"])


def test_js_shell_flagged(site):
    a = audit.audit_page(site + "/spa", ["chicago industrial vacancy"], probe_bots=False)
    assert a["js_shell"] is True
    assert any(f["area"] == "Content" and "JavaScript" in f["message"] for f in a["findings"])


def test_compare_differences(site):
    r = audit.run_audit(site + "/spa", ["chicago industrial vacancy"], [site + "/good"])
    text = " ".join(r["differences"])
    assert "JSON-LD" in text and "JavaScript" in text and "words" in text


def test_non_http_rejected():
    with pytest.raises(ValueError):
        audit.fetch("file:///etc/passwd")


def ex(i, app, prompt, domains, queries=()):
    return {"id": i, "ts": 1000 + i, "app": app, "prompt": prompt, "prompt_norm": prompt.lower(),
            "sources": [{"domain": d, "url": f"https://{d}/p", "title": None} for d in domains],
            "search_queries": list(queries)}


def test_visibility_math():
    exs = [
        ex(1, "Claude", "chicago vacancy", ["cbre.com", "blog.mysite.com"], ["chicago industrial vacancy"]),
        ex(2, "ChatGPT", "chicago vacancy", ["cbre.com", "colliers.com"], ["chicago vacancy q3"]),
        ex(3, "ChatGPT", "dallas rents", ["mysite.com", "cbre.com"]),
        ex(4, "Grok", "dallas rents", []),
    ]
    v = visibility.compute(exs, ["mysite.com"], ["cbre.com"])
    mine = next(b for b in v["board"] if b["domain"] == "mysite.com")
    assert mine["cells"]["Claude"] == {"cited": 1, "total": 1, "with_sources": 1, "share": 1.0, "avg_rank": 2.0}
    assert mine["cells"]["ChatGPT"]["cited"] == 1 and mine["cells"]["ChatGPT"]["share"] == 0.5
    assert mine["cells"]["ChatGPT"]["avg_rank"] == 1.0
    assert {m["id"] for m in v["missed"]} == {2, 4}
    assert next(m for m in v["missed"] if m["id"] == 4)["no_sources"] is True
    assert dict(v["competitor_wins"]) == {"cbre.com": 1}
    assert ("chicago vacancy q3", 1) in v["missed_searches"]
    gap = next(g for g in v["gaps"] if g["prompt"] == "chicago vacancy")
    assert gap["cited_by"] == ["Claude"] and gap["missed_by"] == ["ChatGPT"]
    assert gap["my_url"] == "https://blog.mysite.com/p"


def test_clean_domains():
    assert visibility.clean_domains(["https://www.MySite.com/page", "", "x", "mysite.com"]) == ["mysite.com"]


def test_rfc9309_matching():
    r = audit.Robots("User-agent: *\nDisallow: /\nAllow: /blog/\n")
    assert r.allowed("X", "http://a/blog/x") and not r.allowed("X", "http://a/other")
    r = audit.Robots("User-agent: *\nDisallow: /*.pdf$\n")
    assert not r.allowed("X", "http://a/f.pdf") and r.allowed("X", "http://a/f.pdf?x=1") and r.allowed("X", "http://a/f.html")
    r = audit.Robots("User-agent: *\nDisallow: /page\nAllow: /page\n")   # tie -> allow
    assert r.allowed("X", "http://a/page")
    r = audit.Robots("User-agent: Claude\nDisallow: /\n")                 # exact token only
    assert r.allowed("Claude-SearchBot", "http://a/x")
    r = audit.Robots("User-agent: GPTBot\nUser-agent: ClaudeBot\nDisallow: /\n\nUser-agent: *\nDisallow: /tmp/\n"
                     "User-agent: gptbot\nAllow: /public/\n")             # merged duplicate groups, case-insensitive
    assert not r.allowed("ClaudeBot", "http://a/x") and r.allowed("GPTBot", "http://a/public/a")
    assert r.allowed("OAI-SearchBot", "http://a/x") and not r.allowed("OAI-SearchBot", "http://a/tmp/y")


class Odd(BaseHTTPRequestHandler):
    robots_status = 200

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/robots.txt":
            self.send_response(type(self).robots_status)
            self.end_headers()
            self.wfile.write(b"User-agent: *\nDisallow: /\n")
            return
        self.send_response(403)
        self.send_header("cf-mitigated", "challenge")
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><head><title>Just a moment...</title></head><body>checking</body></html>")


@pytest.mark.parametrize("code,expect", [(403, True), (503, None)])
def test_robots_status_codes(code, expect):
    handler = type("H", (Odd,), {"robots_status": code})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        a = audit.audit_page(f"http://127.0.0.1:{srv.server_address[1]}/p", [], probe_bots=False)
    finally:
        srv.shutdown()
    assert _bot(a, "OAI-SearchBot")["allowed"] is expect
    assert a["challenge"] is True
    areas = {(f["level"], f["area"]) for f in a["findings"]}
    assert ("warn", "Bot protection") in areas
    assert not any(f["area"] == "Content" for f in a["findings"])   # don't audit the challenge page's content
    assert not any(f["area"] == "robots.txt" and f["level"] == "block" for f in a["findings"])
    if code == 503:
        assert ("warn", "robots.txt") in areas


def test_gap_carries_missing_apps_searches():
    exs = [ex(1, "Claude", "q", ["mysite.com"], ["a"]), ex(2, "ChatGPT", "q", ["x.com"], ["b", "c"])]
    gap = visibility.compute(exs, ["mysite.com"], [])["gaps"][0]
    assert gap["missed_searches"] == ["b", "c"]
    assert gap["apps"]["Claude"]["searches"] == ["a"]


def test_term_coverage_word_boundaries():
    assert audit._coverage(["rate"], "corporate offices") == 0.0
    assert audit._coverage(["rate"], "the vacancy rate rose") == 1.0
