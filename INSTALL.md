# Fiduciary — fresh install

A self-contained two-container stack: `portfolio-api` + `mcpo`. Ollama runs on the host,
and scheduling is built into `portfolio-api` (no separate scheduler container). See
`README.md` for what each piece does; this is the step-by-step.

## Files that matter

- `portfolio-api/` — the brain (dashboard, REST API, every agent, the data providers, the built-in chat, the job scheduler)
- `mcpo/config.json.example` — time + tavily (+ monarch only if you use it)
- `mcpo-secrets/mcpo.env.example` — REQUIRED before first up (2026-07-24); portfolio-api writes the
  real Monarch/Tavily credentials here once you set them from SETTINGS -> SECRETS
- `docker-compose.yml`, `.env.example`, `holdings.csv.example`, `snaptrade_register.py`, `setup.py`
- `docs/DEPENDENCIES.md`

## Install (in order)

```bash
# 1. Prereqs: Docker + Compose v2; NVIDIA Container Toolkit for GPU; Ollama on the host.

# 2. Clone + configure
git clone https://github.com/txAlphaMale/Local-AI-Fiduciary.git
cd Local-AI-Fiduciary
cp .env.example .env                          # fill SEC_USER_AGENT, MCPO_API_KEY, ANALYSIS_TOKEN
                                              # (+ provider vars, step 4) -- Finnhub/Tavily/Monarch
                                              # keys are better set from the dashboard after first
                                              # boot (SETTINGS -> SECRETS) than typed here, see below
cp mcpo/config.json.example mcpo/config.json  # REQUIRED before first up
#   ^ if you're NOT using Monarch, delete the "monarch" block from mcpo/config.json
cp mcpo-secrets/mcpo.env.example mcpo-secrets/mcpo.env  # ALSO REQUIRED before first up (2026-07-24)
#   ^ starts empty -- fill Monarch/Tavily creds from the dashboard once it's running, not here

# 3. Pull the models (on the host)
ollama pull qwen3.6:27b     # analysis + Strategist
ollama pull bge-m3          # embeddings for the knowledge RAG
```

**4. Pick your data source** — set `PORTFOLIO_PROVIDER`, either in `.env` before first boot, or
(2026-07-24) from the dashboard itself at **SETTINGS → ACCOUNTS → "Portfolio data source"**
once it's running (the change applies immediately — no restart):

| Provider | Steps |
|----------|-------|
| `csv` *(no account)* | **Easiest: use the in-app HOLDINGS tab** (2026-07-24) — enter positions in an editable table or upload a CSV, no host file access needed. The old manual path still works too: fill a `holdings.csv` from `holdings.csv.example` and put it where `HOLDINGS_CSV` points (default `/data/holdings.csv`, or bind-mount it in the compose). **Have debt (loans/credit cards)?** (2026-07-30) The same HOLDINGS tab has a separate LIABILITIES table below the positions editor — there's no CSV/file equivalent for this one, it's in-app only. Without it, `csv`/`snaptrade` accounts were always treated as debt-free. |
| `snaptrade` *(live, many brokers incl. Robinhood)* | Signup at snaptrade.com → set `SNAPTRADE_CLIENT_ID`/`CONSUMER_KEY`. A personal key (client ID starts `PERS-`) needs nothing else — connect brokerages in SETTINGS → ACCOUNTS → SnapTrade; a commercial key also needs `python3 snaptrade_register.py` to create `USER_ID`/`USER_SECRET`. Debt isn't synced by SnapTrade either — use the HOLDINGS tab's LIABILITIES table (see the `csv` row above) for any loan/credit card. |
| `monarch` *(unofficial, at your own risk — off by default)* | Uses Monarch's private API, which their terms restrict. Set `MONARCH_EMAIL`/`PASSWORD`/`MFA_SECRET`, then an admin must accept the risk acknowledgment under SETTINGS → ACCOUNTS → Monarch before it activates. |

```bash
# 5. Start the stack (portfolio-api + mcpo)
docker compose up -d --build

# 6. Automate what can be automated (validates config, pulls models)
python3 setup.py
```

**6. Scheduling — built in, nothing to install.** The recurring agents run on a scheduler
inside `portfolio-api`, enqueued into the same serial job queue as all heavy work. Default
times (Central by default — set `TZ` in `.env`): analysis daily 18:00, discovery daily
07:00, opportunity scan Sat 18:00, rebalance Sun 19:00, retirement 1st 08:00, strategist
Fri 17:00. Change them in the dashboard under **⚙ Settings → Task Schedule**, or disable the
scheduler entirely with `INTERNAL_SCHEDULER=0`. Every job can also be run on demand from the
dashboard / REST API.

## Verify

```bash
curl -s http://localhost:8097/health          # -> {"ok":true,...}
```
- Dashboard at http://localhost:8097 shows your positions (from your chosen provider).
- Open the **CHAT** tab and ask the Fiduciary "what's my net worth?" — it should call a
  tool and answer.

## HTTPS (recommended before exposing this beyond a trusted LAN)

By default the dashboard is plain HTTP on port 8097 — fine on a trusted home LAN, not fine
otherwise. Set it up from **SETTINGS → SECURITY → Certificate** in the dashboard itself, no
command-line work required. Three built-in methods:

- **Self-signed** — instant, zero prerequisites. Browsers warn until you trust the cert once.
- **Let's Encrypt / ACME** *(preferred, when available)* — a real, browser-trusted certificate
  that renews itself automatically. Needs either a public DNS name with port 80 reachable from
  the internet (HTTP-01), or your DNS provider's API credentials (DNS-01, no public
  reachability needed). If you'll use HTTP-01, uncomment the port-80 mapping in
  `docker-compose.yml` first.
