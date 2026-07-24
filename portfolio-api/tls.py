"""
tls.py — in-app HTTPS certificate management (LOCAL-HOSTING-HARDENING-PLAN.md's HTTPS note,
2026-07-23d, expanded 2026-07-23f/g). Three methods, all built-in — no "read the docs and run
certbot yourself" required:

  1. self-signed  — instant, zero external dependency, zero prerequisites. Browsers show a
                     trust warning until the cert is manually trusted, but it's the only path
                     that works with nothing else set up (no public DNS, no port forwarding, no
                     CA account) — the right quick-start default.
  2. acme         — Let's Encrypt (or any ACME CA) via the vendored `acme.sh` client. HTTP-01
                     (acme.sh's own --standalone challenge listener, needs port 80 reachable from
                     the internet — a real public DNS name pointed at this host) or DNS-01 (no
                     public reachability needed, but needs API credentials for whichever DNS
                     provider hook acme.sh should drive). Auto-renews on a background timer.
                     PREFERRED when available — a real, browser-trusted certificate with zero
                     ongoing manual work.
  3. manual (CSR) — the app generates a private key + CSR; the user takes the CSR to any CA
                     (or an internal/self-signed CA for a LAN-only deployment) and pastes the
                     signed certificate back in. The private key never leaves this server.

All three converge on the SAME two files under TLS_DIR: cert.pem (leaf + any chain) and key.pem
(private key, 0600). `run_server.py` (the container's entrypoint) picks these up at launch and
decides plain-HTTP vs. HTTPS from their presence — nothing in THIS module talks to uvicorn
directly. `restart_to_apply()` re-execs run_server.py in place (same PID, so Docker's restart
policy never sees an exit) so a newly generated/renewed/imported cert takes effect within a
couple seconds, with no separate manual restart step.

Metadata (which method produced the active cert, the ACME domain/challenge/DNS provider, the
auto-renew flag) is stored via store.py under TLS_META_FILE — deliberately NOT derived from the
cert file itself, since status() always reads the actual served PEM directly off disk for
anything the X.509 structure already encodes (SANs, issuer, validity window), so status can never
claim something that isn't what's actually being served.

Known limitation, stated plainly: this module can be and IS fully unit-tested for the self-signed
and manual-CSR paths (pure `cryptography` operations, no network). The ACME path's *orchestration
logic* (argument building, output parsing, error handling) is tested against a stubbed acme.sh;
an actual live Let's Encrypt issuance requires a real public domain + port 80 reachable from the
internet, which no local dev/sandbox environment can provide — that leg can only be verified on
the owner's real deployment.
"""
import os
import sys
import time
import shutil
import subprocess
import threading
import ipaddress
import datetime as _dt

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import store

TLS_DIR = os.environ.get("TLS_DIR", "/data/tls")
CERT_PATH = os.path.join(TLS_DIR, "cert.pem")
KEY_PATH = os.path.join(TLS_DIR, "key.pem")
PENDING_KEY_PATH = os.path.join(TLS_DIR, "pending_csr_key.pem")
PENDING_CSR_PATH = os.path.join(TLS_DIR, "pending.csr")
TLS_META_FILE = os.environ.get("TLS_META_FILE", "/data/tls_meta.json")

ACME_SH = os.environ.get("ACME_SH_PATH", "/opt/acme.sh/acme.sh")
ACME_HOME = os.environ.get("ACME_HOME", "/data/acme")
ACME_EMAIL = os.environ.get("ACME_EMAIL", "")
ACME_STAGING = os.environ.get("ACME_STAGING", "0").lower() in ("1", "true", "yes", "on")

RENEWAL_CHECK_INTERVAL_S = int(os.environ.get("TLS_RENEWAL_CHECK_INTERVAL_S", str(12 * 3600)))
RENEWAL_THRESHOLD_DAYS = int(os.environ.get("TLS_RENEWAL_THRESHOLD_DAYS", "30"))


