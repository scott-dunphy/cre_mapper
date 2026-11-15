#!/usr/bin/env python3
"""AI Traffic Inspector launcher for macOS.

    python3 inspector.py trust-ca         # one time: create + trust the interception certificate
    python3 inspector.py start            # capture (sets the Mac's proxy, opens the dashboard)
    python3 inspector.py start --local    # capture desktop apps by process instead of system proxy
    python3 inspector.py ui               # just browse what was recorded
    python3 inspector.py restore-proxy    # put proxy settings back if something crashed
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dashboard  # noqa: E402
import providers  # noqa: E402
import store  # noqa: E402

STATE_FILE = os.path.join(store.DATA_DIR, "proxy_state.json")
CA_PEM = os.path.expanduser("~/.mitmproxy/mitmproxy-ca-cert.pem")
DEFAULT_LOCAL_APPS = "Claude,ChatGPT,Grok,Perplexity,Google Chrome,Safari,Arc,Microsoft Edge,Brave Browser,Firefox"


def mitmdump_path() -> str:
    for cand in (os.path.join(HERE, ".venv", "bin", "mitmdump"),
                 os.path.join(os.path.dirname(sys.executable), "mitmdump"), shutil.which("mitmdump")):
        if cand and os.path.exists(cand):
            return cand
    sys.exit("mitmdump not found. Run ./setup_mac.sh first (or: pip install mitmproxy).")


def run(cmd, check=True) -> str:
    return subprocess.run(cmd, check=check, capture_output=True, text=True).stdout


# ---------------------------------------------------------------------------
# macOS system proxy
# ---------------------------------------------------------------------------

def network_services() -> list[str]:
    out = run(["networksetup", "-listallnetworkservices"])
    return [line for line in out.splitlines()[1:] if line and not line.startswith("*")]


def read_proxy(service: str, kind: str) -> dict:
    out = run(["networksetup", f"-get{kind}proxy", service], check=False)
    info = dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)
    return {"enabled": info.get("Enabled") == "Yes", "server": info.get("Server", ""), "port": info.get("Port", "0")}


def set_system_proxy(port: int) -> None:
    if os.path.exists(STATE_FILE):
        restore_system_proxy()
    state = {}
    for svc in network_services():
        state[svc] = {k: read_proxy(svc, k) for k in ("web", "secureweb")}
    os.makedirs(store.DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh)
    for svc in state:
        for kind in ("web", "secureweb"):
            run(["networksetup", f"-set{kind}proxy", svc, "127.0.0.1", str(port)], check=False)
            run(["networksetup", f"-set{kind}proxystate", svc, "on"], check=False)
    print(f"• macOS proxy set to 127.0.0.1:{port} on: {', '.join(state)}")


def restore_system_proxy() -> None:
    if not os.path.exists(STATE_FILE):
        return
    with open(STATE_FILE) as fh:
        state = json.load(fh)
    for svc, kinds in state.items():
        for kind, prev in kinds.items():
            if prev["enabled"] and prev["server"]:
                run(["networksetup", f"-set{kind}proxy", svc, prev["server"], str(prev["port"])], check=False)
            else:
                run(["networksetup", f"-set{kind}proxystate", svc, "off"], check=False)
    os.remove(STATE_FILE)
    print("• macOS proxy settings restored")


# ---------------------------------------------------------------------------
# Certificate
# ---------------------------------------------------------------------------

def ensure_ca() -> None:
    if os.path.exists(CA_PEM):
        return
    print("• Generating mitmproxy certificate authority…")
    proc = subprocess.Popen([mitmdump_path(), "--listen-port", "0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if os.path.exists(CA_PEM):
            break
        time.sleep(0.2)
    proc.terminate()
    proc.wait()


def trust_ca(_args) -> None:
    ensure_ca()
    print("• Adding the mitmproxy CA to the System keychain as trusted for TLS (asks for your password)…")
    subprocess.run(["sudo", "security", "add-trusted-cert", "-d", "-r", "trustRoot", "-p", "ssl", "-p", "basic",
                    "-k", "/Library/Keychains/System.keychain", CA_PEM], check=True)
    print("✓ Trusted. Remove it later with:  python3 inspector.py untrust-ca")


def untrust_ca(_args) -> None:
    subprocess.run(["sudo", "security", "remove-trusted-cert", "-d", CA_PEM], check=False)
    subprocess.run(["sudo", "security", "delete-certificate", "-c", "mitmproxy", "/Library/Keychains/System.keychain"], check=False)
    print("✓ mitmproxy CA removed from the System keychain")


def ca_trusted() -> bool:
    r = subprocess.run(["security", "verify-cert", "-c", CA_PEM, "-p", "ssl"], capture_output=True)
    return r.returncode == 0


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def start(args) -> None:
    if sys.platform != "darwin":
        print("! Not macOS: system proxy and certificate trust steps are skipped.")
    ensure_ca()
    if sys.platform == "darwin" and not ca_trusted():
        print("! The mitmproxy certificate is not trusted yet — HTTPS apps will refuse to connect.\n"
              "  Run:  python3 inspector.py trust-ca   (then start again)")
        if not args.force:
            sys.exit(1)

    db = os.path.expanduser(args.db)
    cmd = [mitmdump_path(), "-s", os.path.join(HERE, "addon.py"), "--set", f"aiti_db={db}",
           "--set", "termlog_verbosity=warn", "--set", "flow_detail=0", "--set", "console_eventlog_verbosity=warn"]
    if args.all_hosts:
        cmd += ["--set", "aiti_all_hosts=true"]
    else:
        # Only AI hosts are decrypted; everything else is tunnelled untouched.
        cmd += ["--set", f"allow_hosts={providers.all_host_regex()}"]
    use_system_proxy = not args.local and not args.no_system_proxy and sys.platform == "darwin"
    if args.local:
        cmd += ["--mode", f"local:{args.local}"]
    else:
        cmd += ["--listen-host", "127.0.0.1", "--listen-port", str(args.port)]

    proxy = subprocess.Popen(cmd)
    status = {"capturing": True, "proxy_port": args.port if not args.local else "local", "system_proxy": use_system_proxy}
    stopping = threading.Event()
    lock = threading.Lock()

    def shutdown(*_):
        stopping.set()
        with lock:  # every caller waits until the cleanup has really finished
            if not status["capturing"]:
                return
            status["capturing"] = False
            if use_system_proxy:
                restore_system_proxy()
            if proxy.poll() is None:
                proxy.terminate()
                try:
                    proxy.wait(5)
                except subprocess.TimeoutExpired:
                    proxy.kill()
            print("• Capture stopped.", flush=True)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *a: stopping.set())  # main loop below does the cleanup

    time.sleep(1.0)
    if proxy.poll() is not None:
        sys.exit("mitmdump exited during startup (is port %d in use?)" % args.port)
    if use_system_proxy:
        set_system_proxy(args.port)
    elif not args.local:
        print(f"• Proxy listening on 127.0.0.1:{args.port} (point apps/browsers at it yourself)")
    else:
        print(f"• Local capture mode for processes: {args.local}")

    dashboard.serve(db, args.ui_port, on_shutdown=shutdown, status=status)
    url = f"http://127.0.0.1:{args.ui_port}"
    print(f"• Dashboard: {url}\n  Press Ctrl-C (or 'Stop capture' in the dashboard) to stop and restore settings.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while not stopping.is_set() and proxy.poll() is None:
            time.sleep(0.5)
    finally:
        shutdown()
    # keep the dashboard up for browsing unless asked to quit
    if args.keep_ui:
        print(f"• Dashboard still available at {url} (Ctrl-C to quit)")
        signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
        while True:
            time.sleep(3600)


def ui(args) -> None:
    dashboard.serve(os.path.expanduser(args.db), args.ui_port)
    url = f"http://127.0.0.1:{args.ui_port}"
    print(f"Dashboard (viewer only): {url}  — Ctrl-C to quit")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=store.DEFAULT_DB, help="SQLite database (default %(default)s)")
    common.add_argument("--ui-port", type=int, default=8765)
    common.add_argument("--no-browser", action="store_true")

    s = sub.add_parser("start", parents=[common], help="start capturing")
    s.add_argument("--port", type=int, default=8080, help="proxy port")
    s.add_argument("--local", nargs="?", const=DEFAULT_LOCAL_APPS, metavar="APPS",
                   help="capture by process name instead of the system proxy (needs mitmproxy's macOS "
                        "network extension); default list: %(const)s")
    s.add_argument("--no-system-proxy", action="store_true", help="don't change macOS proxy settings")
    s.add_argument("--all-hosts", action="store_true", help="decrypt and record every host, not only AI apps")
    s.add_argument("--keep-ui", action="store_true", help="keep the dashboard running after capture stops")
    s.add_argument("--force", action="store_true", help="start even if the CA does not look trusted")
    s.set_defaults(fn=start)

    u = sub.add_parser("ui", parents=[common], help="open the dashboard without capturing")
    u.set_defaults(fn=ui)
    sub.add_parser("trust-ca", help="create and trust the interception certificate").set_defaults(fn=trust_ca)
    sub.add_parser("untrust-ca", help="remove the interception certificate").set_defaults(fn=untrust_ca)
    sub.add_parser("restore-proxy", help="restore macOS proxy settings").set_defaults(fn=lambda a: restore_system_proxy())
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
