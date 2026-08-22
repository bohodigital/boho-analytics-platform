#!/bin/sh

# Run one bounded analytics sync subprocess per configured production site.
# A timeout or provider failure is recorded for that site and does not prevent
# later sites from being attempted.

set -u

: "${BOHO_ANALYTICS_CLI:?set BOHO_ANALYTICS_CLI to the reviewed executable}"
: "${BOHO_ANALYTICS_CONFIG:?set BOHO_ANALYTICS_CONFIG to the private TOML path}"
: "${BOHO_ANALYTICS_SYNC_SITES:?set BOHO_ANALYTICS_SYNC_SITES to space-separated site ids}"
: "${BOHO_ANALYTICS_SYNC_CONNECTIONS:?set BOHO_ANALYTICS_SYNC_CONNECTIONS to space-separated connection ids}"

timeout_command="${BOHO_ANALYTICS_TIMEOUT_COMMAND:-/usr/bin/timeout}"
timeout_seconds="${BOHO_ANALYTICS_SITE_TIMEOUT_SECONDS:-3600}"
sync_days="${BOHO_ANALYTICS_SYNC_DAYS:-3}"

case "$BOHO_ANALYTICS_CONFIG" in
  /*) ;;
  *) echo "BOHO_ANALYTICS_CONFIG must be absolute" >&2; exit 2 ;;
esac
case "$timeout_seconds" in
  *[!0-9]*|'') echo "site timeout must be a positive integer" >&2; exit 2 ;;
  0) echo "site timeout must be a positive integer" >&2; exit 2 ;;
esac
case "$sync_days" in
  *[!0-9]*|'') echo "sync days must be a positive integer" >&2; exit 2 ;;
  0) echo "sync days must be a positive integer" >&2; exit 2 ;;
esac

validate_id() {
  case "$1" in
    ''|*[!a-z0-9-]*|-*|*-) return 1 ;;
    *) return 0 ;;
  esac
}

for connection_id in $BOHO_ANALYTICS_SYNC_CONNECTIONS; do
  if ! validate_id "$connection_id"; then
    echo "invalid connection id in BOHO_ANALYTICS_SYNC_CONNECTIONS" >&2
    exit 2
  fi
done
for site_id in $BOHO_ANALYTICS_SYNC_SITES; do
  if ! validate_id "$site_id"; then
    echo "invalid site id in BOHO_ANALYTICS_SYNC_SITES" >&2
    exit 2
  fi
done

failed=0
attempted=0
for site_id in $BOHO_ANALYTICS_SYNC_SITES; do
  attempted=$((attempted + 1))
  set -- "$BOHO_ANALYTICS_CLI" --config "$BOHO_ANALYTICS_CONFIG" \
    sync --site "$site_id" --days "$sync_days"
  for connection_id in $BOHO_ANALYTICS_SYNC_CONNECTIONS; do
    set -- "$@" --connection "$connection_id"
  done
  echo "analytics_sync site=$site_id status=started"
  if "$timeout_command" --signal=TERM --kill-after=30s \
      "${timeout_seconds}s" "$@"; then
    echo "analytics_sync site=$site_id status=success"
  else
    exit_status=$?
    failed=$((failed + 1))
    echo "analytics_sync site=$site_id status=failed exit_status=$exit_status" >&2
  fi
done

if [ "$attempted" -eq 0 ]; then
  echo "BOHO_ANALYTICS_SYNC_SITES did not contain a site id" >&2
  exit 2
fi
if [ "$failed" -ne 0 ]; then
  echo "analytics_sync attempted=$attempted failed=$failed" >&2
  exit 1
fi
echo "analytics_sync attempted=$attempted failed=0"