class AcmeError(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# metadata + shared file plumbing
# ---------------------------------------------------------------------------------------------
def _ensure_dir():
    os.makedirs(TLS_DIR, exist_ok=True)


def _read_meta():
    try:
        return store.read_json_strict(TLS_META_FILE) or {}
    except Exception:
        return {}


def _write_meta(patch):
    """Merges `patch` into the existing metadata (doesn't clobber unrelated keys)."""
    meta = _read_meta()
    meta.update(patch)
    store.write_json(TLS_META_FILE, meta)
    return meta


def has_active_cert():
    return os.path.isfile(CERT_PATH) and os.path.isfile(KEY_PATH)


def _write_active(key_pem, cert_pem, chain_pem=None):
    """Atomic-ish install: write to .tmp then os.replace, so a mid-write crash can never leave
    uvicorn pointed at a half-written cert or key."""
    _ensure_dir()
    full_cert = cert_pem + (b"\n" + chain_pem if chain_pem else b"")
    tmp_key, tmp_cert = KEY_PATH + ".tmp", CERT_PATH + ".tmp"
    with open(tmp_key, "wb") as f:
        f.write(key_pem)
    os.chmod(tmp_key, 0o600)
    with open(tmp_cert, "wb") as f:
        f.write(full_cert)
    os.replace(tmp_key, KEY_PATH)
    os.replace(tmp_cert, CERT_PATH)


def _clear_pending():
    for p in (PENDING_KEY_PATH, PENDING_CSR_PATH):
        try:
            os.remove(p)
        except OSError:
            pass


def mark_applied():
    """Called once at app startup (see app.py). Whatever cert exists on disk right NOW is, by
    definition, what run_server.py just chose to serve for this process — so any earlier
    'restart_required' flag is stale the moment we're actually running. Doesn't touch anything
    else in the metadata."""
    meta = _read_meta()
    if meta.get("restart_required"):
        _write_meta({"restart_required": False})


# ---------------------------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------------------------
def _load_cert():
    with open(CERT_PATH, "rb") as f:
        return x509.load_pem_x509_certificate(f.read())


def _not_before_after(cert):
    # cryptography >=42 exposes *_utc properties; older versions only have the naive ones.
    nb = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before.replace(tzinfo=_dt.timezone.utc)
    na = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(tzinfo=_dt.timezone.utc)
    return nb, na


def _san_list(cert):
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return []
    out = []
    for gn in ext.value:
        if isinstance(gn, x509.DNSName):
            out.append(gn.value)
        elif isinstance(gn, x509.IPAddress):
            out.append(str(gn.value))
    return out


def status():
    """Current certificate status for SETTINGS -> SECURITY -> Certificate. Everything X.509
    already encodes (SANs, issuer, validity) is read straight off the live PEM on disk, never
    from our own metadata — so this can never report a state that isn't what's actually
    installed, even if the metadata write for some reason didn't happen/got out of sync."""
    meta = _read_meta()
    out = {
        "active": has_active_cert(),
        "method": meta.get("method"),
        "auto_renew": bool(meta.get("auto_renew")),
        "domain": meta.get("domain"),
        "challenge": meta.get("challenge"),
        "dns_provider": meta.get("dns_provider"),
        "staging": bool(meta.get("staging")),
        "pending_csr": os.path.isfile(PENDING_CSR_PATH),
        "restart_required": bool(meta.get("restart_required")),
        "acme_available": acme_available(),
        "note": ("HTTPS is served once a certificate exists AND the server has (re)started to "
                 "pick it up. That restart is automatic — the app restarts itself in place a "
                 "couple seconds after installing a new/renewed/imported certificate, so no "
                 "separate manual step is needed; only in-flight requests at that instant are "
                 "interrupted."),
    }
    if not out["active"]:
        return out
    try:
        cert = _load_cert()
        not_before, not_after = _not_before_after(cert)
        now = _dt.datetime.now(_dt.timezone.utc)
        days_remaining = (not_after - now).days
        cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        out.update({
            "common_name": cn_attrs[0].value if cn_attrs else None,
            "issuer": cert.issuer.rfc4514_string(),
            "sans": _san_list(cert),
            "issued_at": int(not_before.timestamp()),
            "expires_at": int(not_after.timestamp()),
            "days_remaining": days_remaining,
            "expired": days_remaining < 0,
            "self_issued": (cert.issuer == cert.subject),
        })
    except Exception as e:
        out["error"] = f"could not read the installed certificate: {e}"
    return out


# ---------------------------------------------------------------------------------------------
# 1. self-signed
# ---------------------------------------------------------------------------------------------
def _san_entries(hosts):
    entries = []
    for h in hosts:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            entries.append(x509.DNSName(h))
    return entries


def generate_self_signed(hostnames, days=825):
    """Generates a fresh private key + self-signed X.509 leaf covering every name/IP in
    `hostnames` (first entry doubles as the subject CN) and installs it as the active cert,
    replacing whatever was there before. 825 days (~2.25y) is a reasonable default validity for
    a self-signed cert — nothing enforces the CA/Browser Forum's public-CA lifetime limits on a
    self-signed leaf, but keeping it bounded avoids it silently living forever unnoticed."""
    if not hostnames:
        raise ValueError("at least one hostname or IP is required")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0])])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(minutes=5))
            .not_valid_after(now + _dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(_san_entries(hostnames)), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    _write_active(key_pem=key_pem, cert_pem=cert_pem)
    _clear_pending()
    store.write_json(TLS_META_FILE, {"method": "self_signed", "auto_renew": False,
                                      "domain": hostnames[0], "hostnames": hostnames,
                                      "created_at": int(time.time()), "restart_required": True})
    restart_to_apply()
    return status()


# ---------------------------------------------------------------------------------------------
# 3. manual — CSR generated here, signed certificate imported back
# ---------------------------------------------------------------------------------------------
def generate_csr(common_name, sans=None):
    """Generates a NEW private key + CSR for `common_name` (plus any extra SAN entries) and
    stashes the key as 'pending' under TLS_DIR — it never leaves this server. Returns the CSR as
    PEM text for the user to submit to any CA. Call import_signed_certificate() with the CA's
    response to finish (or generate_self_signed()/acme_enroll() instead, which discard any
    pending CSR — only one certificate request is "in flight" at a time)."""
    common_name = (common_name or "").strip()
    if not common_name:
        raise ValueError("common_name is required")
    sans = [s.strip() for s in (sans or []) if s and s.strip()]
    if common_name not in sans:
        sans = [common_name] + sans
    _ensure_dir()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
           .add_extension(x509.SubjectAlternativeName(_san_entries(sans)), critical=False)
           .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)
    with open(PENDING_KEY_PATH, "wb") as f:
        f.write(key_pem)
    os.chmod(PENDING_KEY_PATH, 0o600)
    with open(PENDING_CSR_PATH, "wb") as f:
        f.write(csr_pem)
    return csr_pem.decode()


