#!/bin/bash
# Builds ~/Applications/AI Traffic Inspector.app, a double-clickable launcher for this folder.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/AI Traffic Inspector.app"
mkdir -p "$APP/Contents/MacOS" "$HOME/.ai-traffic-inspector"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>AI Traffic Inspector</string>
  <key>CFBundleIdentifier</key><string>local.ai-traffic-inspector</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/launch" <<LAUNCH
#!/bin/bash
# If already running, just bring up the dashboard.
if curl -s -m 1 http://127.0.0.1:8765/api/status >/dev/null; then open http://127.0.0.1:8765; exit 0; fi
cd "$DIR"
exec "$DIR/.venv/bin/python" inspector.py start >> "\$HOME/.ai-traffic-inspector/app.log" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/launch"
echo "• Built $APP"
