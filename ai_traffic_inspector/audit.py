"""Page audit: what an AI search crawler would find when it fetches a URL.

Everything is fetched directly from this Mac (never through the capture proxy), one URL at a
time, no crawling. Findings report what was observed. They don't claim why a model picked or
skipped a page, which this tool can't know.
"""
from __future__ import annotations

import json
import re
import ssl
import statistics
import time
import urllib.error
import urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import bots

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover
    _SSL = ssl.create_default_context()

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
MAX_BYTES = 3_000_000
TIMEOUT = 10
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}),  # bypass the system/capture proxy
                                      urllib.request.HTTPSHandler(context=_SSL))
STOPWORDS = set("""a an the and or of for to in on at by with from is are was were be been what which who whom how
why when where does do did can could should would will vs versus about into over than that this these those it its
your you my our their his her best top near me i we""".split())


def fetch(url: str, ua: str = BROWSER_UA, max_bytes: int = MAX_BYTES) -> dict:
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("only http(s) URLs can be audited")
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                                               "Accept-Language": "en-US,en;q=0.9"})
    start = time.time()
    try:
        resp = _OPENER.open(req, timeout=TIMEOUT)
        status = resp.status
    except urllib.error.HTTPError as e:
        resp, status = e, e.code
    except Exception as e:  # DNS, TLS, timeout…
        return {"status": 0, "error": str(e)[:200], "headers": {}, "body": "", "final_url": url,
                "elapsed": time.time() - start}
    raw = resp.read(max_bytes)
    elapsed = time.time() - start
    charset = resp.headers.get_content_charset() or "utf-8"
    return {"status": status, "error": None, "headers": {k.lower(): v for k, v in resp.headers.items()},
            "body": raw.decode(charset, "replace"), "final_url": resp.geturl() if hasattr(resp, "geturl") else url,
            "elapsed": elapsed}


class _PageParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.meta, self.canonical, self.lang = "", {}, None, None
        self.headings: list[tuple[str, str]] = []
        self.jsonld: list[str] = []
        self.text: list[str] = []
        self.scripts = 0
        self.times: list[str] = []
        self._stack: list[str] = []
        self._cur_heading = None
        self._in_title = self._in_jsonld = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang")
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or a.get("itemprop") or "").lower()
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "link" and "canonical" in a.get("rel", "").lower():
            self.canonical = a.get("href")
        elif tag == "time" and a.get("datetime"):
            self.times.append(a["datetime"])
        elif tag == "title":
            self._in_title = True
        elif tag == "script":
            self.scripts += 1
            self._in_jsonld = "ld+json" in a.get("type", "")
        elif tag in ("h1", "h2", "h3"):
            self._cur_heading = [tag, ""]
        if tag not in ("meta", "link", "br", "img", "input", "hr"):
            self._stack.append(tag)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "script":
            self._in_jsonld = False
        if self._cur_heading and tag == self._cur_heading[0]:
            if self._cur_heading[1].strip():
                self.headings.append((tag, re.sub(r"\s+", " ", self._cur_heading[1]).strip()))
            self._cur_heading = None
        if tag in self._stack:
            while self._stack and self._stack.pop() != tag:
                pass

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_jsonld:
            self.jsonld.append(data)
            return
        if any(t in self.SKIP for t in self._stack):
            return
        if self._cur_heading:
            self._cur_heading[1] += data
        if data.strip():
            self.text.append(data)


def _schema_types(blocks: list[str]) -> tuple[list[str], list[str]]:
    types, dates = [], []

    def visit(o):
        if isinstance(o, dict):
            t = o.get("@type")
            for x in (t if isinstance(t, list) else [t]):
                if isinstance(x, str) and x not in types:
                    types.append(x)
            for k in ("dateModified", "datePublished"):
                if isinstance(o.get(k), str):
                    dates.append(o[k])
            for v in o.values():
                visit(v)
        elif isinstance(o, list):
            for v in o:
                visit(v)

    for b in blocks:
        try:
            visit(json.loads(b))
        except ValueError:
            continue
    return types, dates


