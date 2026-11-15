"""End-to-end: Asker drives a real Chromium through mitmdump (with addon.py) to fake chat sites."""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
CHROME = os.environ.get("AITI_TEST_CHROME", "/opt/pw-browsers/chromium")
MITMDUMP = os.path.join(os.path.dirname(sys.executable), "mitmdump")
pytestmark = pytest.mark.skipif(not (os.path.exists(CHROME) and os.path.exists(MITMDUMP)),
                                reason="needs a Chromium binary and mitmdump")

CHAT_PAGE = """<!doctype html><html><head><title>Fake chat</title></head><body>
<div id="box" contenteditable="true" style="min-height:40px;border:1px solid #888"></div>
<script>
const box = document.getElementById('box');
box.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && SEND) {
    e.preventDefault();
    fetch('/api/organizations/o/chat_conversations/c/completion', {method: 'POST',
      headers: {'content-type': 'application/json'}, body: JSON.stringify({prompt: box.innerText})});
    box.innerText = '';
  }
});
</script></body></html>"""

POSTS = []


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _html(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path == "/chat":
            return self._html(CHAT_PAGE.replace("SEND", "true"))
        if self.path == "/silent":
            return self._html(CHAT_PAGE.replace("SEND", "false"))
        if self.path == "/needs-login":
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
            return
        if self.path == "/login":
            return self._html("<html><head><title>Log in</title></head><body>Please log in</body></html>")
        self._html("nope", 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        POSTS.append(body["prompt"])
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        for ev in [{"type": "message_start", "message": {"model": "fake"}},
                   {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Answer."}}]:
            self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ask")
    site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{site.server_address[1]}"
    apps_file = tmp / "apps.json"
    apps_file.write_text(json.dumps([{"name": "TestBot", "hosts": ["127.0.0.1"], "chat": "/completion$", "parser": "claude"}]))
    db = str(tmp / "t.db")
    port = free_port()
    proxy = subprocess.Popen([MITMDUMP, "-q", "-s", os.path.join(HERE, "addon.py"), "--listen-host", "127.0.0.1",
                              "--listen-port", str(port), "--set", f"aiti_db={db}"],
                             env={**os.environ, "AITI_APPS_FILE": str(apps_file), "AITI_HOME": str(tmp)})
    time.sleep(3)
    import asker
    sites = {"TestBot": {"url": base + "/chat", "composer": ["#box"], "send": []},
             "LoginBot": {"url": base + "/needs-login", "composer": ["#box"], "send": []},
             "SilentBot": {"url": base + "/silent", "composer": ["#box"], "send": []}}
    a = asker.Asker(db, proxy_port=port, chrome_path=CHROME, profile_dir=str(tmp / "profile"), sites=sites,
                    extra_args=["--headless=new", "--no-sandbox", "--proxy-bypass-list=<-loopback>"],
                    answer_timeout=8, composer_timeout=4)
    yield a, db
    a.close()
    proxy.terminate()
    proxy.wait(5)
    site.shutdown()


def wait_run(a, rid, limit=60):
    end = time.time() + limit
    while not a.runs[rid]["done"]:
        assert time.time() < end, a.runs[rid]
        time.sleep(0.5)
    return a.runs[rid]


def test_ask_all_end_to_end(env):
    a, db = env
    rid = a.start_run("What is vacancy in Chicago?\nGive one number.", ["TestBot", "LoginBot", "SilentBot"])
    with pytest.raises(ValueError):
        a.start_run("again", ["TestBot"])  # one run at a time
    run = wait_run(a, rid)
    apps = run["apps"]
    assert apps["TestBot"]["status"] == "answered", apps
    assert apps["TestBot"]["matched_by"] == "prompt"
    assert apps["LoginBot"]["status"] == "needs_signin", apps
    assert apps["SilentBot"]["status"] == "no_answer", apps
    assert POSTS == ["What is vacancy in Chicago?\nGive one number."]  # multi-line prompt sent exactly once

    import store
    con = store.connect(db)
    row = con.execute("SELECT app, prompt, response_text FROM exchanges WHERE id=?", (apps["TestBot"]["exchange_id"],)).fetchone()
    assert row["app"] == "TestBot" and row["response_text"] == "Answer." and "Give one number" in row["prompt"]


def test_close_stops_chrome(env):
    a, _ = env
    proc = a.proc
    assert proc is not None and proc.poll() is None
    a.close()
    assert proc.poll() is not None
    assert a.info()["browser_open"] is False
