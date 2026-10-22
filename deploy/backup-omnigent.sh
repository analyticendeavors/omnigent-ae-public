#!/usr/bin/env bash
# Nightly consistent dump of the OmniGent Postgres database into
# /secure/omnigent/backups, newest seven kept, for the box's restic run to
# carry to Azure Blob. Phase 6, 2026-09-23. Run by ae-omnigent-dump.timer at
# 02:40 local; by hand: sudo deploy/backup-omnigent.sh
#
# Why a dump and not the data directory: the Postgres data directory is a live
# database, and a file-level copy of one (which is what restic makes) is not
# restorable in general. pg_dump inside the running container is consistent
# by construction. The data directory stays on /secure too, for encryption at
# rest, not as the backup.
#
# What restic needs: /secure/omnigent/backups (and /secure/omnigent/data, the
# config, agents and artifacts) added to the path list in
# home-server/backup/backup.sh in agents-and-automation. That file is not in
# this repository; until the line lands, the dumps exist only on the box.
#
# Restore (into a running stack, over an empty database):
#   gunzip -c /secure/omnigent/backups/omnigent-<stamp>.sql.gz \
#     | deploy/compose.sh exec -T postgres sh -c 'exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
set -euo pipefail

# shellcheck source=scripts/_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/../scripts/_lib.sh"

BACKUP_DIR="${AE_BACKUP_DIR:-/secure/omnigent/backups}"
KEEP="${AE_BACKUP_KEEP:-7}"
COMPOSE="$ROOT/deploy/compose.sh"

# Without this an unmounted /secure means the dump lands in the plain directory
# underneath the mountpoint and the run reports success, the one failure mode a
# backup must never have. Same check as backup.sh's, for the same reason.
volume="$(dirname "$(dirname "$BACKUP_DIR")")"
mountpoint -q "$volume" || die "$volume is not a mountpoint; refusing to write a dump that would not be on the encrypted volume"

if ! [[ "$KEEP" =~ ^[1-9][0-9]*$ ]]; then
  die "AE_BACKUP_KEEP must be a positive integer, got '$KEEP'"
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

stamp="$(date +%Y-%m-%dT%H%M%S)"
out="$BACKUP_DIR/omnigent-$stamp.sql.gz"
tmp="$out.tmp"
trap 'rm -f -- "${tmp:?}"' EXIT

# -T: no TTY, so the dump streams cleanly through the pipe. The user and the
# database come from the container's own environment, so this follows whatever
# POSTGRES_USER and POSTGRES_DB compose set. pipefail makes a pg_dump failure
# fail the whole line even though gzip would exit 0. The single quotes are
# deliberate: the variables expand inside the container, not here.
# shellcheck disable=SC2016
"$COMPOSE" exec -T postgres sh -c 'exec pg_dump --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  | gzip -9 > "$tmp"

gzip -t "$tmp" || die "$tmp is not a valid gzip stream; the dump was cut short"
[ -s "$tmp" ] || die "$tmp is empty; is the stack up? (deploy/compose.sh ps)"
chmod 600 "$tmp"
mv -f "$tmp" "$out"
trap - EXIT

# Keep the newest $KEEP. The names sort by their timestamp, so a name sort is
# a time sort, with no dependence on mtime. find's -delete would need the
# same list, so the loop deletes exactly what the listing named.
mapfile -t old < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'omnigent-*.sql.gz' -printf '%f\n' | sort -r | tail -n +"$((KEEP + 1))")
for f in "${old[@]}"; do
  rm -f -- "${BACKUP_DIR:?}/${f:?}"
done

note "wrote $out ($(du -h "$out" | cut -f1)); ${#old[@]} older dump(s) removed, $KEEP kept"
