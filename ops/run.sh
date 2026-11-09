#!/bin/sh
# Schedule every five minutes. Sending is opt-in; credentials stay in the environment.
set -eu
cd "${VYPER_GRANT_DIR:?Set VYPER_GRANT_DIR}"
keeper_status=0
if [ "${KEEPER_SEND:-0}" = 1 ]; then
    uv run vyper-grant keeper --send || keeper_status=$?
else
    uv run vyper-grant keeper || keeper_status=$?
fi
uv run vyper-grant reconcile
uv run vyper-grant --out dashboard/dist report
exit "$keeper_status"