def import_signed_certificate(cert_pem, chain_pem=None):
    """Pairs a CA-signed certificate with the pending CSR's private key — validated by comparing
    public-key numbers, so a mismatched/wrong certificate is rejected with a clear error rather
    than silently installing a cert nothing has the matching key for — and installs it as the
    active cert."""
    if not os.path.isfile(PENDING_KEY_PATH):
        raise ValueError("no pending CSR on file — call generate_csr() first")
    cert_bytes = cert_pem.encode() if isinstance(cert_pem, str) else cert_pem
    cert = x509.load_pem_x509_certificate(cert_bytes)
    with open(PENDING_KEY_PATH, "rb") as f:
        key_pem = f.read()
    key = serialization.load_pem_private_key(key_pem, password=None)
    if cert.public_key().public_numbers() != key.public_key().public_numbers():
        raise ValueError("this certificate's public key does not match the pending CSR's "
                          "private key — make sure you're importing the certificate that was "
                          "actually issued against the CSR generate_csr() produced")
    chain_bytes = None
    if chain_pem:
        chain_bytes = chain_pem.encode() if isinstance(chain_pem, str) else chain_pem
    _write_active(key_pem=key_pem, cert_pem=cert_bytes, chain_pem=chain_bytes)
    cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    _clear_pending()
    store.write_json(TLS_META_FILE, {"method": "manual", "auto_renew": False,
                                      "domain": cn_attrs[0].value if cn_attrs else None,
                                      "created_at": int(time.time()), "restart_required": True})
    restart_to_apply()
    return status()


# ---------------------------------------------------------------------------------------------
# 2. ACME (Let's Encrypt, or any ACME CA) via vendored acme.sh
# ---------------------------------------------------------------------------------------------
def acme_available():
    return os.path.isfile(ACME_SH) and os.access(ACME_SH, os.X_OK)


