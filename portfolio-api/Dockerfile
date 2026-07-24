FROM python:3.12-slim
WORKDIR /app

# curl: fetches the acme.sh installer below. socat: acme.sh's --standalone HTTP-01 challenge
# listener needs it. Both are also needed at RUNTIME (acme.sh shells out to them), not just at
# build time, so they stay installed rather than being removed after the install step.
RUN apt-get update && apt-get install -y --no-install-recommends curl socat ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Vendored acme.sh (2026-07-23f) — the built-in Let's Encrypt / ACME client tls.py shells out to.
# Deliberately invoked with ZERO flags (2026-07-23h, second fix — a real deploy proved the FIRST
# fix's --nocron alone still broke with "Unknown parameter: ----nocron": get.acme.sh's installer
# doubles a leading "--" on ANY flag when invoked as `sh savedfile.sh --whatever`, not just
# --home — so the only invocation immune to that bug is passing nothing at all). This means it
# installs to its plain default location (~/.acme.sh -> /root/.acme.sh, since this RUN executes
# as root) and may register its own default cron entry — both harmless here: (1) this container
# has no init system/cron daemon actually running to ever fire that entry, and (2) even if it
# somehow did, it would operate on /root/.acme.sh as the data dir, completely separate from the
# ACME_HOME (/data/acme, under the persistent volume) tls.py always passes explicitly via --home
# on every REAL issue/renew call it makes at runtime — a totally different, well-documented
# per-command usage of that flag that was never the problem. Renewal is driven by tls.py's own
# background thread (app.py's tls.start_renewal_thread()) regardless.
RUN curl -fsSL https://get.acme.sh -o /tmp/acme-install.sh \
    && sh /tmp/acme-install.sh \
    && rm -f /tmp/acme-install.sh

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# 8097 = plain HTTP (default, no certificate configured yet). 8443 = HTTPS, once one of the 3
# in-app certificate methods (SETTINGS -> SECURITY -> Certificate) is set up — run_server.py
# picks whichever applies at every (re)start, see tls.py. Only one is actually listening at a
# time; the other's mapped host port simply refuses connections until you switch modes.
EXPOSE 8097 8443
CMD ["python", "run_server.py"]
