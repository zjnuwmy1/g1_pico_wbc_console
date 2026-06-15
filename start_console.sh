#!/usr/bin/env bash
# G1 PICO WBC Console launcher — zero-dep, runs on system python3.
set -euo pipefail
export PATH="$HOME/miniconda3/bin:$HOME/.local/bin:$PATH"
cd "$(dirname "$0")"
# Bind to the LAN by default so another PC on the same network can open the
# console. The real-robot deploy still requires passing the in-UI safety gate.
# Restrict back to local-only with:  WBC_HOST=127.0.0.1 ./start_console.sh
exec python3 server.py --host "${WBC_HOST:-0.0.0.0}" "$@"
