ARG MC_IMAGE=higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/mc:20260216
ARG AGENTTEAMS_CONTROLLER_IMAGE=higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-controller:v1.2.6
FROM ${MC_IMAGE} AS mc
FROM ${AGENTTEAMS_CONTROLLER_IMAGE} AS controller
FROM node:22.22-bookworm-slim
ARG CODEX_VERSION=0.143.0
ARG NPM_REGISTRY=https://registry.npmjs.org/
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git jq python3 python3-venv tini \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.npm npm install --global --no-audit --no-fund --loglevel=info \
    --registry="${NPM_REGISTRY}" --fetch-timeout=60000 --fetch-retries=1 "@openai/codex@${CODEX_VERSION}"
COPY --from=mc /usr/bin/mc /usr/local/bin/mc.bin
COPY --from=controller /usr/local/bin/agt /usr/local/bin/agt
# AT is a named BuildKit context, pinned by Makefile or overridden for local tests.
COPY --from=at /shared/lib/ /opt/agentteams/scripts/lib/
COPY --from=at /deepseek-harness/scripts/*.py /opt/agentteams/transport/
COPY --from=at /plugins/teamharness/ /opt/agentteams/plugins/teamharness/
COPY --from=at /LICENSE /usr/share/licenses/agentteams/LICENSE
COPY pyproject.toml /opt/agentteams/codex/pyproject.toml
COPY src/ /opt/agentteams/codex/src/
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir /opt/agentteams/codex \
    && chmod +x /opt/agentteams/scripts/lib/mc-wrapper.sh \
    && ln -s /opt/agentteams/scripts/lib/mc-wrapper.sh /usr/local/bin/mc
COPY scripts/entrypoint.sh /opt/agentteams/entrypoint.sh
RUN chmod +x /opt/agentteams/entrypoint.sh
ENV PYTHONPATH=/opt/agentteams/transport
EXPOSE 8088
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/agentteams/entrypoint.sh"]
