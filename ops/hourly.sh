#!/bin/sh
# Set VYPER_GRANT_DIR, ETHEREUM_RPC_URL and ARBITRUM_RPC_URL externally.
set -eu
cd "${VYPER_GRANT_DIR:?Set VYPER_GRANT_DIR}"
uv run vyper-grant reconcile
uv run vyper-grant report