def _acme_install(domain, challenge, dns_provider, staging):
    """Copies acme.sh's issued key/cert OUT of its own directory layout and into TLS_DIR (acme.sh
    keeps its own copy under ACME_HOME/<domain>/ and rewrites it in place on renewal — pulling a
    COPY means uvicorn's view is stable and doesn't depend on acme.sh's internal layout surviving
    a future acme.sh upgrade)."""
    src_dir = os.path.join(ACME_HOME, domain)
    key_src = os.path.join(src_dir, f"{domain}.key")
    cert_src = os.path.join(src_dir, f"{domain}.cer")
    chain_src = os.path.join(src_dir, "fullchain.cer")
    if not (os.path.isfile(key_src) and os.path.isfile(cert_src)):
        raise AcmeError(f"acme.sh reported success but the expected output files are missing "
                         f"under {src_dir} — check the acme.sh log under {ACME_HOME}")
    with open(key_src, "rb") as f:
        key_pem = f.read()
    if os.path.isfile(chain_src):
        with open(chain_src, "rb") as f:
            cert_pem = f.read()      # fullchain already includes leaf + intermediate(s)
    else:
        with open(cert_src, "rb") as f:
            cert_pem = f.read()
    _write_active(key_pem=key_pem, cert_pem=cert_pem)
    _clear_pending()
    store.write_json(TLS_META_FILE, {"method": "acme", "auto_renew": True, "domain": domain,
                                      "challenge": challenge, "dns_provider": dns_provider,
                                      "staging": bool(staging), "created_at": int(time.time()),
                                      "restart_required": True})
    restart_to_apply()
    return status()


def acme_enroll(domain, challenge="http-01", dns_provider=None, dns_creds=None,
                 extra_domains=None, staging=None):
    """Requests a Let's Encrypt (or whatever ACME CA acme.sh is configured for) certificate for
    `domain` [+ extra_domains as additional SANs]. challenge='http-01' uses acme.sh's own
    --standalone listener (briefly binds port 80 for the challenge only — needs a real public
    DNS name pointed at this host and port 80 reachable from the internet). challenge='dns-01'
    needs no public reachability at all, but needs `dns_provider` (acme.sh's own hook name, e.g.
    'dns_cf' for Cloudflare — see https://github.com/acmesh-official/acme.sh/wiki/dnsapi for the
    full list and each provider's required credential variable names) and `dns_creds` (a
    {ENV_VAR_NAME: value} dict of exactly those variables). Credentials are exported only into
    this one subprocess call's environment, never written by US anywhere — acme.sh persists what
    IT needs for renewal in its own account.conf under ACME_HOME (which lives under /data, so it
    survives restarts), which is why acme_renew() below doesn't need dns_creds passed again."""
    if not acme_available():
        raise AcmeError(f"acme.sh not found at {ACME_SH} — was it installed in this image? "
                         f"(self-signed and manual-CSR certificates don't need it and still work)")
    domain = (domain or "").strip()
    if not domain:
        raise ValueError("domain is required")
    _ensure_dir()
    os.makedirs(ACME_HOME, exist_ok=True)
    try:
        os.chmod(ACME_HOME, 0o700)   # holds DNS-provider API credentials in account.conf
    except OSError:
        pass
    staging = ACME_STAGING if staging is None else bool(staging)
    domains = [domain] + [d.strip() for d in (extra_domains or []) if d and d.strip() and d != domain]

    cmd = [ACME_SH, "--home", ACME_HOME, "--issue"]
    for d in domains:
        cmd += ["-d", d]
    if challenge == "dns-01":
        if not dns_provider:
            raise ValueError("dns_provider is required for dns-01")
        cmd += ["--dns", dns_provider]
    else:
        challenge = "http-01"
        cmd += ["--standalone", "--httpport", "80"]
    if staging:
        cmd += ["--staging"]
    if ACME_EMAIL:
        cmd += ["--accountemail", ACME_EMAIL]

    env = dict(os.environ)
    if dns_creds:
        for k, v in dns_creds.items():
            env[str(k)] = str(v)

    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        raise AcmeError(f"could not execute {ACME_SH} — is it present and executable in this image?")
    except subprocess.TimeoutExpired:
        raise AcmeError("acme.sh did not finish within 300s (DNS/HTTP challenge may be stuck)")
    # acme.sh exits 0 on a fresh issue, 2 when the existing cert is already valid/up to date —
    # both mean "there is now a good certificate", the only two we treat as success.
    if proc.returncode not in (0, 2):
        raise AcmeError(f"acme.sh --issue failed (exit {proc.returncode}): "
                         f"{(proc.stderr or proc.stdout or '').strip()[-2000:]}")
    return _acme_install(domain, challenge, dns_provider, staging)


