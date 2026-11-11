#!/bin/sh
# Run every five minutes, including the closing window before Thursday 00:00 UTC.
# Optional argument: checkpoint (snapshot only) or funding-keeper (weekly payment).
# Sending is opt-in. Use the same directory/signer for the shared process lock.
set -eu
cd "${VYPER_GRANT_DIR:?Set VYPER_GRANT_DIR}"
keeper_status=0
if [ "${KEEPER_SEND:-0}" = 1 ]; then
    uv run vyper-grant "${1:-keeper}" --send || keeper_status=$?
else
    uv run vyper-grant "${1:-keeper}" || keeper_status=$?
fi
uv run vyper-grant --out dashboard/dist report
exit "$keeper_status"
