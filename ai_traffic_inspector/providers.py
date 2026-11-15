"""Which hosts belong to which AI app, and how to read their chat traffic.

Every function here is pure (no mitmproxy import) so it can be unit tested and
re-run over stored flows. The vendors change their private web APIs often, so
each app has a specific parser plus a generic fallback that still pulls out the
prompt, the answer text, any search queries and every cited URL.

To add or override an app without editing this file, create
`apps.local.json` next to it:

    [{"name": "MyBot", "hosts": ["mybot.ai"], "chat": "/api/chat$", "parser": "generic"}]
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlparse


@dataclass
class App:
    name: str
    hosts: tuple[str, ...]
    chat: str  # regex matched against the request path (without query string)
    parser: str = "generic"

    def matches_host(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def is_chat(self, path: str) -> bool:
        return bool(re.search(self.chat, path.split("?", 1)[0]))


APPS: list[App] = [
    App("Claude", ("claude.ai", "anthropic.com", "claudeusercontent.com"),
        r"/(retry_)?completion$|/v1/messages$", "claude"),
    App("ChatGPT", ("chatgpt.com", "chat.openai.com", "openai.com", "oaistatic.com", "oaiusercontent.com"),
        r"/backend-(api|anon)/(f/)?conversation$|/v1/(chat/completions|responses)$", "chatgpt"),
    App("Grok", ("grok.com", "x.ai"),
        r"/rest/app-chat/conversations/(new|[^/]+/responses)$|/v1/(chat/completions|responses|messages)$", "grok"),
    # Meta AI is where Meta's Muse models are served.
    App("Meta AI (Muse)", ("meta.ai",), r"/api/graphql", "generic"),
    App("Gemini", ("gemini.google.com",), r"StreamGenerate", "generic"),
    App("Perplexity", ("perplexity.ai",), r"/rest/sse/perplexity_ask", "generic"),
    App("Copilot", ("copilot.microsoft.com",), r"/c/api/(chat|conversations)", "generic"),
]


def _load_local_apps() -> None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apps.local.json")
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for spec in json.load(fh):
            app = App(spec["name"], tuple(spec["hosts"]), spec.get("chat", r"^$"), spec.get("parser", "generic"))
            APPS[:] = [a for a in APPS if a.name != app.name]
            APPS.insert(0, app)


_load_local_apps()

# Domains that are part of the apps themselves (or CDNs / favicon services) and
# therefore never count as a "source" the model cited.
OWN_DOMAINS = {h for a in APPS for h in a.hosts} | {
    "fbcdn.net", "twimg.com", "gstatic.com", "googleusercontent.com", "googleapis.com",
    "w3.org", "sentry.io", "statsig.com", "featuregates.org", "intercom.io", "segment.io",
    "cloudflare.com", "cloudfront.net", "pplx.ai", "imgix.net", "bing.net",
}
_ASSET_EXT = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|woff2?|ttf|css|js|map)$", re.I)
URL_RE = re.compile(r"https?://[^\s\"'<>\\)\]}|`]+")


def all_host_regex() -> str:
    """Regex for mitmproxy's allow_hosts: only these hosts get TLS-intercepted."""
    hosts = sorted({h for a in APPS for h in a.hosts})
    return r"(^|\.)(" + "|".join(re.escape(h) for h in hosts) + r")(:\d+)?$"


def identify_app(host: str) -> App | None:
    for app in APPS:
        if app.matches_host(host):
            return app
    return None


def detect_client(user_agent: str) -> str:
    ua = user_agent or ""
    if "Electron" in ua and "Claude" in ua:
        return "Claude Desktop"
    for name in ("ChatGPT", "Grok", "Perplexity", "Claude"):
        if re.search(rf"\b{name}/\d", ua):
            return f"{name} app"
    if "Electron" in ua:
        return "Electron app"
    if "Edg/" in ua:
        return "Edge"
    if "Firefox/" in ua:
        return "Firefox"
    if "Chrome/" in ua:
        return "Chrome"
    if "Safari/" in ua and "Version/" in ua:
        return "Safari"
    return ua.split(" ", 1)[0][:40] if ua else "unknown"


# --------------------------------------------------------------------------
# Body decoding helpers
# --------------------------------------------------------------------------

def parse_request_body(body: bytes, content_type: str):
    if not body:
        return None
    text = body.decode("utf-8", "replace")
    if "x-www-form-urlencoded" in (content_type or ""):
        out = {}
        for k, v in parse_qsl(text, keep_blank_values=True):
            try:
                out[k] = json.loads(v)
            except ValueError:
                out[k] = v
        return out
    try:
        return json.loads(text)
    except ValueError:
        return text


