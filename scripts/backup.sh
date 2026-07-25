#!/usr/bin/env bash
# backup.sh -- SEC-7: encrypted backup of the portfolio-api data volume.
#
# Controls: ISO/IEC 27001:2022 A.8.13 (Information backup) · NIST SP 800-53 CP-9/CP-10 ·
# SOC 2 (2017 TSC) A1.2/CC7.1.
#
# WHAT THIS BACKS UP: the ENTIRE /data volume as one archive -- SQLite databases (store, auth,
# audit, chat), the encryption key material (secrets.key, secrets_keyring.json, audit_hmac.key),
# TLS certs/keys, and every JSON config file. Backing up the key files ALONGSIDE the data they
# protect is intentional and necessary (a backup missing the key is useless -- it can't be
# decrypted), which is exactly why this script encrypts the WHOLE archive with a SEPARATE
# passphrase that must never itself live inside /data (see "IMPORTANT" below).
#
# USAGE:
#   Against a live Docker volume (typical real deployment):
#     docker run --rm -v portfolio-data:/data -v "$(pwd)/backups:/backup" -w /data \
#       alpine sh -c "tar czf - ." | \
#       BACKUP_PASSPHRASE=... ./scripts/backup.sh --stdin-tar
#   ...or simpler, if this script is run ON THE HOST where the volume is bind-mounted/reachable:
#     BACKUP_PASSPHRASE=... ./scripts/backup.sh /var/lib/docker/volumes/fiduciary_portfolio-data/_data
#   Against a plain directory (also how this script is tested in CI/sandbox, no Docker needed):
#     BACKUP_PASSPHRASE=... ./scripts/backup.sh /path/to/data/dir
#
# OUTPUT: BACKUP_OUTPUT_DIR/fiduciary-backup-<UTC timestamp>.tar.gz.enc (AES-256-CBC, PBKDF2,
# 300000 iterations, random salt -- openssl enc, no external dependency beyond openssl itself)
# plus a .sha256 sidecar of the ENCRYPTED file (integrity check on the backup artifact itself,
# doesn't leak plaintext content since it's a hash of ciphertext).
#
# IMPORTANT -- BACKUP_PASSPHRASE must NOT be stored inside /data (that would defeat the point:
# anyone who gets the backup archive would also need the passphrase, and if the passphrase lived
# alongside the very key files it's meant to protect, a single stolen archive would be
# self-decrypting). Store it in a password manager or a secrets manager separate from this host.
# RPO/RTO are documented in compliance/DATA-RETENTION-SCHEDULE.md and RESUME-TRACKER.md's SEC-7
# entry -- summary: run this daily via host cron (RPO ~24h), restore.sh completes in well under
# a minute for this app's realistic data size (RTO ~minutes, evidenced by the restore test run
# alongside this script's build).

set -euo pipefail

SRC="${1:-${BACKUP_SOURCE_DIR:-/data}}"
OUT="${BACKUP_OUTPUT_DIR:-./backups}"
PASS="${BACKUP_PASSPHRASE:-}"

if [ -z "$PASS" ]; then
  echo "ERROR: BACKUP_PASSPHRASE is not set. Refusing to write an unencrypted backup." >&2
  exit 1
fi
if [ ! -d "$SRC" ]; then
  echo "ERROR: source directory '$SRC' does not exist or is not reachable." >&2
  exit 1
fi

mkdir -p "$OUT"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
enc_path="$OUT/fiduciary-backup-$ts.tar.gz.enc"

tar czf - -C "$SRC" . | \
  openssl enc -aes-256-cbc -pbkdf2 -iter 300000 -salt -pass env:BACKUP_PASSPHRASE -out "$enc_path"

sha256sum "$enc_path" > "$enc_path.sha256" 2>/dev/null || shasum -a 256 "$enc_path" > "$enc_path.sha256"

echo "Backup written: $enc_path"
echo "Checksum:       $enc_path.sha256"
echo "Size:           $(du -h "$enc_path" | cut -f1)"
