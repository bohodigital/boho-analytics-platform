#!/bin/sh
set -eu

: "${BOHO_ANALYTICS_CLI:?set BOHO_ANALYTICS_CLI to the reviewed executable}"
: "${BOHO_ANALYTICS_CONFIG:?set BOHO_ANALYTICS_CONFIG to the private TOML path}"
: "${BOHO_ANALYTICS_BACKUP_DIR:?set BOHO_ANALYTICS_BACKUP_DIR to an absolute directory}"

case "$BOHO_ANALYTICS_BACKUP_DIR" in
  /*) ;;
  *) echo "BOHO_ANALYTICS_BACKUP_DIR must be absolute" >&2; exit 2 ;;
esac

retention_days="${BOHO_ANALYTICS_BACKUP_RETENTION_DAYS:-90}"
case "$retention_days" in
  *[!0-9]*|'') echo "backup retention must be a non-negative integer" >&2; exit 2 ;;
esac

umask 077
mkdir -p "$BOHO_ANALYTICS_BACKUP_DIR"
scheduled_dir="$BOHO_ANALYTICS_BACKUP_DIR/scheduled"
mkdir -p "$scheduled_dir"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
destination="$scheduled_dir/analytics-$timestamp.sqlite3"

"$BOHO_ANALYTICS_CLI" --config "$BOHO_ANALYTICS_CONFIG" db backup "$destination"
chmod 600 "$destination"

# Retention is physically confined to the scheduled subdirectory. Manual,
# pre-migration, preview, and rollback evidence remain outside it and are preserved.
find "$scheduled_dir" -maxdepth 1 -type f \
  -name 'analytics-[0-9]*.sqlite3' -mtime "+$retention_days" -delete

printf 'backup=%s retention_days=%s\n' "$destination" "$retention_days"
