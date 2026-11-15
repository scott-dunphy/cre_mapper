"""mitmproxy addon: records AI-app traffic into SQLite.

Run through inspector.py, or directly:
    mitmdump -s addon.py --set aiti_db=~/.ai-traffic-inspector/traffic.db
"""
from __future__ import annotations

import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mitmproxy import ctx, http  # noqa: E402
from mitmproxy.net import encoding  # noqa: E402

import providers  # noqa: E402
import store  # noqa: E402

log = logging.getLogger("aiti")
SKIP_BODY_TYPES = ("image/", "font/", "video/", "audio/", "application/wasm", "application/octet-stream")


class Inspector:
    def __init__(self):
        self.con = None
        self.streams: dict[str, dict] = {}
        self.recorded: set[str] = set()

    def load(self, loader):
        loader.add_option("aiti_db", str, store.DEFAULT_DB, "SQLite file to record into.")
        loader.add_option("aiti_all_hosts", bool, False, "Record every host, not only known AI apps.")
        loader.add_option("aiti_max_body", int, 4_000_000, "Max bytes of each body to keep.")

    def configure(self, updated):
        if "aiti_db" in updated or self.con is None:
            self.con = store.connect(os.path.expanduser(ctx.options.aiti_db))

    # -- helpers ----------------------------------------------------------
    def _app(self, flow: http.HTTPFlow):
        return providers.identify_app(flow.request.pretty_host)

    def _wanted(self, flow) -> bool:
        return self._app(flow) is not None or ctx.options.aiti_all_hosts

    # -- hooks ------------------------------------------------------------
    def responseheaders(self, flow: http.HTTPFlow):
        """Stream chat / event-stream replies through to the app while keeping a copy."""
        if not self._wanted(flow):
            return
        app = self._app(flow)
        ct = flow.response.headers.get("content-type", "")
        chat = app is not None and app.is_chat(flow.request.path)
        if not (chat or "event-stream" in ct or "ndjson" in ct):
            return
        state = {"buf": bytearray(), "first": None}
        self.streams[flow.id] = state
        limit = ctx.options.aiti_max_body

        def tee(data: bytes) -> bytes:
            if data and state["first"] is None:
                state["first"] = time.time()
            if len(state["buf"]) < limit:
                state["buf"].extend(data)
            return data

        flow.response.stream = tee

    def response(self, flow: http.HTTPFlow):
        self._safe_record(flow)

    def error(self, flow: http.HTTPFlow):
        # Also covers streams cut off part-way (e.g. pressing "Stop" in the app): keep the partial answer.
        self._safe_record(flow)

    def _safe_record(self, flow: http.HTTPFlow):
        if not self._wanted(flow) or flow.id in self.recorded:
            return
        self.recorded.add(flow.id)
        if len(self.recorded) > 5000:
            self.recorded.clear()
        try:
            self._record(flow)
        except Exception:  # never break the user's traffic because of a parser bug
            log.exception("aiti: failed to record %s", flow.request.pretty_url)

    def websocket_message(self, flow: http.HTTPFlow):
        if not self._wanted(flow) or not flow.websocket:
            return
        msg = flow.websocket.messages[-1]
        app = self._app(flow)
        store.insert_flow(
            self.con, ts=msg.timestamp, app=app.name if app else "other",
            client=providers.detect_client(flow.request.headers.get("user-agent", "")),
            mode=flow.client_conn.proxy_mode.full_spec,
            method="WS→" if msg.from_client else "WS←", url=flow.request.pretty_url,
            host=flow.request.pretty_host, path=flow.request.path, status=101,
            req_ct="", resp_ct="text" if msg.is_text else "binary", req_headers="[]", resp_headers="[]",
            req_body=msg.content if msg.from_client else None, resp_body=None if msg.from_client else msg.content,
            req_size=len(msg.content) if msg.from_client else 0, resp_size=0 if msg.from_client else len(msg.content),
        )

    # -- recording ----------------------------------------------------------
    def _record(self, flow: http.HTTPFlow):
        req, resp = flow.request, flow.response
        app = self._app(flow)
        app_name = app.name if app else "other"
        client = providers.detect_client(req.headers.get("user-agent", ""))
        mode = flow.client_conn.proxy_mode.full_spec
        if mode.startswith("local"):
            client = f"{client} (local)"
        limit = ctx.options.aiti_max_body

        req_body = req.get_content(strict=False) or b""
        resp_body, resp_ct, status, ttft = b"", "", 0, None
        state = self.streams.pop(flow.id, None)
        if resp is not None:
            resp_ct = resp.headers.get("content-type", "")
            status = resp.status_code
            if state is not None:
                resp_body = bytes(state["buf"])
                ttft = (state["first"] - req.timestamp_start) if state["first"] else None
                enc = resp.headers.get("content-encoding", "").strip().lower()
                if enc and enc != "identity":
                    try:
                        resp_body = encoding.decode(resp_body, enc)
                    except Exception:
                        pass
            else:
                resp_body = resp.get_content(strict=False) or b""
        end = (resp.timestamp_end if resp and resp.timestamp_end else time.time())
        ttfb = (resp.timestamp_start - req.timestamp_start) if resp and resp.timestamp_start else None
        is_chat = bool(app and app.is_chat(req.path))
        keep_resp = not any(resp_ct.startswith(t) for t in SKIP_BODY_TYPES)

        flow_id = store.insert_flow(
            self.con, ts=req.timestamp_start, app=app_name, client=client, mode=mode,
            method=req.method, url=req.pretty_url, host=req.pretty_host, path=req.path, status=status,
            req_ct=req.headers.get("content-type", ""), resp_ct=resp_ct,
            req_headers=store.headers_json(req.headers.items(multi=True)),
            resp_headers=store.headers_json(resp.headers.items(multi=True)) if resp else "[]",
            req_body=req_body[:limit], resp_body=resp_body[:limit] if keep_resp else None,
            req_size=len(req_body), resp_size=len(resp_body),
            ttfb=ttfb, ttft=ttft, duration=end - req.timestamp_start, is_chat=int(is_chat),
            error=flow.error.msg if flow.error else None,
        )
        if not is_chat or req.method != "POST":
            return
        analysis = providers.analyze_exchange(
            app, req.path, req_body, req.headers.get("content-type", ""), resp_body.decode("utf-8", "replace"))
        if analysis["prompt"] or analysis["response_text"]:
            store.insert_exchange(self.con, flow_id, req.timestamp_start, app_name, client,
                                  ttft or ttfb, end - req.timestamp_start, analysis)
            log.info("aiti: %s | %s | %d sources", app_name, analysis["prompt"][:60], len(analysis["sources"]))


addons = [Inspector()]
