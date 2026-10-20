# Pinned to -bookworm explicitly (2026-07-27), not the floating `python:3.12-slim` tag it used to
# be: a Trivy scan on 2026-07-27 found 202 vulnerabilities (5 CRITICAL, incl. perl-base CVE-2026-
# 13221 and socat CVE-2026-56123) with almost no "Fixed Version" available, and the build log's own
# `Detected OS family="debian" version="13.6"` line showed why -- the floating tag had silently
# resolved to Debian 13 "trixie" (very new stable release, Debian's security team hasn't backported
# most patches yet) instead of Debian 12 "bookworm" (years of active security patching). Pinning to
# `-bookworm` fixes both problems: an actively-patched base AND a base that can't silently shift OS
# releases out from under this Dockerfile again on a future `docker build` with no cache. Jason:
# rebuild + rescan (`docker build -t fiduciary-portfolio-api:scan .` then `trivy image ... fiduciary-
# portfolio-api:scan`) to confirm this actually clears the 2 CRITICAL findings above -- not verified
# against a real Trivy run from this session (no Docker available in the sandbox).
FROM python:3.15-slim-bookworm
WORKDIR /app

# curl: fetches acme.sh below. tar: extracts it. socat: acme.sh's --standalone HTTP-01 challenge
# listener needs it. All three are also needed at RUNTIME (acme.sh shells out to them), not just
# at build time, so they stay installed rather than being removed after the install step.
RUN apt-get update && apt-get install -y --no-install-recommends curl tar socat ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Vendored acme.sh (2026-07-23f) — the built-in Let's Encrypt / ACME client tls.py shells out to.
# Third fix, 2026-07-23h — a real deploy walked through 2 prior attempts that both failed on the
# convenience `curl get.acme.sh | sh -s <flags>` one-liner: it turns out THAT wrapper script
# doubles a leading "--" on ANY flag forwarded through it this way ("--home" and separately
# "--nocron" both broke with "Unknown parameter: ----X"), and even with zero flags it then hard-
# refuses to install without a `cron` package present ("Please add '--force' and try install
# again... ./acme.sh --install --force" — its own error message, which is quoting acme.sh's REAL
# install script's OWN argument syntax, not the wrapper's). So: skip the flaky get.acme.sh wrapper
# entirely and do what it does internally, ourselves — fetch the same GitHub source tarball
# directly and invoke the real ./acme.sh --install with real flags, matching exactly the command
# acme.sh's own error output confirmed works. --force: no cron package in this slim image, and
# none needed — renewal is driven by tls.py's own background thread (app.py's
# tls.start_renewal_thread()), not a crontab entry.
RUN curl -fsSL https://github.com/acmesh-official/acme.sh/archive/master.tar.gz -o /tmp/acme.tar.gz \
    && mkdir -p /tmp/acme-src && tar -xzf /tmp/acme.tar.gz -C /tmp/acme-src --strip-components=1 \
    && (cd /tmp/acme-src && ./acme.sh --install --force) \
    && rm -rf /tmp/acme.tar.gz /tmp/acme-src

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# 8097 = plain HTTP (default, no certificate configured yet). 8443 = HTTPS, once one of the 3
# in-app certificate methods (SETTINGS -> SECURITY -> Certificate) is set up — run_server.py
# picks whichever applies at every (re)start, see tls.py. Once HTTPS is active, 8097 stops
# serving the app itself but stays open as a 307 redirect to the same path on 8443 (2026-07-24),
# so an old bookmark/typed URL on 8097 lands you somewhere instead of going dark.
EXPOSE 8097 8443
CMD ["python", "run_server.py"]
