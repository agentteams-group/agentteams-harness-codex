#!/bin/bash
if [ "${AGENTTEAMS_LOG_CAPTURED:-}" != "1" ]; then
    exec python3 "${AGENTTEAMS_LOG_LIB_DIR:-/opt/agentteams/scripts/lib}/runtime_supervisor.py" --runtime codex -- bash "$0" "$@"
fi
python3 "${AGENTTEAMS_LOG_LIB_DIR:-/opt/agentteams/scripts/lib}/runtime_observability.py" bootstrap

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
    if /opt/venv/bin/python -m agentteams_codex.bootstrap; then
        break
    fi
    if [ "${attempt}" -eq 12 ]; then
        echo 'ERROR: unable to restore the Worker startup configuration' >&2
        exit 1
    fi
    sleep 5
done
# An explicit developer-account mount is separate from object-storage state.
# Your auth.json is never included in a session snapshot.
if [ "${CODEX_AUTH_MODE:-gateway}" = chatgpt ] && { [ -z "${AGENTTEAMS_CONTROLLER_URL:-}" ] || [ "${AGENTTEAMS_AGENT_ROLE:-worker}" = manager ]; }; then
    cp "${CODEX_AUTH_FILE:?CODEX_AUTH_FILE is required in chatgpt mode}" "${CODEX_HOME}/auth.json"
    chmod 600 "${CODEX_HOME}/auth.json"
fi
export PYTHONPATH="/opt/agentteams/scripts/lib:/opt/agentteams/transport${PYTHONPATH:+:${PYTHONPATH}}"
exec /opt/venv/bin/agentteams-codex
