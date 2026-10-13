#!/usr/bin/env bash
# Install ops-agent on the host (Amazon Linux 2023). Run as root, from a clone of this repository:
#
#     sudo bash services/ops-agent/deploy/install.sh arn:aws:iam::<ACCOUNT_ID>:role/ivrm-ops-agent
#
# Safe to run again: it never overwrites /etc/ivrm-ops-agent/env or aws-config once they exist,
# it never enables the timer, and it never sends anything. Afterwards, put the Discord webhook URL
# into /etc/ivrm-ops-agent/env and follow DEPLOY.md step 6 (--test-notify, --check, one run).
set -eu

ROLE_ARN="${1:-}"
case "$ROLE_ARN" in
  arn:aws*:iam::[0-9]*:role/*) ;;
  *) echo "usage: sudo bash $0 arn:aws:iam::<ACCOUNT_ID>:role/ivrm-ops-agent" >&2; exit 2 ;;
esac
# DESTDIR is for testing the script against a scratch tree; on a real host it is empty.
DESTDIR="${DESTDIR:-}"
if [ -z "$DESTDIR" ] && [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo)" >&2
  exit 2
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/.." && pwd)"
[ -f "$SRC/requirements.txt" ] && [ -d "$SRC/ops_agent" ] || { echo "not an ops-agent checkout: $SRC" >&2; exit 2; }

APP="$DESTDIR"/opt/ivrm-ops-agent
ETC="$DESTDIR"/etc/ivrm-ops-agent
UNITS="$DESTDIR"/etc/systemd/system

echo "[1/6] python 3.11"
command -v python3.11 >/dev/null 2>&1 || dnf install -y python3.11

echo "[2/6] service user"
id ivrm-ops-agent >/dev/null 2>&1 || useradd -r -s /sbin/nologin ivrm-ops-agent

echo "[3/6] code and virtualenv"
install -d -m 0755 "$APP"
rm -rf "$APP/ops_agent"
cp -r "$SRC/ops_agent" "$APP/ops_agent"
find "$APP/ops_agent" -name '__pycache__' -type d -prune -exec rm -rf {} +
install -m 0644 "$SRC/requirements.txt" "$APP/requirements.txt"
[ -x "$APP/venv/bin/python" ] || python3.11 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet -r "$APP/requirements.txt"
[ -n "$DESTDIR" ] || chown -R root:root "$APP"

echo "[4/6] configuration (existing files are kept)"
install -d -m 0755 "$ETC"
if [ ! -f "$ETC/env" ]; then
  install -m 0600 "$SRC/deploy/env.example" "$ETC/env"
  echo "      created $ETC/env: put the Discord webhook URL into it"
else
  echo "      kept $ETC/env"
fi
if [ ! -f "$ETC/aws-config" ]; then
  sed "s#^role_arn = .*#role_arn = $ROLE_ARN#" "$SRC/deploy/aws-config.example" > "$ETC/aws-config"
  chmod 0644 "$ETC/aws-config"
  echo "      created $ETC/aws-config"
else
  echo "      kept $ETC/aws-config (check that role_arn is $ROLE_ARN)"
fi
grep -q "^role_arn = $ROLE_ARN\$" "$ETC/aws-config" || echo "      WARNING: role_arn in $ETC/aws-config differs from $ROLE_ARN"

echo "[5/6] systemd units"
install -d -m 0755 "$UNITS"
install -m 0644 "$SRC/deploy/ivrm-ops-agent.service" "$UNITS/ivrm-ops-agent.service"
install -m 0644 "$SRC/deploy/ivrm-ops-agent.timer" "$UNITS/ivrm-ops-agent.timer"
systemctl daemon-reload

echo "[6/6] done. The timer is NOT enabled yet."
echo "Next: edit $ETC/env, then DEPLOY.md step 6 (--test-notify, --check, one service run), then:"
echo "      sudo systemctl enable --now ivrm-ops-agent.timer"
