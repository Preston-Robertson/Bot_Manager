#!/usr/bin/env bash
# Launch the Bot Manager web server on Linux (e.g. inside the Proxmox LXC).
set -euo pipefail
cd "$(dirname "$0")"
exec python3 app.py