def iter_events(text: str):
    """Yield JSON objects from a JSON, SSE, NDJSON or Gemini-style body."""
    text = (text or "").strip()
    if text.startswith(")]}'"):
        text = text[4:].strip()
    if not text:
        return
    try:
        yield json.loads(text)
        return
    except ValueError:
        pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]" or line[0] not in "{[":
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def walk(obj, path=(), depth=0):
    """Yield (path, leaf) for every scalar, decoding JSON nested inside strings."""
    if depth > 60:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, path + (str(k),), depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, path, depth + 1)
    elif isinstance(obj, str):
        s = obj.strip()
        if len(s) > 1 and s[0] in "[{" and s[-1] in "]}":
            try:
                yield from walk(json.loads(s), path, depth + 1)
                return
            except ValueError:
                pass
        yield path, obj
    else:
        yield path, obj


def _dicts(obj, depth=0):
    """Yield every dict in a structure (including JSON nested in strings)."""
    if depth > 60:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _dicts(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _dicts(v, depth + 1)
    elif isinstance(obj, str):
        s = obj.strip()
        if len(s) > 1 and s[0] in "[{" and s[-1] in "]}":
            try:
                yield from _dicts(json.loads(s), depth + 1)
            except ValueError:
                pass


def normalize_prompt(p: str) -> str:
    p = re.sub(r"\s+", " ", (p or "").lower()).strip()
    return p.strip(" ?!.")


# --------------------------------------------------------------------------
# Prompt extraction
# --------------------------------------------------------------------------

def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if "parts" in content:
            return "\n".join(p for p in content["parts"] if isinstance(p, str))
        return content.get("text") or ""
    if isinstance(content, list):
        return "\n".join(_content_text(c) for c in content if not (isinstance(c, dict) and c.get("type") not in (None, "text", "input_text")))
    return ""


def _last_user_message(messages) -> str:
    if not isinstance(messages, list):
        return ""
    for m in reversed(messages):
        if not isinstance(m, dict):
            continue
        role = m.get("role") or (m.get("author") or {}).get("role")
        if role in ("user", None):
            return _content_text(m.get("content"))
    return ""


_PROMPT_KEYS = ["prompt", "query_str", "sensitive_string_value", "message", "query", "input", "text", "content", "q"]


def extract_prompt(app_parser: str, body) -> str:
    if body is None:
        return ""
    if isinstance(body, str):
        return body.strip() if len(body) < 20000 else ""
    if isinstance(body, dict):
        for key in ("prompt", "query_str"):
            if isinstance(body.get(key), str):
                return body[key]
        if app_parser == "grok" and isinstance(body.get("message"), str):
            return body["message"]
        if "messages" in body:
            text = _last_user_message(body["messages"])
            if text:
                return text
        if isinstance(body.get("input"), (str, list)):
            text = body["input"] if isinstance(body["input"], str) else _last_user_message(body["input"])
            if text:
                return text
    # Generic: best-ranked key anywhere in the (possibly nested) body.
    found: dict[str, str] = {}
    first_string = ""
    for path, leaf in walk(body):
        if not isinstance(leaf, str) or not leaf.strip():
            continue
        if not first_string and not leaf.startswith(("http", "{", "[")) and " " in leaf:
            first_string = leaf
        key = path[-1].lower() if path else ""
        if key in _PROMPT_KEYS and len(leaf) > len(found.get(key, "")):
            found[key] = leaf
    for key in _PROMPT_KEYS:
        if key in found:
            return found[key]
    return first_string


def extract_model(body) -> str:
    if isinstance(body, dict):
        for key in ("model", "modelName", "model_slug"):
            if isinstance(body.get(key), str):
                return body[key]
    return ""


# --------------------------------------------------------------------------
# Response parsers: each returns dict(text=..., model=..., queries=[...])
# --------------------------------------------------------------------------

def parse_claude(events) -> dict:
    texts, model = [], ""
    tools: dict = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        if t == "message_start":
            model = (ev.get("message") or {}).get("model") or model
        elif t == "message" and isinstance(ev.get("content"), list):  # non-streaming API reply
            model = ev.get("model") or model
            for block in ev["content"]:
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") in ("tool_use", "server_tool_use"):
                    tools[len(tools)] = {"name": block.get("name"), "json": "", "input": block.get("input")}
        elif t == "content_block_start":
            cb = ev.get("content_block") or {}
            if cb.get("type") in ("tool_use", "server_tool_use"):
                tools[ev.get("index")] = {"name": cb.get("name"), "json": "", "input": cb.get("input")}
            elif cb.get("type") == "text" and cb.get("text"):
                texts.append(cb["text"])
        elif t == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta":
                texts.append(d.get("text", ""))
            elif d.get("type") == "input_json_delta" and ev.get("index") in tools:
                tools[ev["index"]]["json"] += d.get("partial_json", "")
    queries = []
    for tool in tools.values():
        name = (tool["name"] or "").lower()
        if "search" not in name and "fetch" not in name:
            continue
        try:
            args = json.loads(tool["json"]) if tool["json"] else (tool["input"] or {})
        except ValueError:
            args = tool["input"] or {}
        if not isinstance(args, dict):
            continue
        if args.get("query"):
            queries.append(str(args["query"]))
        elif args.get("url"):
            queries.append("fetch: " + str(args["url"]))
    return {"text": "".join(texts), "model": model, "queries": queries}


def parse_chatgpt(events) -> dict:
    msgs: list[dict] = []
    cur = None
    last_p = last_o = None
    model = ""

    def apply(p, o, v):
        if cur is None or not isinstance(p, str) or not p.startswith("/message/content/parts/") or not isinstance(v, str):
            return
        if o in (None, "append"):
            cur["text"] += v
        elif o == "replace":
            cur["text"] = v

    for ev in events:
        if not isinstance(ev, dict):
            continue
        v, p, o = ev.get("v"), ev.get("p"), ev.get("o")
        m = ev.get("message") if isinstance(ev.get("message"), dict) else (
            v.get("message") if isinstance(v, dict) and isinstance(v.get("message"), dict) else None)
        if m is not None:
            meta = m.get("metadata") or {}
            model = meta.get("model_slug") or meta.get("default_model_slug") or model
            content = m.get("content") or {}
            role = (m.get("author") or {}).get("role")
            snap = "".join(x for x in content.get("parts", []) if isinstance(x, str))
            if cur is not None and m.get("id") and cur["id"] == m.get("id"):
                if len(snap) >= len(cur["text"]):
                    cur["text"] = snap
            else:
                cur = {"id": m.get("id"), "text": snap,
                       "ok": role == "assistant" and content.get("content_type") in ("text", "multimodal_text")}
                msgs.append(cur)
            last_p = last_o = None
            continue
        if o == "patch" and isinstance(v, list):
            for sub in v:
                if isinstance(sub, dict):
                    apply(sub.get("p"), sub.get("o"), sub.get("v"))
                    last_p, last_o = sub.get("p") or last_p, sub.get("o") or last_o
            continue
        if p is not None:
            last_p, last_o = p, o
        elif o is not None:
            last_o = o
        if isinstance(v, str):
            apply(p if p is not None else last_p, o if o is not None else last_o, v)
    text = "\n\n".join(m["text"] for m in msgs if m["ok"] and m["text"].strip())
    return {"text": text, "model": model, "queries": []}


def parse_grok(events) -> dict:
    tokens, final, model = [], "", ""
    for ev in events:
        if not isinstance(ev, dict):
            continue
        result = ev.get("result")
        if not isinstance(result, dict):
            continue
        resp = result.get("response") if isinstance(result.get("response"), dict) else result
        tok = resp.get("token")
        if isinstance(tok, str) and not resp.get("isThinking") and resp.get("messageTag") in (None, "final"):
            tokens.append(tok)
        mr = resp.get("modelResponse")
        if isinstance(mr, dict) and isinstance(mr.get("message"), str):
            final = mr["message"]
            model = mr.get("model") or model
    return {"text": final or "".join(tokens), "model": model, "queries": []}


def parse_openai_api(events) -> dict:
    parts, model = [], ""
    for ev in events:
        if not isinstance(ev, dict):
            continue
        model = ev.get("model") or model
        if ev.get("type") == "response.output_text.delta":
            parts.append(ev.get("delta", ""))
        for ch in ev.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            c = (ch.get("delta") or ch.get("message") or {}).get("content")
            if isinstance(c, str):
                parts.append(c)
    return {"text": "".join(parts), "model": model, "queries": []}


_TEXT_DELTA_KEYS = {"token", "delta", "text", "chunk"}
_TEXT_SNAPSHOT_KEYS = {"answer", "text", "content", "message", "snippet", "markdown"}


def parse_generic(events) -> dict:
    deltas, longest, model = [], "", ""
    for ev in events:
        for path, leaf in walk(ev):
            if not isinstance(leaf, str):
                continue
            key = path[-1].lower() if path else ""
            if key in ("model", "model_slug", "modelname") and not model:
                model = leaf
            if leaf.startswith("http") or len(leaf) < 1:
                continue
            if key in _TEXT_DELTA_KEYS:
                deltas.append(leaf)
            if (key in _TEXT_SNAPSHOT_KEYS or not path) and len(leaf) > len(longest) and " " in leaf:
                longest = leaf
    joined = "".join(deltas)
    text = longest if len(longest) >= 0.5 * len(joined) else joined
    return {"text": text, "model": model, "queries": []}


PARSERS = {"claude": parse_claude, "chatgpt": parse_chatgpt, "grok": parse_grok,
           "openai_api": parse_openai_api, "generic": parse_generic}


def choose_parser(app: App, path: str) -> str:
    if app.name in ("ChatGPT", "Grok") and re.search(r"/v1/(chat/completions|responses)$", path):
        return "openai_api"
    if app.name == "Grok" and path.endswith("/v1/messages"):
        return "claude"
    return app.parser


# --------------------------------------------------------------------------
# Sources and search queries (format independent)
# --------------------------------------------------------------------------

def _is_own(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in OWN_DOMAINS)


def extract_sources(events, raw_text: str = "") -> list[dict]:
    found: dict[str, dict] = {}

    def add(url: str, title=None):
        url = url.rstrip(".,;:!*'")
        try:
            parsed = urlparse(url)
            domain = (parsed.hostname or "").lower()
        except ValueError:
            return
        if not domain or "." not in domain or _is_own(domain) or _ASSET_EXT.search(parsed.path):
            return
        domain = domain[4:] if domain.startswith("www.") else domain
        entry = found.setdefault(url, {"url": url, "domain": domain, "title": None})
        if isinstance(title, str) and title.strip() and not entry["title"]:
            entry["title"] = title.strip()[:300]

    events = list(events)
    for ev in events:
        for d in _dicts(ev):
            for key in ("url", "link", "uri", "source_url", "href", "sourceUrl"):
                val = d.get(key)
                if isinstance(val, str) and val.startswith("http"):
                    add(val, d.get("title") or d.get("name") or d.get("site_name") or d.get("attribution"))
        for _, leaf in walk(ev):
            if isinstance(leaf, str) and "http" in leaf:
                for m in URL_RE.findall(leaf):
                    add(m)
    if not events and raw_text:
        for m in URL_RE.findall(raw_text.replace("\\/", "/")):
            add(m)
    return list(found.values())


_SEARCH_KEYS = {"search_queries", "searchqueries", "web_search_queries", "websearchqueries", "search_query", "queries"}


def extract_search_queries(events) -> list[str]:
    out = []
    for ev in events:
        for path, leaf in walk(ev):
            if not isinstance(leaf, str) or not leaf.strip() or leaf.startswith("http"):
                continue
            lowered = [p.lower() for p in path]
            key = lowered[-1] if lowered else ""
            in_search = any(p in _SEARCH_KEYS for p in lowered)
            if (in_search and key in _SEARCH_KEYS | {"q", "query", "text"}) or (
                    key in ("q", "query") and any("search" in p for p in lowered[:-1])):
                out.append(leaf.strip())
    return out


def _dedupe(items):
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def analyze_exchange(app: App, path: str, req_body: bytes, req_ct: str, resp_text: str) -> dict:
    """Turn one chat request/response into a structured record."""
    parser = choose_parser(app, path)
    body = parse_request_body(req_body, req_ct)
    prompt = extract_prompt(parser, body)
    events = list(iter_events(resp_text))
    result = PARSERS[parser](events)
    if not result["text"].strip() and parser != "generic":
        result = {**parse_generic(events), "queries": result["queries"]}
        parser += "+generic"
    queries = _dedupe(result["queries"] + extract_search_queries(events))
    queries = [q for q in queries if normalize_prompt(q) != normalize_prompt(prompt)]
    sources = extract_sources(events, resp_text)
    return {
        "parser": parser,
        "prompt": prompt.strip(),
        "model": result["model"] or extract_model(body),
        "response_text": result["text"].strip(),
        "search_queries": queries,
        "sources": sources,
    }
