#!/bin/bash
set -euo pipefail

source /opt/agentteams/scripts/lib/agentteams-env.sh
AGENTTEAMS_WORKER_NAME="${AGENTTEAMS_WORKER_NAME:?AGENTTEAMS_WORKER_NAME is required}"
WORKER_HOME="${AGENTTEAMS_WORKER_HOME:-/root/agentteams-fs/agents/${AGENTTEAMS_WORKER_NAME}}"
mkdir -p "${WORKER_HOME}/runtime" "${WORKER_HOME}/.codex" "${WORKER_HOME}/workspace"

if ! ensure_mc_credentials || ! agentteams_mc_host_configured; then
    if [ "${AGENTTEAMS_STORAGE_PROVIDER:-minio}" = oss ]; then
        echo 'ERROR: your OSS storage credentials are unavailable' >&2
        exit 1
    fi
    mc alias set "${AGENTTEAMS_STORAGE_ALIAS}" \
        "${AGENTTEAMS_FS_ENDPOINT:?}" "${AGENTTEAMS_FS_ACCESS_KEY:?}" "${AGENTTEAMS_FS_SECRET_KEY:?}" >/dev/null
fi

export HOME="${WORKER_HOME}"
export CODEX_HOME="${WORKER_HOME}/.codex"
export TEAMHARNESS_RUNTIME_CONFIG="${WORKER_HOME}/runtime/runtime.yaml"
export TEAMHARNESS_WORKSPACE="${WORKER_HOME}/workspace"
export AGENTTEAMS_PLUGIN_DIR=/opt/agentteams/plugins/teamharness
export AGENTTEAMS_MATRIX_USER_ID="@${AGENTTEAMS_WORKER_NAME}:${AGENTTEAMS_MATRIX_DOMAIN}"

for attempt in $(seq 1 12); do
    if mc mirror "${AGENTTEAMS_STORAGE_PREFIX%/}/agents/${AGENTTEAMS_WORKER_NAME}/runtime/" "${WORKER_HOME}/runtime/" --overwrite >/dev/null 2>&1; then
        break
    fi
    sleep 5
done
test -s "${TEAMHARNESS_RUNTIME_CONFIG}"
if jq -e '.workspace_has_files == true' "${WORKER_HOME}/runtime/codex-bridge-state.json" >/dev/null 2>&1; then
    mc mirror "${AGENTTEAMS_STORAGE_PREFIX%/}/agents/${AGENTTEAMS_WORKER_NAME}/workspace/" "${TEAMHARNESS_WORKSPACE}/" --overwrite >/dev/null
else
    mc mirror "${AGENTTEAMS_STORAGE_PREFIX%/}/agents/${AGENTTEAMS_WORKER_NAME}/workspace/" "${TEAMHARNESS_WORKSPACE}/" --overwrite >/dev/null 2>&1 || true
fi
# An explicit developer-account mount is separate from object-storage state.
# Your auth.json is never included in a session snapshot.
if [ "${CODEX_AUTH_MODE:-gateway}" = chatgpt ]; then
    cp "${CODEX_AUTH_FILE:?CODEX_AUTH_FILE is required in chatgpt mode}" "${CODEX_HOME}/auth.json"
    chmod 600 "${CODEX_HOME}/auth.json"
fi
export PYTHONPATH="/opt/agentteams/scripts/lib:/opt/agentteams/transport${PYTHONPATH:+:${PYTHONPATH}}"
python3 /opt/agentteams/scripts/lib/agentteams_receipts.py serve --runtime codex --workspace "${WORKER_HOME}" &
exec /opt/venv/bin/agentteams-codex
