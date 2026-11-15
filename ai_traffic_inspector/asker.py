"""Ask every AI app the same question by driving a visible Google Chrome window.

How it works
- Chrome is started as a normal, visible app with its own profile under
  ~/.ai-traffic-inspector/chrome-profile (your everyday Chrome profile is never touched) and a
  debugging port on 127.0.0.1 so Playwright can attach to it. No stealth tricks are used: if a
  site shows a "verify you are human" check, the run waits for you to solve it in the window.
- For each app it opens a new chat, types the question and presses Enter, one site at a time.
- The answer is detected by the capture itself: the run waits until the inspector records a new
  exchange for that app. Page layout is only used to find the text box.
- Sign-in is a separate step ("Open Chrome to sign in") that launches the same profile *without*
  the debugging port, so sites see a plain Chrome window. Sessions persist in the profile.

Automating consumer AI apps may be against their terms of service. Runs are one at a time and
only when you click "Ask all"; there is no scheduling.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from difflib import SequenceMatcher

import store
from providers import normalize_prompt

PROFILE_DIR = os.path.join(store.DATA_DIR, "chrome-profile")
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
]

# URLs and selectors change often: override any entry with "ask_url", "composer" and "send"
# keys in apps.local.json. Composer selectors are tried in order, then the generic fallbacks.
SITES = {
    "ChatGPT": {"url": "https://chatgpt.com/", "composer": ["#prompt-textarea", "div.ProseMirror[contenteditable='true']"],
                "send": ["button[data-testid='send-button']", "button[aria-label*='Send' i]"]},
    "Claude": {"url": "https://claude.ai/new", "composer": ["div.ProseMirror[contenteditable='true']",
                                                            "div[contenteditable='true'][aria-label*='prompt' i]"],
               "send": ["button[aria-label*='Send' i]"]},
    "Grok": {"url": "https://grok.com/", "composer": ["textarea[aria-label*='Ask' i]", "div.ProseMirror[contenteditable='true']"],
             "send": ["button[type='submit']", "button[aria-label*='Submit' i]"]},
    "Meta AI (Muse)": {"url": "https://www.meta.ai/", "composer": ["div[contenteditable='true'][role='textbox']", "textarea"],
                       "send": ["div[aria-label*='Send' i]", "button[aria-label*='Send' i]"]},
    "Gemini": {"url": "https://gemini.google.com/app", "composer": ["rich-textarea div[contenteditable='true']",
                                                                    "div.ql-editor[contenteditable='true']"],
               "send": ["button[aria-label*='Send' i]"]},
    "Perplexity": {"url": "https://www.perplexity.ai/", "composer": ["#ask-input", "textarea[placeholder]"],
                   "send": ["button[aria-label*='Submit' i]", "button[aria-label*='Send' i]"]},
}
GENERIC_COMPOSER = ["[contenteditable='true']", "textarea"]
LOGIN_RE = re.compile(r"log-?in|sign-?in|sign-?up|/auth|accounts\.google|/oauth", re.I)
CHALLENGE_RE = re.compile(r"just a moment|verify you are human|are you a robot|attention required|captcha", re.I)
BACKGROUND_FLAGS = ["--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows"]


def _local_overrides(sites: dict) -> dict:
    path = os.environ.get("AITI_APPS_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "apps.local.json")
    if os.path.exists(path):
        with open(path) as fh:
            for spec in json.load(fh):
                if spec.get("ask_url"):
                    base = sites.get(spec["name"], {})
                    sites[spec["name"]] = {"url": spec["ask_url"], "composer": spec.get("composer", base.get("composer", [])),
                                           "send": spec.get("send", base.get("send", []))}
    return sites


def find_chrome(custom: str | None = None) -> str | None:
    for c in ([custom] if custom else []) + CHROME_CANDIDATES:
        if c and os.path.exists(c):
            return c
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def profile_in_use(profile: str) -> bool:
    """Chrome leaves a SingletonLock symlink ("host-pid") while a profile is open."""
    lock = os.path.join(profile, "SingletonLock")
    if not os.path.lexists(lock):
        return False
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[1])
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, IndexError):
        return False


class Asker:
    def __init__(self, db_path: str, proxy_port: int | None = None, chrome_path: str | None = None,
                 profile_dir: str = PROFILE_DIR, sites: dict | None = None, extra_args=(),
                 answer_timeout: float = 240, composer_timeout: float = 20, challenge_timeout: float = 180):
        self.db_path = db_path
        self.proxy_port = proxy_port
        self.chrome = find_chrome(chrome_path)
        self.profile = profile_dir
        self.sites = sites if sites is not None else _local_overrides({k: dict(v) for k, v in SITES.items()})
        self.extra_args = list(extra_args)
        self.answer_timeout, self.composer_timeout, self.challenge_timeout = answer_timeout, composer_timeout, challenge_timeout
        self.runs: dict[int, dict] = {}
        self._ids = itertools.count(1)
        self._busy = False
        self.proc = self.signin_proc = None
        self._pw = self._browser = None
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="asker").start()

    # ---- public API (called from HTTP threads) --------------------------------
    def info(self) -> dict:
        return {"sites": list(self.sites), "chrome": self.chrome, "busy": self._busy,
                "profile_in_use": profile_in_use(self.profile) and not self._automation_alive(),
                "browser_open": self._automation_alive()}

    def start_run(self, prompt: str, apps: list[str]) -> int:
        prompt = (prompt or "").strip()
        apps = [a for a in apps if a in self.sites]
        if not prompt or not apps:
            raise ValueError("Type a question and pick at least one app.")
        if not self.chrome:
            raise ValueError("Google Chrome wasn't found. Install it, or start with --chrome /path/to/chrome.")
        if self._busy:
            raise ValueError("A run is already in progress.")
        if profile_in_use(self.profile) and not self._automation_alive():
            raise ValueError("The sign-in Chrome window is still open. Close it (⌘Q), then press Ask all again.")
        self._busy = True
        rid = next(self._ids)
        self.runs[rid] = {"id": rid, "prompt": prompt, "ts": time.time(), "done": False, "error": None,
                          "apps": {a: {"status": "queued", "detail": "", "exchange_id": None, "matched_by": None}
                                   for a in apps}}
        asyncio.run_coroutine_threadsafe(self._run(self.runs[rid]), self.loop)
        return rid

    def open_signin(self, apps: list[str] | None = None) -> None:
        """Plain Chrome window (no debugging port) on the same profile, one tab per site."""
        if not self.chrome:
            raise ValueError("Google Chrome wasn't found.")
        if self._automation_alive():
            self.close()
        if profile_in_use(self.profile):
            raise ValueError("That Chrome profile is already open.")
        urls = [self.sites[a]["url"] for a in (apps or self.sites) if a in self.sites]
        os.makedirs(self.profile, exist_ok=True)
        self.signin_proc = subprocess.Popen([self.chrome, f"--user-data-dir={self.profile}", "--no-first-run",
                                             "--no-default-browser-check", *self.extra_args, *urls],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def close(self) -> None:
        if self._browser or self._pw:
            try:
                asyncio.run_coroutine_threadsafe(self._disconnect(), self.loop).result(10)
            except Exception:
                pass
        for proc in (self.proc,):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(8)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self.proc = None

    # ---- browser -------------------------------------------------------------
    def _automation_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    async def _disconnect(self):
        try:
            if self._browser:
                await self._browser.close()
        finally:
            self._browser = None
            if self._pw:
                await self._pw.stop()
                self._pw = None

    async def _ensure_browser(self):
        if self._automation_alive() and self._browser and self._browser.is_connected():
            return self._browser.contexts[0]
        await self._disconnect()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        os.makedirs(self.profile, exist_ok=True)
        port = _free_port()
        args = [self.chrome, f"--user-data-dir={self.profile}", f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
                *BACKGROUND_FLAGS, *self.extra_args]
        if self.proxy_port:
            args.append(f"--proxy-server=http://127.0.0.1:{self.proxy_port}")
        self.proc = subprocess.Popen(args + ["about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        endpoint = f"http://127.0.0.1:{port}"
        for _ in range(100):
            if self.proc.poll() is not None:
                raise RuntimeError("Chrome exited on startup (is that profile open in another window?)")
            try:
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(endpoint + "/json/version", timeout=1)
                break
            except OSError:
                await asyncio.sleep(0.2)
        else:
            raise RuntimeError("Chrome didn't open its debugging port.")
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(endpoint)
        return self._browser.contexts[0]

    # ---- one run -------------------------------------------------------------
    async def _run(self, run: dict):
        try:
            ctx = await self._ensure_browser()
        except Exception as e:
            run["error"] = str(e)[:300]
            for st in run["apps"].values():
                st.update(status="error", detail="Chrome couldn't be started")
            run["done"], self._busy = True, False
            return
        for old in list(ctx.pages):  # tidy up tabs from the previous run
            if old.url != "about:blank" or len(ctx.pages) > 1:
                try:
                    await old.close()
                except Exception:
                    pass
        try:
            for app, st in run["apps"].items():  # type into one site at a time
                await self._send(ctx, app, st, run["prompt"])
            await self._await_answers(run)
        except Exception as e:  # pragma: no cover
            run["error"] = str(e)[:300]
        finally:
            run["done"], self._busy = True, False

    async def _find_composer(self, page, site, st):
        selectors = list(site.get("composer", [])) + GENERIC_COMPOSER
        deadline = time.time() + self.composer_timeout
        while True:
            title = ""
            try:
                title = await page.title()
            except Exception:
                pass
            if CHALLENGE_RE.search(title):
                st.update(status="needs_you", detail="Solve the check in the Chrome window; the run will continue.")
                deadline = max(deadline, time.time() + 2)
                if time.time() > st.setdefault("_challenge_start", time.time()) + self.challenge_timeout:
                    return None
            for sel in selectors:
                loc = page.locator(sel).first
                try:
                    if await loc.count() and await loc.is_visible() and await loc.is_editable():
                        return loc
                except Exception:
                    continue
            if time.time() > deadline:
                return None
            await asyncio.sleep(0.5)

    async def _send(self, ctx, app, st, prompt):
        site = self.sites[app]
        st.update(status="opening", detail=site["url"])
        try:
            page = await ctx.new_page()
            await page.goto(site["url"], wait_until="domcontentloaded", timeout=45000)
            box = await self._find_composer(page, site, st)
            st.pop("_challenge_start", None)
            if box is None:
                if LOGIN_RE.search(page.url):
                    st.update(status="needs_signin", detail="Sign in in the Chrome window, then press Ask all again.")
                elif st["status"] == "needs_you":
                    st.update(status="needs_you", detail="The human check wasn't completed in time.")
                else:
                    st.update(status="no_textbox", detail="Couldn't find the message box (not signed in, or the page "
                                                          "layout changed; selectors can be updated in apps.local.json).")
                return
            await page.bring_to_front()
            await box.click()
            lines = prompt.split("\n")
            for i, line in enumerate(lines):
                if line:
                    await page.keyboard.insert_text(line)
                if i < len(lines) - 1:
                    await page.keyboard.press("Shift+Enter")
            st["sent_at"] = time.time()
            await page.keyboard.press("Enter")
            await asyncio.sleep(1.2)
            if await self._still_has_text(box, prompt):
                for sel in site.get("send", []):
                    btn = page.locator(sel).first
                    try:
                        if await btn.count() and await btn.is_enabled():
                            await btn.click()
                            break
                    except Exception:
                        continue
            st.update(status="waiting", detail="Sent; waiting for the captured answer…")
        except Exception as e:
            st.update(status="error", detail=str(e).splitlines()[0][:200])

    @staticmethod
    async def _still_has_text(box, prompt) -> bool:
        head = prompt.strip().split("\n")[0][:30]
        try:
            tag = await box.evaluate("e => e.tagName")
            text = await (box.input_value() if tag in ("TEXTAREA", "INPUT") else box.inner_text())
        except Exception:
            return False
        return bool(head) and head in text

    def _match(self, app: str, since: float, prompt: str):
        con = store.connect(self.db_path)
        try:
            rows = con.execute("SELECT id, ts, prompt_norm FROM exchanges WHERE app=? AND ts>=? ORDER BY ts",
                               (app, since - 2)).fetchall()
        finally:
            con.close()
        want = normalize_prompt(prompt)
        for r in rows:
            have = r["prompt_norm"] or ""
            if have and (have == want or want in have or SequenceMatcher(None, want, have).ratio() >= 0.7):
                return r["id"], "prompt"
        old = [r for r in rows if time.time() - r["ts"] > 15]
        return (old[0]["id"], "time") if old else (None, None)

    async def _await_answers(self, run):
        while True:
            waiting = [(a, st) for a, st in run["apps"].items() if st["status"] == "waiting"]
            if not waiting:
                return
            for app, st in waiting:
                ex_id, how = await asyncio.to_thread(self._match, app, st["sent_at"], run["prompt"])
                if ex_id:
                    st.update(status="answered", exchange_id=ex_id, matched_by=how,
                              detail="Answer captured" + (" (matched by time, not prompt text)" if how == "time" else ""))
                elif time.time() - st["sent_at"] > self.answer_timeout:
                    st.update(status="no_answer", detail="Sent, but no answer was captured. Check the Traffic tab "
                                                         "and the Chrome window.")
            await asyncio.sleep(1.5)


if __name__ == "__main__":  # manual check: python asker.py "question"
    a = Asker(store.DEFAULT_DB, proxy_port=8080)
    rid = a.start_run(sys.argv[1], list(SITES))
    while not a.runs[rid]["done"]:
        time.sleep(2)
        print({k: v["status"] for k, v in a.runs[rid]["apps"].items()})
    print(json.dumps(a.runs[rid], indent=2, default=str))
