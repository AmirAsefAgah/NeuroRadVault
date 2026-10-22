#!/usr/bin/env sh
# Start NeuroRadVault (macOS / Linux). On macOS you can also double-click NeuroRadVault.command.
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then exec python3 -m vault "$@"; fi
if command -v python >/dev/null 2>&1; then exec python -m vault "$@"; fi
echo "Python 3.9 or newer is needed: https://www.python.org/downloads/"
exit 1
