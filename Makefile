AT_REF ?= b37da7070f610640a84b2061a951c3e4ac690ecb
AT_SOURCE ?= ../AgentTeams
AT_CONTEXT ?= https://github.com/agentscope-ai/AgentTeams.git\#$(AT_REF)
IMAGE ?= ghcr.io/agentteams-group/agentteams-harness-codex
VERSION ?= v0.1.0
PLATFORMS ?= linux/amd64,linux/arm64

.PHONY: build push test
build:
	docker build --build-context at=$(AT_CONTEXT) -t $(IMAGE):$(VERSION) .
push:
	docker buildx build --platform $(PLATFORMS) --build-context at=$(AT_CONTEXT) -t $(IMAGE):$(VERSION) --push .
test:
	AT_SOURCE=$(AT_SOURCE) PYTHONPATH="$(AT_SOURCE)/shared/lib:$(PYTHONPATH)" python3 -m pytest tests -v

direct-control-smoke:
	docker run --rm --entrypoint /opt/venv/bin/python -v "$(CURDIR):/dm:ro" -e PYTHONPATH=/dm/src:/opt/agentteams/scripts/lib:/opt/agentteams/transport $(IMAGE):$(VERSION) /dm/tests/direct_control_smoke.py steer
	docker run --rm --entrypoint /opt/venv/bin/python -v "$(CURDIR):/dm:ro" -e PYTHONPATH=/dm/src:/opt/agentteams/scripts/lib:/opt/agentteams/transport $(IMAGE):$(VERSION) /dm/tests/direct_control_smoke.py stop

native-trace-content-smoke:
	docker run --rm --network none --entrypoint /opt/venv/bin/python -v "$(CURDIR):/harness:ro" -e PYTHONPATH=/harness/src:/harness/tests:/opt/agentteams/scripts/lib:/opt/agentteams/transport -e AGENTTEAMS_OTEL_CAPTURE_CONTENT=true $(IMAGE):$(VERSION) /harness/tests/native_trace_content_smoke.py
	docker run --rm --network none --entrypoint /opt/venv/bin/python -v "$(CURDIR):/harness:ro" -e PYTHONPATH=/harness/src:/harness/tests:/opt/agentteams/scripts/lib:/opt/agentteams/transport -e AGENTTEAMS_OTEL_CAPTURE_CONTENT=false $(IMAGE):$(VERSION) /harness/tests/native_trace_content_smoke.py
