#!/usr/bin/env bash
# restore.sh -- SEC-7: decrypt + restore a backup produced by backup.sh.
#
# Controls: ISO/IEC 27001:2022 A.8.13 · NIST SP 800-53 CP-9/CP-10 · SOC 2 A1.2/CC7.1.
# Auditors ask for the RESTORE TEST, not the backup -- this script's whole job is to make that
# test trivial and repeatable, not just a one-time manual exercise.
#
# USAGE:
#   BACKUP_PASSPHRASE=... ./scripts/restore.sh <encrypted-backup-file> <target-directory>
#
# SAFETY: refuses to restore into a non-empty target directory unless FORCE=1 is set, so a
# restore test can never silently clobber a live /data volume by accident. For a REAL disaster
# recovery (restoring into the actual volume after data loss), point --target at the volume path
# with FORCE=1 once you've confirmed the volume is genuinely empty/gone.

set -euo pipefail

SRC_FILE="${1:-}"
TARGET="${2:-}"
PASS="${BACKUP_PASSPHRASE:-}"
FORCE="${FORCE:-0}"

if [ -z "$SRC_FILE" ] || [ -z "$TARGET" ]; then
  echo "Usage: BACKUP_PASSPHRASE=... $0 <encrypted-backup-file> <target-directory>" >&2
  exit 1
fi
if [ -z "$PASS" ]; then
  echo "ERROR: BACKUP_PASSPHRASE is not set." >&2
  exit 1
fi
if [ ! -f "$SRC_FILE" ]; then
  echo "ERROR: backup file '$SRC_FILE' not found." >&2
  exit 1
fi

# Verify the checksum sidecar if present -- catches a corrupted/truncated backup BEFORE spending
# time attempting (and possibly partially completing) a decrypt+extract.
if [ -f "$SRC_FILE.sha256" ]; then
  if command -v sha256sum >/dev/null 2>&1; then
    ( cd "$(dirname "$SRC_FILE")" && sha256sum -c "$(basename "$SRC_FILE").sha256" ) \
      || { echo "ERROR: checksum mismatch -- backup file may be corrupted." >&2; exit 1; }
  else
    ( cd "$(dirname "$SRC_FILE")" && shasum -a 256 -c "$(basename "$SRC_FILE").sha256" ) \
      || { echo "ERROR: checksum mismatch -- backup file may be corrupted." >&2; exit 1; }
  fi
  echo "Checksum OK."
fi

mkdir -p "$TARGET"
if [ "$(ls -A "$TARGET" 2>/dev/null)" ] && [ "$FORCE" != "1" ]; then
  echo "ERROR: target directory '$TARGET' is not empty. Set FORCE=1 to restore into it anyway." >&2
  exit 1
fi

openssl enc -d -aes-256-cbc -pbkdf2 -iter 300000 -pass env:BACKUP_PASSPHRASE -in "$SRC_FILE" | \
  tar xzf - -C "$TARGET"

echo "Restore complete into: $TARGET"
echo "Files restored:"
find "$TARGET" -type f | sort
