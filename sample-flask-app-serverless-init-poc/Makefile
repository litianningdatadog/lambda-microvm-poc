IMAGE := flask-serverless-init-poc
CONTAINER := flask-serverless-init-poc

# Pass DD_API_KEY from your shell env; falls back to a dummy so the container
# still boots locally (trace-agent will log connection errors, app still runs).
DD_API_KEY ?= dummy-api-key

# Override the image's ENV DD_LOG_LEVEL without rebuilding, e.g.:
#   make run DD_LOG_LEVEL=warn
DD_LOG_LEVEL ?= debug

# Transcript path for `runwithlog`; override with: make runwithlog LOG_FILE=foo.log
LOG_FILE ?= run.log

# Shared docker-run flags used by every run* target.
RUN_FLAGS := -p 8080:8080 -p 9000:9000 \
	-e DD_API_KEY=$(DD_API_KEY) \
	-e DD_LOG_LEVEL=$(DD_LOG_LEVEL)

.PHONY: build run runfg runbg runwithlog readlog stop _rm

build:
	cp /Users/tianning.li/DataDog/datadog-agent-internal/serverless-init-linux-arm64 .
	@[ -f .dd-env ] || echo "# placeholder — set DD_API_KEY in your shell or .dd-env" > .dd-env
	docker build -t $(IMAGE) .

# Default `run` stays as foreground for backward compat with the sibling POC.
run: runfg

# Internal: remove any stale container with our name so the next `docker run`
# can claim it. Silent no-op when there's nothing to remove.
_rm:
	-@docker rm -f $(CONTAINER) 2>/dev/null || true

runfg: _rm
	docker run --rm -it --name $(CONTAINER) $(RUN_FLAGS) $(IMAGE)

runbg: _rm
	docker run --rm -d --name $(CONTAINER) $(RUN_FLAGS) $(IMAGE)
	@echo "Started $(CONTAINER) in background. Tail logs with: make readlog"

runwithlog: _rm
	docker run --rm -it --name $(CONTAINER) $(RUN_FLAGS) $(IMAGE) 2>&1 | tee $(LOG_FILE)

readlog:
	docker logs -f $(CONTAINER)

stop: _rm
