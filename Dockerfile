ARG MC_IMAGE=higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/mc:20260216
ARG AGENTTEAMS_CONTROLLER_IMAGE=higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-controller:v1.2.6
FROM ${MC_IMAGE} AS mc
FROM ${AGENTTEAMS_CONTROLLER_IMAGE} AS controller
# Fixed native SDK capability: durable User input while a tool result is held.
FROM rust:1.95-bookworm AS native-sdk
ARG CODEX_SDK_REF=c4d748f586a84a3ed5b6aceb82e9a1db4abb1cda
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
    clang cmake pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch rust-v0.143.0 https://github.com/openai/codex.git /build/codex \
    && test "$(git -C /build/codex rev-parse HEAD)" = "${CODEX_SDK_REF}"
COPY patches/codex-durable-user-input.patch /tmp/codex-durable-user-input.patch
RUN git -C /build/codex apply /tmp/codex-durable-user-input.patch
WORKDIR /build/codex/codex-rs
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,id=at129-codex-sdk-${TARGETARCH},target=/build/codex/codex-rs/target,sharing=locked \
    CARGO_INCREMENTAL=0 CARGO_PROFILE_RELEASE_DEBUG=0 cargo build --locked --release -p codex-app-server --bin codex-app-server \
    && cp target/release/codex-app-server /usr/local/bin/codex-app-server
# Build the public CLI from the same pinned Core as the protocol assets.
FROM higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/golang:1.26-alpine AS public-cli
WORKDIR /build
COPY --from=at /agentteams-controller/ ./
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go build -o /out/agt ./cmd/agt && /out/agt assistant --help > /dev/null

FROM node:22.22-bookworm-slim
ARG CODEX_VERSION=0.143.0
ARG NPM_REGISTRY=https://registry.npmjs.org/
RUN apt-get update && apt-get install -y --no-install-recommends \
    bubblewrap ca-certificates curl git jq python3 python3-venv tini \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.npm npm install --global --no-audit --no-fund --loglevel=info \
    --registry="${NPM_REGISTRY}" --fetch-timeout=60000 --fetch-retries=1 "@openai/codex@${CODEX_VERSION}"
COPY --from=native-sdk /usr/local/bin/codex-app-server /usr/local/bin/codex-app-server
COPY --from=mc /usr/bin/mc /usr/local/bin/mc.bin
COPY --from=public-cli /out/agt /usr/local/bin/agt
# AT is a named BuildKit context, pinned by Makefile or overridden for local tests.
COPY --from=at /shared/lib/ /opt/agentteams/scripts/lib/
COPY --from=at /deepseek-harness/scripts/*.py /opt/agentteams/transport/
COPY --from=at /plugins/teamharness/ /opt/agentteams/plugins/teamharness/
COPY --from=at /LICENSE /usr/share/licenses/agentteams/LICENSE
COPY pyproject.toml /opt/agentteams/codex/pyproject.toml
COPY src/ /opt/agentteams/codex/src/
COPY configs/requirements.toml /etc/codex/requirements.toml
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir /opt/agentteams/codex -r /opt/agentteams/scripts/lib/telemetry/requirements.txt \
    && chmod 0755 /usr/local/bin/mc.bin /opt/agentteams/scripts/lib/mc-wrapper.sh \
    && ln -s /opt/agentteams/scripts/lib/mc-wrapper.sh /usr/local/bin/mc
COPY scripts/entrypoint.sh /opt/agentteams/entrypoint.sh
RUN chmod +x /opt/agentteams/entrypoint.sh
ENV PYTHONPATH=/opt/agentteams/scripts/lib:/opt/agentteams/transport \
    PATH=/opt/venv/bin:${PATH}
EXPOSE 8088
COPY health_adapter.py /opt/agentteams/health_adapter.py
RUN printf '#!/bin/sh\nexec /opt/venv/bin/python /opt/agentteams/health_adapter.py "$@"\n' > /opt/agentteams/harness-health \
    && chmod 0755 /opt/agentteams/harness-health

ENTRYPOINT ["/usr/bin/tini", "--", "/opt/agentteams/entrypoint.sh"]
