#!/bin/sh
# SSH forced-command entry point. Install as /opt/ivrm-ops-gate/run.sh (root-owned, not writable by others).
# The client's own command line is ignored: ops_gate reads SSH_ORIGINAL_COMMAND and accepts
# only "run|check <ticket> <signature>".
cd /opt/ivrm-ops-gate || exit 70
exec env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin PYTHONPATH=/opt/ivrm-ops-gate \
  SSH_ORIGINAL_COMMAND="${SSH_ORIGINAL_COMMAND:-}" \
  /usr/bin/python3 -s -m ops_gate --config /etc/ivrm-ops-gate/config.json
