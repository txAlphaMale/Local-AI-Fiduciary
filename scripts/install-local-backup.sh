#!/usr/bin/env bash
# install-local-backup.sh -- SEC-7a (Backup & Disaster Recovery), local/self-hosted deployment.
#
# WHAT THIS FIXES: scripts/backup.sh and restore.sh have existed since 2026-07-25 and were
# evidenced (a real backup->restore round-trip, byte-for-byte diff-verified) -- but nothing
# actually SCHEDULED backup.sh to run. It was a script you had to remember to run by hand. This
# installs a systemd timer that runs it automatically, every day, with no further attention needed.
#
# WHY SYSTEMD TIMERS INSTEAD OF A RAW CRONTAB LINE: survives reboots without re-adding anything,
# logs every run to the journal (`journalctl -u fiduciary-backup`) instead of silently succeeding/
# failing into /dev/null the way cron does by default, and `Persistent=true` below means a run
# that was missed because the host was off gets caught up on next boot instead of silently skipped
# until the next scheduled time. Modern Ubuntu/Debian/Fedora all ship systemd, so this has no extra
# dependency beyond what's already on the host.
#
# USAGE (run ONCE, as root, on the host where the portfolio-api data volume actually lives):
#   sudo ./scripts/install-local-backup.sh
# You'll be prompted for a BACKUP_PASSPHRASE (or set one non-interactively: see below) and the
# data volume path. Safe to re-run -- it overwrites its own unit files idempotently and will not
# duplicate the timer.
#
# NON-INTERACTIVE (e.g. scripted provisioning): set these three before running --
#   BACKUP_PASSPHRASE=...        (required -- same passphrase restore.sh will need later)
#   BACKUP_SOURCE_DIR=/var/lib/docker/volumes/fiduciary_portfolio-data/_data   (default shown)
#   BACKUP_RETENTION_DAYS=30     (optional -- 0/unset keeps every backup forever)
#
# WHERE THE PASSPHRASE ACTUALLY LIVES: /etc/fiduciary-backup.env, root-owned, mode 600 -- readable
# only by root (the systemd service also runs as root, since it needs to read the Docker volume
# path under /var/lib/docker). This is a deliberate, unavoidable tradeoff for UNATTENDED automation:
# backup.sh's own header explains why the passphrase must live OUTSIDE /data (so a stolen /data
# volume alone can't self-decrypt its own backup) -- this file satisfies that by being a completely
# separate path, but it is still a secret sitting on this host's disk. If that's not an acceptable
# tradeoff for your threat model, don't run this installer -- keep running backup.sh manually with
# the passphrase typed in each time instead.
#
# WHAT THIS DOES NOT DO: copy the resulting .tar.gz.enc files anywhere OFF this host. A backup that
# lives on the same disk as the thing it's backing up does not protect against that disk/host being
# lost, stolen, or destroyed -- only against accidental deletion, a bad upgrade, or data corruption.
# For real disaster-recovery coverage, periodically copy BACKUP_OUTPUT_DIR somewhere else (a NAS,
# an external drive, rclone to any cloud storage) -- deliberately not built here since it depends
# entirely on what off-site storage you actually have, which this script has no way to know.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run this as root (sudo ./scripts/install-local-backup.sh) -- systemd system units" >&2
  echo "and /etc/fiduciary-backup.env both need root to install." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SH="$SCRIPT_DIR/backup.sh"
if [ ! -x "$BACKUP_SH" ]; then
  # Tolerate a non-executable checkout (git doesn't always preserve the bit across platforms).
  chmod +x "$BACKUP_SH" 2>/dev/null || true
fi
[ -f "$BACKUP_SH" ] || { echo "ERROR: $BACKUP_SH not found." >&2; exit 1; }

ENV_FILE="/etc/fiduciary-backup.env"
DEFAULT_SRC="/var/lib/docker/volumes/fiduciary_portfolio-data/_data"
DEFAULT_OUT="/var/backups/fiduciary"

SRC="${BACKUP_SOURCE_DIR:-}"
OUT="${BACKUP_OUTPUT_DIR:-$DEFAULT_OUT}"
RETENTION="${BACKUP_RETENTION_DAYS:-0}"
PASS="${BACKUP_PASSPHRASE:-}"

if [ -z "$SRC" ]; then
  read -r -p "Data volume path to back up [$DEFAULT_SRC]: " SRC
  SRC="${SRC:-$DEFAULT_SRC}"
