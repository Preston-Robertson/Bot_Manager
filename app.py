"""Bot Manager entry point.

The real engine lives in `manager_core.py`; the FastAPI adapter lives in
`web/server.py`. This module just starts the ASGI server.

Environment variables:
  BOTMGR_HOST   bind address (default 0.0.0.0)
  BOTMGR_PORT   bind port    (default 28473)
  BOTMGR_TOKEN  optional shared-secret for the dashboard + API
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("BOTMGR_HOST", "0.0.0.0")
    # 28473 is IANA-unassigned and outside Linux's default ephemeral range
    # (net.ipv4.ip_local_port_range 32768-60999), so the kernel won't ever
    # auto-grab it for an outbound socket. Override with BOTMGR_PORT.
    port = int(os.environ.get("BOTMGR_PORT", "28473"))
    uvicorn.run("web.server:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
