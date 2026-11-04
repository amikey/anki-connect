#!/bin/bash
set -euo pipefail

# ----------------------------
# Anki Daily Backup Script (low-impact, consistent)
# ----------------------------
# Goals vs. the old script:
#  - Don't starve Anki's single-threaded HTTP server (AnkiConnect runs on
#    Anki's main thread; CPU/IO starvation => API timeouts for everyone).
#  - Don't gzip already-compressed media (huge CPU win).
#  - Snapshot SQLite atomically so backups are restorable (no torn DB).

# Paths
ANKI_DATA="$HOME/.local/share/Anki2"
BACKUP_DIR="$HOME/anki_exports"
mkdir -p "$BACKUP_DIR"

# ----------------------------
# Single-instance guard
# ----------------------------
# If a previous run is slow or hung, the next cron tick must NOT start a second
# backup on top of it (that's how you get N tar+upload jobs stacking on one
# vCPU). flock -n grabs the lock or exits immediately.
exec 9>"$HOME/.anki_backup.lock"
if ! flock -n 9; then
    echo "$(date '+%F %T') another backup is still running; skipping this run." >&2
    exit 0
fi

# Timestamp
TIMESTAMP=$(date +%F_%H-%M-%S)

# DigitalOcean Spaces remote
REMOTE="do_spaces:alledeamdata/ankibackups"

# Staging area (hardlink clone of Anki2; near-zero space/time for media)
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/anki_backup.XXXXXX")"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

# ----------------------------
# 1. Hardlink-clone the live data dir (instant, no media copy)
# ----------------------------
# cp -al makes hardlinks for every file, so the clone shares media bytes on
# disk. We then break the link only for the SQLite files we snapshot below.
cp -al "$ANKI_DATA" "$STAGE/Anki2"

# ----------------------------
# 2. Atomically snapshot every SQLite DB into the clone
# ----------------------------
# `.backup` takes a consistent copy even while Anki is writing. This replaces
# the hardlinked (and possibly torn) live files in the clone.
while IFS= read -r -d '' db; do
    rel="${db#"$ANKI_DATA"/}"
    dest="$STAGE/Anki2/$rel"
    rm -f "$dest"                      # break hardlink to live file
    if sqlite3 "$db" ".backup '$dest'"; then
        :
    else
        echo "WARN: sqlite .backup failed for $db; falling back to file copy" >&2
        cp -f "$db" "$dest"
    fi
done < <(find "$ANKI_DATA" -type f \
            \( -name 'collection.anki2' -o -name 'collection.media.db*' \
               -o -name 'prefs21.db' \) -print0)

# ----------------------------
# 3. Pack the clone. Compress with zstd (multithreaded) if available,
#    else pigz, else plain tar (NO gzip on media).
# ----------------------------
if command -v zstd >/dev/null 2>&1; then
    # 1 vCPU box: use the fast, low-CPU level (-1). It still compresses the
    # SQLite DBs well but won't sit on the only core like gzip did. (-T0 is a
    # no-op on a single core but harmless.)
    BACKUP_FILE="$BACKUP_DIR/anki_backup_$TIMESTAMP.tar.zst"
    nice -n 19 ionice -c3 tar -cf - -C "$STAGE" Anki2 \
        | nice -n 19 zstd -1 -q -o "$BACKUP_FILE"
elif command -v pigz >/dev/null 2>&1; then
    BACKUP_FILE="$BACKUP_DIR/anki_backup_$TIMESTAMP.tar.gz"
    nice -n 19 ionice -c3 tar -cf - -C "$STAGE" Anki2 \
        | pigz > "$BACKUP_FILE"
else
    # No compression: media is already compressed, so this is mostly a
    # straight read+write and far cheaper than gzip.
    BACKUP_FILE="$BACKUP_DIR/anki_backup_$TIMESTAMP.tar"
    nice -n 19 ionice -c3 tar -cf "$BACKUP_FILE" -C "$STAGE" Anki2
fi

# ----------------------------
# 4. Upload, throttled so it can't saturate CPU/network/disk
# ----------------------------
nice -n 19 ionice -c3 rclone copy "$BACKUP_FILE" "$REMOTE" \
    --s3-acl private \
    --s3-no-check-bucket \
    --transfers=1 \
    --checkers=1 \
    --bwlimit=10M \
    --log-file="$BACKUP_DIR/anki_backup_$TIMESTAMP.log"

# ----------------------------
# 5. Retention: keep last 2 days of backups locally (older ones live in Spaces)
# ----------------------------
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'anki_backup_*.tar*' -mtime +1 -delete

# Optional remote retention (rclone v1.71+):
# rclone delete "$REMOTE" --min-age 7d --s3-no-check-bucket --s3-acl private