- **Manual (CSR)** — the app generates a CSR you take to any CA (or an internal CA for a
  LAN-only deployment); paste the signed certificate back in.

Whichever you pick, the app restarts itself in place a couple seconds later to apply it — the
page will briefly go unresponsive, then reload at `https://<host>:8443` (the `HTTPS_PORT`
in `.env`, default 8443; the plain-HTTP port stops serving the app once HTTPS is active, but
keeps answering with a redirect to 8443 — see below). See the in-app WIKI (Getting started →
"HTTPS / certificates") for the full walkthrough of each method.

## Backups (recommended — one command, then forget about it)

`scripts/backup.sh` encrypts the entire `/data` volume (SQLite DBs, encryption key material, TLS
certs, config) into a single AES-256 archive; `scripts/restore.sh` reverses it, checksum-verified.
Both existed before this, but nothing scheduled them — you had to remember to run `backup.sh` by
hand. This installs a systemd timer that runs it every day automatically:

```bash
sudo ./scripts/install-local-backup.sh
```

You'll be prompted for a passphrase (store it in a password manager too — there is no recovery
without it) and the data volume's path (find it with
`docker volume inspect fiduciary_portfolio-data --format '{{ .Mountpoint }}'` if you don't already
know it). The passphrase is stored at `/etc/fiduciary-backup.env`, root-only (mode 600) — a
necessary tradeoff for unattended automation, kept deliberately OUTSIDE `/data` so a stolen data
volume alone can't self-decrypt its own backup.

```bash
systemctl list-timers fiduciary-backup.timer     # confirm it's scheduled
sudo systemctl start fiduciary-backup.service    # trigger one run right now instead of waiting
journalctl -u fiduciary-backup.service -n 50     # see the last run's output
```

**Do a real restore drill at least once** — a backup you've never restored is a guess, not a plan:

```bash
sudo BACKUP_PASSPHRASE=$(sudo grep BACKUP_PASSPHRASE /etc/fiduciary-backup.env | cut -d= -f2-) \
  ./scripts/restore.sh /var/backups/fiduciary/fiduciary-backup-<timestamp>.tar.gz.enc /tmp/restore-test
```

**What this does not do:** copy backups off this host. A backup sitting on the same disk as the
data it protects doesn't survive that disk/host being lost, stolen, or destroyed — only accidental
deletion, a bad upgrade, or corruption. For real disaster-recovery coverage, periodically copy
`/var/backups/fiduciary` (or wherever `BACKUP_OUTPUT_DIR` points) somewhere else — a NAS, an
external drive, or `rclone` to any cloud storage of your choice.

Once HTTPS is active, the old plain-HTTP port (8097 by default) doesn't go dark: it stays open
just to 307-redirect any request to the same address on `HTTPS_PORT`, so an old bookmark or
typed `http://` URL still gets you there automatically instead of a connection error.

> After the first run you can manage most config from the **⚙ Settings gear** (top-right):
> advanced knobs, model config, and a masked write-only editor for the app's own keys
> (Finnhub / SEC user-agent / SnapTrade / the AI-provider keys below). Anything already set
> in `.env` wins and shows read-only.
>
> Monarch and Tavily credentials (2026-07-24) are ALSO editable from the same SECRETS panel now
> — portfolio-api writes them to `mcpo-secrets/mcpo.env`, which the mcpo container reads the next
> time it's **recreated**: `docker compose up -d mcpo --force-recreate` (not `docker compose
> restart mcpo` — a plain restart reuses the container's existing environment and will NOT pick up
> the change). **`--force-recreate` is not optional here** (confirmed 2026-07-26, a real incident):
> mcpo has no build step (it's a prebuilt image, `ghcr.io/open-webui/mcpo:main`, same tag every
> time), so Compose's recreate-detection hash — which is computed from the resolved
> docker-compose.yml service definition, not the byte content of any `env_file:` — never notices
> that `mcpo.env` changed. A plain `docker compose up -d mcpo` will silently do nothing, leaving
> mcpo running on stale credentials until its cached Monarch session eventually expires and fails
> to re-authenticate. Only the mcpo API key itself stays `.env`-only (it's a startup CLI flag, not
> something a running container can pick up from a file).
>
> The same SECRETS/SETTINGS mechanism covers the optional Claude (Anthropic) provider added
> 2026-07-24 — no `.env` editing needed to enable it. See `NEW-USER-SETUP-GUIDE.md`'s "AI
> provider" section before turning it on (a claude.ai Pro/Max subscription does NOT cover this
> — it needs a separate, pay-per-token API key).
>
> **Values in the DB (SECRETS/SETTINGS panels) are encrypted at rest** (Fernet, via
> `portfolio-api/secrets_crypto.py`) — a key auto-generated at `/data/secrets.key` on first use,
> same bootstrap pattern as `SESSION_SECRET`. Back that key file up alongside your data volume;
> losing it makes every already-saved value permanently unrecoverable.
>
> **Migrating a value OFF `.env` and into the UI now takes exactly one container recreation:**
> you can save the new value from SETTINGS/SECRETS *while the old `.env` line is still active* —
> it's staged, not applied yet (the panel will say so). Then remove that line from `.env` and
> recreate the relevant service — **not** `docker compose restart`, which reuses the container's
> existing environment and won't see the edited `.env` at all. For portfolio-api,
> `docker compose up -d --build portfolio-api` is enough (the `--build` forces a recreate via the
> new image reference). For Monarch/Tavily specifically, recreate portfolio-api first (as above —
> its boot is what regenerates `mcpo-secrets/mcpo.env` with the now-unlocked value) — then mcpo
> with `docker compose up -d mcpo --force-recreate` (plain `up -d mcpo` is NOT enough — see above).
> The staged value activates on that one pass, no second recreation, no gap where the feature is
> briefly unconfigured.
