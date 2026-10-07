#!/bin/sh
# Shared helpers for the gate's operation scripts. POSIX sh, no bashisms.
# The gate runs these with a minimal environment (PATH only) and no stdin.

log() { printf '%s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# wait_until <timeout_seconds> <interval_seconds> <description> <command...>
# Runs the command until it succeeds or the timeout passes. Fails with a message otherwise.
wait_until() {
  _timeout=$1; _interval=$2; _what=$3; shift 3
  _waited=0
  while :; do
    if "$@" >/dev/null 2>&1; then
      log "ok: $_what (after ${_waited}s)"
      return 0
    fi
    if [ "$_waited" -ge "$_timeout" ]; then
      fail "$_what did not become ready within ${_timeout}s"
    fi
    sleep "$_interval"
    _waited=$((_waited + _interval))
  done
}

# container_ready <name>: running, and healthy when the image defines a healthcheck.
container_ready() {
  _info=$(docker inspect "$1" --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null) || return 1
  case "$_info" in
    "running none"|"running healthy") return 0 ;;
    *) return 1 ;;
  esac
}

# compose_service_running <service...> uses the COMPOSE_ARGS words set by the caller.
# Every named service must be listed as running.
compose_services_running() {
  # shellcheck disable=SC2086
  _running=$(docker compose $COMPOSE_ARGS ps --status running --services 2>/dev/null) || return 1
  for _svc in "$@"; do
    printf '%s\n' "$_running" | grep -qx "$_svc" || return 1
  done
}