def acme_renew(domain=None):
    """Forces acme.sh to renew (or issue, if somehow missing) the certificate for the currently
    configured ACME domain. Called by the background thread when days_remaining drops to/below
    RENEWAL_THRESHOLD_DAYS, or on demand from the UI's 'Renew now' button. DNS-01 credentials
    are NOT needed here — acme.sh already persisted what it needs in its own account.conf from
    the original acme_enroll() call."""
    meta = _read_meta()
    if not domain:
        # Only fall back to the stored domain if the CURRENTLY active method is actually ACME —
        # `domain` is a shared metadata field across all 3 methods (self-signed/manual also set
        # it, for display), so a self-signed or manual cert installed AFTER a prior ACME one
        # would otherwise leave a stale domain here that was never actually ACME-issued.
        if meta.get("method") != "acme":
            raise AcmeError("no ACME domain on record — run acme_enroll() first")
        domain = (meta.get("domain") or "").strip()
    domain = domain.strip()
    if not domain:
        raise AcmeError("no ACME domain on record — run acme_enroll() first")
    if not acme_available():
        raise AcmeError(f"acme.sh not found at {ACME_SH}")
    cmd = [ACME_SH, "--home", ACME_HOME, "--renew", "-d", domain, "--force"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise AcmeError("acme.sh --renew did not finish within 300s")
    if proc.returncode not in (0, 2):
        raise AcmeError(f"acme.sh --renew failed (exit {proc.returncode}): "
                         f"{(proc.stderr or proc.stdout or '').strip()[-2000:]}")
    return _acme_install(domain, meta.get("challenge"), meta.get("dns_provider"), meta.get("staging"))


def set_auto_renew(enabled):
    _write_meta({"auto_renew": bool(enabled)})
    return status()


# ---------------------------------------------------------------------------------------------
# background renewal check + apply-by-restart
# ---------------------------------------------------------------------------------------------
_renew_thread_started = False
_renew_thread_lock = threading.Lock()


def start_renewal_thread():
    """Idempotent — safe to call more than once (only the first call actually starts the
    thread). A plain periodic poll rather than folding into app.py's GPU-serialized job queue:
    renewal is a cheap, non-GPU, non-time-of-day check that has nothing in common with that
    queue's daily/weekly analysis jobs."""
    global _renew_thread_started
    with _renew_thread_lock:
        if _renew_thread_started:
            return
        _renew_thread_started = True
    threading.Thread(target=_renewal_loop, daemon=True, name="tls-renewal").start()


def _renewal_loop():
    while True:
        try:
            renew_if_due()
        except Exception:
            pass   # best-effort — a failed background renewal attempt shouldn't crash the app;
                   # the certificate just stays as-is and the next periodic check tries again
        time.sleep(RENEWAL_CHECK_INTERVAL_S)


def renew_if_due():
    meta = _read_meta()
    if meta.get("method") != "acme" or not meta.get("auto_renew"):
        return None
    st = status()
    dr = st.get("days_remaining")
    if st.get("active") and dr is not None and dr <= RENEWAL_THRESHOLD_DAYS:
        return acme_renew()
    return None


def clear_active_cert():
    """Removes the active cert/key — the next (self-triggered) restart serves plain HTTP again.
    Doesn't touch acme.sh's own state under ACME_HOME, so re-enrolling later doesn't start over."""
    for p in (CERT_PATH, KEY_PATH):
        try:
            os.remove(p)
        except OSError:
            pass
    _write_meta({"method": None, "auto_renew": False, "restart_required": True})
    restart_to_apply()
    return status()


def restart_to_apply(delay_s=2):
    """Restarts THIS process in place (os.execv replaces the process image but keeps the same
    PID, so Docker never sees an exit and its restart policy never triggers) so uvicorn picks up
    a new/changed certificate. Runs after a short delay on a background thread so the HTTP
    response confirming the change reaches the browser first. Re-execs run_server.py itself
    (not uvicorn directly) so the plain-HTTP-vs-HTTPS decision gets freshly re-derived from
    whatever's on disk NOW, rather than reusing stale argv from container boot."""
    def _go():
        time.sleep(delay_s)
        launcher = os.environ.get(
            "TLS_LAUNCHER_PATH",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_server.py"))
        os.execv(sys.executable, [sys.executable, launcher])
    threading.Thread(target=_go, daemon=True).start()
