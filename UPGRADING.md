# Upgrading an existing install

For anyone who already runs Fiduciary from a clone of this repo. A new install should follow [INSTALL.md](INSTALL.md) instead.

**Short version:** pull, rebuild, restart. Your data, `.env` and settings are kept, and no manual data migration is needed (the app updates its own data files at start-up). Everything below the basic steps is optional.

## 1. Basic upgrade (about 5 minutes)

```bash
cd <your Fiduciary folder>
git pull
docker compose build --pull portfolio-api
docker compose up -d
```

- `--pull` refreshes the base image, which is where operating-system security fixes arrive. Do this on every upgrade.
- `up -d` (without a service name) also recreates `mcpo`: its image is now pinned by digest, so Docker fetches that exact image once.
- If you edited `docker-compose.yml` yourself, `git pull` may report a conflict. Keep your local changes (for example ports or volume paths) and merge in the new `image:` line for `mcpo`.
- Check it: `docker ps` shows both containers `Up`, and `portfolio-api` becomes `healthy`. Then open the dashboard, sign in, and open `/api/audit/verify` in the same browser: it should answer `{"ok":true, ...}`.

## 2. What you will notice

| Area | Change |
|---|---|
| Net worth chart | A single quiet line (daily, then weekly, then monthly as it gets older). Each Overview graph can be shown or hidden. |
| INVESTED tile | Excludes cash lines. Net worth and every planning/risk calculation are unchanged. |
| Cost basis | A combined row shows `n/r` plus "known for X of Y accounts" when only some accounts have a basis. |
| Usernames | `< >` and other markup characters are refused. Existing normal names are fine. |
| Audit log | Admins see attempted login names plus a "suspicious" flag; viewers see a stable token instead of the typed name. Two new alert rules: one IP trying many usernames, and markup-like login names. |
| Error messages | A server-side failure now says `internal error (ref xxxxxx)`. The full detail is in the log: `docker compose logs portfolio-api` (search for the ref). Validation messages are unchanged. |
| Backup passphrase | Saving it in SETTINGS > SECRETS applies immediately; the old "rebuild required" notice is gone. |
| Security | Third-party scripts are integrity-pinned; `acme.sh` and `mcpo` are pinned; CI now runs the test suite on every change. |

## 3. Optional: harden the container (Linux or WSL, data on the Linux disk)

Runs `portfolio-api` as your own user with a read-only filesystem, no Linux capabilities, and resource limits; `mcpo` also drops all capabilities. Default behaviour is unchanged unless you do this. Not for data kept on a Windows drive (`/mnt/c/...`), where file ownership does not map.

```bash
docker compose stop portfolio-api
docker run --rm -v <project>_portfolio-data:/data   # e.g. fiduciary_portfolio-data for a folder named Fiduciary -v "$PWD/backups:/b" alpine sh -c "chown -R $(id -u):$(id -u) /data /b"
```

Then add two lines to your `.env` (the volume name is `<folder name>_portfolio-data`; check with `docker volume ls`):

```
COMPOSE_FILE=docker-compose.yml:docker-compose.hardened.yml
APP_UID=<the output of: id -u>
```

and rebuild:

```bash
docker compose build --pull portfolio-api && docker compose up -d
```

(Plain `up -d`, not just `portfolio-api`, so `mcpo` is recreated with its new settings too.) Afterwards `docker compose exec portfolio-api id -u` should print your uid, not `0`.

To undo: remove those two `.env` lines and rebuild. Root can still read the data. Details are in the header of `docker-compose.hardened.yml`.

## 4. Optional: automatic daily backups

Today, backups happen only when you press RUN BACKUP NOW. To add a daily one:

```bash
sudo ./scripts/install-local-backup.sh
```

Accept the default data source. If Docker runs outside your Linux install (Docker Desktop with WSL), the script detects that and reads the volume through Docker. Use the same passphrase you saved in the app, and keep a copy in a password manager: there is no recovery without it. Test it once with `sudo systemctl start fiduciary-backup.service`, and try a restore to a scratch folder (`scripts/restore.sh`) before you rely on it.

## 5. If something goes wrong

- Container will not start after hardening: undo step 3 (remove the two `.env` lines, rebuild).
- Anything else: `git log` shows what you pulled; `git checkout <previous commit>` and `docker compose build portfolio-api && docker compose up -d` returns to the old version, since your data is not modified by the code update.
