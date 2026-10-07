"""Fake docker/curl/sleep/date for the script tests. A virtual clock: nothing really waits."""

from __future__ import annotations

from pathlib import Path

# Like the real docker: without -f only the CURRENT directory is searched for a compose file, and
# --project-directory does not choose the file.
DOCKER = r"""#!/bin/sh
echo "docker $*" >> "$SHIM_LOG"
. "$SHIM_DIR/clock.sh"; tick 1
case "$1" in
  inspect) cat "$SHIM_DIR/inspect_$2" 2>/dev/null || exit 1 ;;
  compose)
    has_f=0; for a in "$@"; do [ "$a" = "-f" ] && has_f=1; done
    if [ "$has_f" = 0 ]; then
      found=0
      for n in compose.yaml compose.yml docker-compose.yaml docker-compose.yml; do [ -f "$PWD/$n" ] && found=1; done
      if [ "$found" = 0 ]; then echo "no configuration file provided: not found" >&2; exit 1; fi
    fi
    case "$*" in
      *" ps "*) cat "$SHIM_DIR/compose_ps" 2>/dev/null ;;
      *) [ -f "$SHIM_DIR/compose_fail" ] && exit 3 ;;
    esac ;;
esac
exit 0
"""

CLOCK = r"""
tick() { _n=$(cat "$SHIM_DIR/clock" 2>/dev/null || echo 1000); echo $((_n + $1)) > "$SHIM_DIR/clock"; }
"""

CURL = r"""#!/bin/sh
echo "curl $*" >> "$SHIM_LOG"
. "$SHIM_DIR/clock.sh"; tick "${CURL_SECONDS:-5}"
[ -f "$SHIM_DIR/curl_ok" ]
"""

SLEEP = r"""#!/bin/sh
. "$SHIM_DIR/clock.sh"; tick "${1:-1}"
"""

DATE = r"""#!/bin/sh
cat "$SHIM_DIR/clock" 2>/dev/null || echo 1000
"""


def install(directory: Path) -> None:
    (directory / "clock.sh").write_text(CLOCK)
    for name, body in (("docker", DOCKER), ("curl", CURL), ("sleep", SLEEP), ("date", DATE)):
        path = directory / name
        path.write_text(body)
        path.chmod(0o755)
