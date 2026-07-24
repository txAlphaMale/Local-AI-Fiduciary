#!/usr/bin/env python3
"""
run_server.py — the container's entrypoint. Decides plain-HTTP vs. HTTPS by checking whether a
valid certificate/key pair currently exists under TLS_DIR (see tls.py), then os.execv's into
uvicorn with the right flags.

This indirection (rather than baking --ssl-certfile/--ssl-keyfile straight into the Dockerfile
CMD) is what makes tls.restart_to_apply() work: installing a new/renewed/imported certificate
re-execs THIS script rather than uvicorn directly, so the HTTP-vs-HTTPS decision gets re-derived
fresh from whatever's on disk at that moment. os.execv replaces the process image in place (same
PID) — Docker's restart policy never sees an exit, so this is a true in-place restart, not a
container restart.

Started via `python run_server.py` — see Dockerfile.
"""
import os
import sys

import tls  # local module, same directory — Python adds the script's own dir to sys.path[0]

HOST = "0.0.0.0"
HTTP_PORT = os.environ.get("PORT", "8097")
HTTPS_PORT = os.environ.get("HTTPS_PORT", "8443")

args = [sys.executable, "-m", "uvicorn", "app:app", "--host", HOST]

if tls.has_active_cert():
    args += ["--port", HTTPS_PORT, "--ssl-certfile", tls.CERT_PATH, "--ssl-keyfile", tls.KEY_PATH]
    print(f"[run_server] starting HTTPS on port {HTTPS_PORT} "
          f"(cert: {tls.CERT_PATH})", flush=True)
else:
    args += ["--port", HTTP_PORT]
    print(f"[run_server] starting plain HTTP on port {HTTP_PORT} "
          f"(no active certificate — set one up under SETTINGS -> SECURITY -> Certificate)",
          flush=True)

os.execv(sys.executable, args)
