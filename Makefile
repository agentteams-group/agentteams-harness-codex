AT_REF ?= f48a6de1ab52d5c16245ea715b683bb0eed8a622
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
	AT_SOURCE=$(AT_SOURCE) PYTHONPATH="$(AT_SOURCE)/shared/lib:$(PYTHONPATH)" python3 -m unittest discover -s tests -v

direct-control-smoke:
	docker run --rm --entrypoint /opt/venv/bin/python -v "$(CURDIR):/dm:ro" -e PYTHONPATH=/dm/src:/opt/agentteams/scripts/lib:/opt/agentteams/transport $(IMAGE):$(VERSION) /dm/tests/direct_control_smoke.py steer
	docker run --rm --entrypoint /opt/venv/bin/python -v "$(CURDIR):/dm:ro" -e PYTHONPATH=/dm/src:/opt/agentteams/scripts/lib:/opt/agentteams/transport $(IMAGE):$(VERSION) /dm/tests/direct_control_smoke.py stop