fi
if [ ! -d "$SRC" ]; then
  echo "ERROR: '$SRC' does not exist. Find the real path with:" >&2
  echo "  docker volume inspect fiduciary_portfolio-data --format '{{ .Mountpoint }}'" >&2
  exit 1
fi

if [ -z "$PASS" ]; then
  if [ -f "$ENV_FILE" ] && grep -q '^BACKUP_PASSPHRASE=' "$ENV_FILE" 2>/dev/null; then
    echo "Re-using the existing passphrase already in $ENV_FILE (unchanged)."
  else
    read -r -s -p "Choose a BACKUP_PASSPHRASE (store this in a password manager too -- there is no recovery without it): " PASS
    echo
    [ -z "$PASS" ] && { echo "ERROR: an empty passphrase is not allowed." >&2; exit 1; }
  fi
fi

mkdir -p "$OUT"

# --- Write the env file (root-only) -- only overwrite the passphrase line if we actually have a
# new one, so re-running this installer to change SRC/OUT/RETENTION doesn't force a new passphrase.
if [ -n "$PASS" ]; then
  umask 077
  cat > "$ENV_FILE" <<ENVEOF
# fiduciary-backup.env -- root-only (mode 600). Written by install-local-backup.sh on $(date -u +%FT%TZ).
# BACKUP_PASSPHRASE is the ONLY thing that decrypts your fiduciary backups -- also store it in a
# password manager. Losing this file AND your password-manager copy makes every backup permanently
# unrecoverable, same as losing secrets.key itself (see the "Secrets encryption" Wiki entry).
BACKUP_PASSPHRASE=$PASS
BACKUP_SOURCE_DIR=$SRC
BACKUP_OUTPUT_DIR=$OUT
BACKUP_RETENTION_DAYS=$RETENTION
ENVEOF
  chmod 600 "$ENV_FILE"
  chown root:root "$ENV_FILE"
fi

# --- systemd unit + timer (idempotent overwrite) ---
cat > /etc/systemd/system/fiduciary-backup.service <<SERVICEEOF
[Unit]
Description=Fiduciary encrypted data backup (SEC-7a)
After=docker.service

[Service]
Type=oneshot
EnvironmentFile=$ENV_FILE
ExecStart=$BACKUP_SH
# Belt-and-suspenders: backup.sh already refuses to write unencrypted, but keep this unit from
# ever running as anyone other than root, since reading a Docker volume under /var/lib/docker
# normally requires it.
User=root
SERVICEEOF

cat > /etc/systemd/system/fiduciary-backup.timer <<TIMEREOF
[Unit]
Description=Run the Fiduciary encrypted backup daily (SEC-7a)

[Timer]
OnCalendar=daily
# Missed a run because the host was off at the scheduled time? Catch up at next boot instead of
# silently waiting for tomorrow's slot -- the whole point of an RPO is not depending on the host
# happening to be up at exactly 00:00.
Persistent=true
# Small random spread so a fleet of these (not that you have one) wouldn't all fire at once --
# harmless for a single host, costs nothing to leave in.
RandomizedDelaySec=15min

[Install]
WantedBy=timers.target
TIMEREOF

systemctl daemon-reload
systemctl enable --now fiduciary-backup.timer

echo
echo "Installed. Backups will run daily (systemd decides the exact time; see 'next' below) into: $OUT"
[ "$RETENTION" != "0" ] && echo "Older backups auto-pruned after $RETENTION days." || echo "Retention: keeping every backup (set BACKUP_RETENTION_DAYS to change)."
echo
echo "Verify:"
echo "  systemctl list-timers fiduciary-backup.timer     # confirm it's scheduled, see 'next' run"
echo "  sudo systemctl start fiduciary-backup.service     # trigger one run right now, don't wait"
echo "  journalctl -u fiduciary-backup.service -n 50      # see the last run's output"
echo
echo "IMPORTANT: do a real restore drill at least once -- a backup you've never restored is a"
echo "guess, not a plan. Example:"
echo "  sudo BACKUP_PASSPHRASE=\$(sudo grep BACKUP_PASSPHRASE $ENV_FILE | cut -d= -f2-) \\"
echo "    $SCRIPT_DIR/restore.sh $OUT/fiduciary-backup-<timestamp>.tar.gz.enc /tmp/restore-test"
