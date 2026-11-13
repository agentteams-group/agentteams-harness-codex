AT_REF ?= 0ffbdc55d39350f9e78cf27392bbc049b64a0392
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
	AT_SOURCE=$(AT_SOURCE) python3 -m unittest discover -s tests -v
