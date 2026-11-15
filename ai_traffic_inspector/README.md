# AI Traffic Inspector (macOS)

A local app that records the web traffic of AI chat apps on your Mac (Claude, ChatGPT, Grok,
Meta AI / Muse, Gemini, Perplexity), works out for each question you asked:

- the **prompt** you sent,
- the **web searches** the model ran on your behalf,
- the **sources / URLs** that came back (with titles and domains),
- the **answer** text, the model name and the timing (time to first token, total),

and lets you **compare** the same question across apps: which domains each app cited, which
domains only one app cited, how much the source lists overlap, and the answers side by side.
The raw requests and responses (headers and bodies) are kept too, so you can check every request
an app made and every host it contacted.

Everything stays on your Mac. The dashboard listens on `127.0.0.1` only, and data is stored in
`~/.ai-traffic-inspector/traffic.db` (SQLite).

## How it works

```
 Claude / ChatGPT / Grok / browser
        │  (macOS system proxy, or per-app "local" capture)
        ▼
 mitmproxy on 127.0.0.1:8080  ──►  addon.py  ──►  SQLite  ◄──  dashboard (http://127.0.0.1:8765)
        │  decrypts only AI hosts; everything else is passed through untouched
        ▼
     the internet
```

[mitmproxy](https://mitmproxy.org) does the HTTPS interception with a certificate you trust
once. Only AI-app hosts are decrypted (see `providers.py`). Banking, Apple and all other traffic
goes through the proxy as an untouched encrypted tunnel. Streaming answers are passed to the app
as they arrive and copied on the side, so apps still stream normally.

## Setup (one time)

```bash
cd ai_traffic_inspector
./setup_mac.sh
```

This creates `.venv/`, installs mitmproxy, adds mitmproxy's certificate to your System keychain
(macOS asks for your password), and builds `~/Applications/AI Traffic Inspector.app`.

## Use

Double-click **AI Traffic Inspector** in `~/Applications`, or:

```bash
.venv/bin/python inspector.py start
```

It switches your Mac's HTTP/HTTPS proxy to the inspector and opens the dashboard. Then ask the
same question in Claude, ChatGPT, Grok and Meta AI. Each question shows up under **Queries**.
The **Compare** tab groups questions with the same (or nearly the same) wording across apps.
You can also tick any queries and press **Compare selected**.

**Stop capture** (in the dashboard) or Ctrl-C stops the proxy and **restores your original proxy
settings**. If the process is ever killed hard, run:

```bash
.venv/bin/python inspector.py restore-proxy
```

Other commands:

| Command | What it does |
|---|---|
| `inspector.py ui` | Browse recorded data without capturing |
| `inspector.py start --local` | Capture by **app process** using mitmproxy's macOS network extension instead of the system proxy (approve "Mitmproxy Redirector" in System Settings → General → Login Items & Extensions the first time). Catches apps that ignore the system proxy. |
| `inspector.py start --local "Claude,ChatGPT"` | Same, for just those processes |
| `inspector.py start --all-hosts` | Decrypt and record **every** host, not just AI apps (more breakage risk) |
| `inspector.py start --no-system-proxy` | Run the proxy on :8080 without touching settings, then point one browser at it |
| `inspector.py untrust-ca` | Remove the mitmproxy certificate from the keychain |

Export: **Export sources CSV** gives one row per (query, cited URL) with rank, domain, title and
the searches the model ran. You can open it in Excel to track which sites each AI cites.

## Ask every app the same question

At the top of the **Queries** tab, type a question, tick the apps and press **Ask all**.

1. **First time only:** press **Open Chrome to sign in**. A separate Chrome window opens with a
   tab for each app, using the inspector's own Chrome profile
   (`~/.ai-traffic-inspector/chrome-profile`; your everyday Chrome profile isn't touched). Sign in
   to each site, then quit that window (⌘Q). You stay signed in for later runs.
2. **Ask all** opens that profile in a visible Chrome window. It starts a new chat in each app,
   types the question and presses Enter, one site at a time. It then waits until the inspector
   captures each answer. The status for each app reads *answered*, *sign in*, *needs you* (solve
   the "verify you're human" check in the window and the run continues), *no text box* or
   *no answer*.
3. When it finishes, **Compare these answers** opens them side by side.

Notes:
- **Google Chrome must be installed**, or start with `--chrome /path/to/chrome`. Playwright
  attaches to your real Chrome, so `playwright install` is never needed.
- **The automation window has a debugging port on 127.0.0.1** so the inspector can control it.
  It's closed, together with that Chrome window, when capture stops.
- **For Claude to search the web,** turn on web search in Claude's settings.
- **Website changes can break the automation.** If a site's layout changes and it can't find the
  message box, update the selectors in `apps.local.json`:
  `[{"name": "ChatGPT", "ask_url": "https://chatgpt.com/", "composer": ["#prompt-textarea"]}]`
  (an entry for an existing app changes only the fields you give)
- **Account risk:** automating consumer AI apps may be against their terms of service. Runs happen
  only when you press the button, one at a time, with no scheduling.

## Making your sites more findable by AI (Visibility tab)

1. **List your domains and competitors.** Subdomains count. The tool can only say "not cited"
   for domains you name.
2. **Citation share by app.** For each domain and app, the share of your captured questions
   whose sources include it, and its average position in the source list.
3. **Cited by some apps but not others.** One app cited your page for a question and another
   didn't. The card shows the searches each app ran. Check the missing app's crawler access, and
   compare its searches with your title and headings. **Audit it** runs that check using the
   missing app's searches.
4. **Searches the AIs ran when you weren't cited.** These are the literal web searches the models
   issued. Ranking for them is what gets a page pulled into an answer, so they're your content
   targets.
5. **Page audit.** Enter your page, the pages the AI cited instead, and the AI's searches. It
   fetches each page from your Mac (not through the capture proxy) and reports:
   - **robots.txt rules for each AI crawler, split by role.** *Search* crawlers (OAI-SearchBot,
     Claude-SearchBot, PerplexityBot…) and *user-fetch* agents decide whether you can appear in
     answers. *Training* crawlers (GPTBot, ClaudeBot) don't, per the vendors' docs, so blocking
     only those isn't flagged as a problem. The table and its sources are in `bots.py`. xAI
     publishes no official crawler docs, so Grok's tokens are marked unverified and not scored.
   - robots.txt read the way the standard (RFC 9309) says crawlers read it: a 4xx means no rules;
     a 5xx or no answer means crawlers stop
   - noindex / nosnippet tags, canonical, HTTP status and speed, and bot-protection challenge
     pages (for example Cloudflare's "Just a moment")
   - **text visible without JavaScript.** AI fetchers generally don't run JavaScript, so a
     client-rendered page can look empty to them.
   - title, H1–H3, JSON-LD schema types, published/updated dates
   - how many of the AI's search terms appear in your title, headings and body
   - an *indicative* fetch using AI-bot user-agents. CDNs verify real crawlers by IP, so a
     different response is only a hint.
   - /llms.txt presence, reported as low evidence

   It then lists **observed differences** between your page and the cited ones (word count,
   schema, dates, search-term coverage, JavaScript dependence). These are differences, not proof
   of why a model chose a page.

From the Queries tab, every source has an **audit** link.

## What it can and can't see

- **Browser versions work best.** claude.ai, chatgpt.com, grok.com and meta.ai in Chrome, Safari,
  Arc or Edge all honour the system proxy and the keychain certificate. Firefox uses its own
  certificate store: import `~/.mitmproxy/mitmproxy-ca-cert.pem` in Firefox settings.
- **Desktop apps:** the Claude desktop app (Electron) follows the system proxy and keychain. Native
  apps such as ChatGPT for Mac usually do too. **If an app pins its certificates, its requests
  will fail while the inspector is running.** Use that app's web version instead, and stop the
  inspector to get the app working again.
- **Searches happen on the vendor's servers.** The apps don't fetch web pages from your Mac, so
  the inspector sees what the vendor sends back: the search queries (when the app shows them),
  the result URLs and titles, and the citations. It can't see pages the vendor read but didn't
  report back to the app.
- **Private web APIs change.** The vendors' internal chat APIs aren't documented and change often.
  Each app has a dedicated parser plus a generic fallback that still pulls out the prompt, the URLs
  and the longest answer text. If an app changes format, the raw request and response are always
  saved in **Traffic**. The `parser` field on each query shows which parser was used.
- **"Muse"** is treated as Meta AI (meta.ai), where Meta's Muse models are served. To add or
  change an app, create `apps.local.json`:

  ```json
  [{"name": "Muse", "hosts": ["example-muse-host.com"], "chat": "/api/chat", "parser": "generic"}]
  ```

## Privacy

Your prompts and answers are stored unencrypted in `~/.ai-traffic-inspector/traffic.db`.
`Authorization`, `Cookie`, `Set-Cookie`, API-key and CSRF headers are replaced with
`[redacted]` before anything is written. **Clear all data** in the dashboard wipes the database.
The trusted mitmproxy certificate can decrypt traffic only while the proxy is running. Remove it
with `untrust-ca` when you're done.

## Files

| File | Purpose |
|---|---|
| `inspector.py` | Launcher: certificate, system proxy on/off, starts mitmdump + dashboard |
| `addon.py` | mitmproxy addon: stream copy, redaction, writes flows and parsed queries |
| `visibility.py`, `audit.py`, `bots.py` | Citation share / missed queries, page audit, AI crawler table |
| `asker.py` | “Ask all”: drives a visible Chrome window to send one question to every app |
| `providers.py` | App/host table and parsers (Claude SSE, ChatGPT delta stream, Grok NDJSON, OpenAI API, generic) |
| `store.py` | SQLite schema |
| `dashboard.py`, `static/index.html` | Local web UI and JSON API |
| `tests/` | Parser tests: `python -m pytest tests` |
