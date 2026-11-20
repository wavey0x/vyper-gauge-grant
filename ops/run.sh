#!/bin/sh
# Run every five minutes, including the closing window before Thursday 00:00 UTC.
# Optional argument: checkpoint (snapshot only) or funding-keeper (weekly payment).
# Sending is opt-in. Use the same directory/signer for the shared process lock.
# KEEPER_TICK records when cron started this pass, before any wait for the lock,
# so the keeper knows which five-minute pass it is running for.
set -eu
KEEPER_TICK="$(date -u +%s)"
export KEEPER_TICK
cd "${VYPER_GRANT_DIR:?Set VYPER_GRANT_DIR}"
keeper_status=0
if [ "${KEEPER_SEND:-0}" = 1 ]; then
    uv run vyper-grant "${1:-keeper}" --send || keeper_status=$?
else
    uv run vyper-grant "${1:-keeper}" || keeper_status=$?
fi
# A failed report must not hide the keeper's status (3 means a failed simulation).
report_status=0
uv run vyper-grant --out dashboard/dist report || report_status=$?
if [ "$keeper_status" -ne 0 ]; then
    exit "$keeper_status"
fi
exit "$report_status"
