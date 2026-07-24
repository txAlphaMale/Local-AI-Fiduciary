FROM python:3.12-slim
WORKDIR /app

# curl: fetches the acme.sh installer below. socat: acme.sh's --standalone HTTP-01 challenge
# listener needs it. Both are also needed at RUNTIME (acme.sh shells out to them), not just at
# build time, so they stay installed rather than being removed after the install step.
RUN apt-get update && apt-get install -y --no-install-recommends curl socat ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Vendored acme.sh (2026-07-23f) — the built-in Let's Encrypt / ACME client tls.py shells out to.
# --nocron: renewal is driven by tls.py's own background thread (app.py's
# tls.start_renewal_thread()), not acme.sh's usual crontab entry — one renewal mechanism, not two.
# Deliberately NOT passing --home here (2026-07-23h fix — a real deploy hit "Unknown parameter:
# ----home", acme.sh's installer doubles a leading "--" when invoked this way): the installer's
# job is just putting the acme.sh SCRIPT on disk, which is stateless and fine at its default
# location (~/.acme.sh -> /root/.acme.sh, since this RUN executes as root). Where acme.sh keeps
# actual CERTIFICATE/ACCOUNT STATE is a completely separate, well-documented `--home` flag passed
# at RUNTIME on every real acme.sh invocation (see ACME_HOME in tls.py, under the persistent
# /data volume) — that one was never the problem.
RUN curl -fsSL https://get.acme.sh -o /tmp/acme-install.sh \
    && sh /tmp/acme-install.sh --nocron \
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