def terms(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9'\-]+", (text or "").lower())
    return [w for w in dict.fromkeys(words) if w not in STOPWORDS and len(w) > 2]


def _coverage(query_terms: list[str], text: str) -> float | None:
    if not query_terms:
        return None
    hay = (text or "").lower()
    return round(sum(1 for t in query_terms if t in hay) / len(query_terms), 2)


def check_robots(url: str) -> dict:
    p = urlparse(url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    r = fetch(robots_url, max_bytes=500_000)
    rp = urllib.robotparser.RobotFileParser()
    if r["status"] in (401, 403):
        state = "forbidden (treated as disallow all)"
        rp.disallow_all = True
    elif r["status"] >= 400 or r["status"] == 0:
        state = f"not found ({r['status'] or r['error']}) — everything allowed"
        rp.allow_all = True
    else:
        state = "found"
        rp.parse(r["body"].splitlines())
    results = []
    for b in bots.as_dicts():
        allowed = rp.can_fetch(b["token"], url)
        results.append({**b, "allowed": allowed,
                        "note": bots.ROLE_NOTES[b["role"]].format(app=b["app"])})
    return {"url": robots_url, "state": state, "bots": results}


def audit_page(url: str, queries: list[str] | None = None, probe_bots: bool = True) -> dict:
    """Fetch one page and report what an AI crawler would see."""
    queries = [q for q in (queries or []) if q and not q.startswith("fetch:")]
    page = fetch(url)
    out = {"url": url, "final_url": page["final_url"], "status": page["status"], "error": page["error"],
           "elapsed": round(page["elapsed"], 2), "ts": time.time()}
    with ThreadPoolExecutor(4) as pool:
        robots_f = pool.submit(check_robots, page["final_url"] or url)
        p = urlparse(page["final_url"] or url)
        llms_f = pool.submit(fetch, f"{p.scheme}://{p.netloc}/llms.txt", BROWSER_UA, 200_000)
        probe_fs = {t: pool.submit(fetch, url, f"Mozilla/5.0 (compatible; {t}/1.0)", 1_000_000)
                    for t in (bots.PROBE_TOKENS if probe_bots else [])}
        robots = robots_f.result()
        llms = llms_f.result()
        probes = {t: f.result() for t, f in probe_fs.items()}

    parser = _PageParser()
    ctype = page["headers"].get("content-type", "")
    if page["body"] and ("html" in ctype or not ctype):
        try:
            parser.feed(page["body"])
        except Exception:
            pass
    text = re.sub(r"\s+", " ", " ".join(parser.text)).strip()
    words = len(text.split())
    schema, schema_dates = _schema_types(parser.jsonld)
    meta_robots = " ".join(filter(None, [parser.meta.get("robots", ""), parser.meta.get("googlebot", ""),
                                          page["headers"].get("x-robots-tag", "")])).lower()
    dates = [d for d in [parser.meta.get("article:modified_time"), parser.meta.get("article:published_time"),
                         parser.meta.get("datemodified"), parser.meta.get("datepublished")] if d] + schema_dates + parser.times
    canonical = urljoin(page["final_url"] or url, parser.canonical) if parser.canonical else None
    h1 = [h for t, h in parser.headings if t == "h1"]
    heading_text = " ".join(h for _, h in parser.headings)
    q_terms = terms(" ".join(queries))
    js_shell = bool(page["body"]) and words < 150 and (
        parser.scripts >= 3 or re.search(r'id="(root|__next|app|__nuxt)"', page["body"] or ""))

    out.update({
        "title": parser.title.strip(), "description": parser.meta.get("description") or parser.meta.get("og:description") or "",
        "h1": h1, "headings": parser.headings[:60], "word_count": words, "scripts": parser.scripts,
        "schema_types": schema, "dates": dates[:5], "canonical": canonical, "lang": parser.lang,
        "meta_robots": meta_robots, "js_shell": bool(js_shell), "robots": robots,
        "llms_txt": llms["status"] == 200 and "html" not in llms["headers"].get("content-type", ""),
        "query_terms": q_terms,
        "coverage": {"title": _coverage(q_terms, parser.title), "headings": _coverage(q_terms, heading_text + " " + parser.title),
                     "body": _coverage(q_terms, text)},
        "missing_terms": [t for t in q_terms if t not in (parser.title + " " + heading_text).lower()],
        "probes": [{"token": t, "status": r["status"], "bytes": len(r["body"]), "error": r["error"]} for t, r in probes.items()],
        "excerpt": text[:600], "html_bytes": len(page["body"]),
    })
    out["findings"] = findings(out)
    return out


def findings(a: dict) -> list[dict]:
    f = []

    def add(level, area, msg):
        f.append({"level": level, "area": area, "message": msg})

    if a["status"] == 0:
        add("block", "Fetch", f"Couldn't fetch the page: {a['error']}")
        return f
    if a["status"] >= 400:
        add("block", "Fetch", f"The page returned HTTP {a['status']} to a normal browser request.")
    for b in a["robots"]["bots"]:
        if b["allowed"]:
            continue
        if not b["verified"]:
            add("info", "robots.txt", f"Disallows {b['token']} (an unverified {b['app']} token, so not scored).")
        elif b["role"] in ("search", "fetch"):
            add("block", "robots.txt", f"Disallows {b['token']} ({b['app']}). {b['note']}")
        elif b["role"] == "control":
            add("warn", "robots.txt", f"Disallows {b['token']}. {b['note']}")
        else:
            add("info", "robots.txt", f"Disallows {b['token']} ({b['app']}). {b['note']}")
    if "noindex" in a["meta_robots"] or "none" in a["meta_robots"].split(","):
        add("block", "Indexing", f"Search crawlers are told not to index this page (robots: “{a['meta_robots']}”).")
    if "nosnippet" in a["meta_robots"] or "max-snippet:0" in a["meta_robots"]:
        add("warn", "Indexing", "nosnippet / max-snippet:0 limits the text search engines (including Google AI features) can quote.")
    if a["canonical"] and a["final_url"] and a["canonical"].rstrip("/") != a["final_url"].rstrip("/"):
        add("warn", "Indexing", f"Canonical points elsewhere ({a['canonical']}); crawlers may index that URL instead.")
    if a["js_shell"]:
        add("warn", "Content", f"Only {a['word_count']} word{'' if a['word_count'] == 1 else 's'} of text in the raw HTML and the page loads {a['scripts']} scripts. "
                               "AI retrieval fetchers generally don't run JavaScript, so they may see an almost empty page.")
    elif a["word_count"] < 300:
        add("info", "Content", f"Thin page: {a['word_count']} words of visible text in the HTML.")
    if not a["title"]:
        add("warn", "Content", "No <title>.")
    if not a["h1"]:
        add("warn", "Content", "No H1 heading.")
    if not a["description"]:
        add("info", "Content", "No meta description.")
    if not a["schema_types"]:
        add("info", "Structure", "No JSON-LD structured data (schema.org) found.")
    if not a["dates"]:
        add("info", "Freshness", "No published/updated date found in meta tags, schema or <time> elements.")
    if a["missing_terms"]:
        add("info", "Relevance", "Terms from the AI's searches missing from the title and headings: " + ", ".join(a["missing_terms"][:12]))
    if a["elapsed"] > 3:
        add("info", "Speed", f"Slow response: {a['elapsed']}s to download the HTML.")
    for p in a["probes"]:
        if p["status"] != a["status"] or (a["html_bytes"] > 2000 and p["bytes"] < 0.3 * a["html_bytes"]):
            add("warn", "Bot access (indicative)",
                f"Request with the {p['token']} user-agent got HTTP {p['status'] or p['error']} vs {a['status']} for a browser. "
                "CDNs check real crawlers by IP, so this is only a hint that bots may be treated differently.")
    add("info", "llms.txt", ("Has /llms.txt." if a["llms_txt"] else "No /llms.txt.") +
        " (Low evidence that this affects AI answers either way.)")
    order = {"block": 0, "warn": 1, "info": 2}
    return sorted(f, key=lambda x: order[x["level"]])


def _median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def compare_audits(mine: dict | None, others: list[dict]) -> list[str]:
    """Observed differences between the user's page and pages that were cited."""
    ok = [o for o in others if o.get("status") and o["status"] < 400]
    if not mine or not ok:
        return []
    out = []
    n = len(ok)
    med_words = _median([o["word_count"] for o in ok])
    if med_words and mine["word_count"] < 0.5 * med_words:
        out.append(f"Cited pages have a median of {int(med_words):,} words of HTML text; yours has {mine['word_count']:,}.")
    with_schema = [o for o in ok if o["schema_types"]]
    if with_schema and not mine["schema_types"]:
        kinds = sorted({t for o in with_schema for t in o["schema_types"]})[:6]
        out.append(f"{len(with_schema)} of {n} cited pages use JSON-LD ({', '.join(kinds)}); yours has none.")
    with_dates = [o for o in ok if o["dates"]]
    if with_dates and not mine["dates"]:
        out.append(f"{len(with_dates)} of {n} cited pages show a published/updated date; yours doesn't.")
    cov = _median([o["coverage"]["headings"] for o in ok])
    if cov is not None and mine["coverage"]["headings"] is not None and mine["coverage"]["headings"] + 0.2 < cov:
        out.append(f"Cited pages' titles/headings contain {int(cov * 100)}% of the AI's search terms (median); "
                   f"yours contain {int(mine['coverage']['headings'] * 100)}%.")
    if mine["js_shell"] and not any(o["js_shell"] for o in ok):
        out.append("Your page's text appears to need JavaScript; none of the cited pages do.")
    med_h = _median([len(o["headings"]) for o in ok])
    if med_h and len(mine["headings"]) < 0.5 * med_h:
        out.append(f"Cited pages have a median of {int(med_h)} H1–H3 headings; yours has {len(mine['headings'])}.")
    blocked = [f for f in mine["findings"] if f["level"] == "block"]
    if blocked:
        out.insert(0, f"Your page has {len(blocked)} blocking issue(s) that cited pages don't share — fix those first.")
    return out


def run_audit(url: str | None, queries: list[str], compare_urls: list[str]) -> dict:
    urls = ([url] if url else []) + [u for u in compare_urls if u and u != url][:5]
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(lambda u: audit_page(u, queries), urls))
    mine = results[0] if url else None
    others = results[1:] if url else results
    return {"mine": mine, "others": others, "queries": queries, "differences": compare_audits(mine, others)}
