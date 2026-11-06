#!/bin/zsh
set -euo pipefail
cd "${0:A:h:h}"
SWIFT_MODE="${1:-release}"
swift build -c "$SWIFT_MODE"
APP="$PWD/build/MCP Server Manager.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/backend"
cp ".build/$SWIFT_MODE/MCPServerManager" "$APP/Contents/MacOS/MCPServerManager"
cp backend/*.py "$APP/Contents/Resources/backend/"
swift scripts/make-icon.swift "$PWD/build/AppIcon.iconset"
iconutil -c icns "$PWD/build/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
PYTHON_BIN="$(command -v python3)"
"$PYTHON_BIN" - "$APP" "$PYTHON_BIN" <<'PY'
import pathlib, plistlib, sys, shutil
app = pathlib.Path(sys.argv[1])
shutil.rmtree(app / 'Contents/Resources/backend/__pycache__', ignore_errors=True)
plist = {'CFBundleIconFile': 'AppIcon', 'CFBundleName': 'MCP Server Manager', 'CFBundleDisplayName': 'MCP Server Manager', 'CFBundleIdentifier': 'local.mcp-server-manager', 'CFBundleExecutable': 'MCPServerManager', 'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': '0.1.0', 'CFBundleVersion': '1', 'LSMinimumSystemVersion': '14.0', 'LSUIElement': True, 'NSHighResolutionCapable': True, 'NSPrincipalClass': 'NSApplication', 'MCPManagerPython': sys.argv[2]}
(app / 'Contents/Info.plist').write_bytes(plistlib.dumps(plist))
PY
codesign --force --deep --sign - "$APP"
print -r -- "$APP"
