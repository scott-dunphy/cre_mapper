#!/bin/bash
# One-time setup: Python environment, mitmproxy, trusted certificate, and the .app launcher.
set -euo pipefail
cd "$(dirname "$0")"
PY=$(command -v python3 || true)
if [ -z "$PY" ]; then echo "python3 is needed: install it from python.org or with 'brew install python'"; exit 1; fi
echo "• Creating .venv and installing mitmproxy…"
"$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "• Trusting the interception certificate (macOS will ask for your password)…"
.venv/bin/python inspector.py trust-ca
./make_app.sh
echo
echo "✓ Done. Open 'AI Traffic Inspector' from ~/Applications, or run:  .venv/bin/python inspector.py start"
