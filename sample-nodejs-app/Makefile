IMAGE            := sample-nodejs-app
PORT             := 8080
BASE_PATH        := /aws/lambda-microvms/runtime/beta/v1
HOST             := http://localhost:$(PORT)
DD_TRACE_JS_REPO := /Users/tianning.li/DataDog/dd-trace-js

.PHONY: build build-local start stop check

build:
	cp /Users/tianning.li/DataDog/datadog-agent-internal/serverless-init-linux-arm64 .
	@[ -f .dd-env ] || echo "# placeholder — set DD_API_KEY in your shell or .dd-env" > .dd-env
	docker build -t $(IMAGE) .

build-local:
	cp /Users/tianning.li/DataDog/datadog-agent-internal/serverless-init-linux-arm64 .
	@[ -f .dd-env ] || echo "# placeholder — set DD_API_KEY in your shell or .dd-env" > .dd-env
	cd $(DD_TRACE_JS_REPO) && npm pack
	cp $(DD_TRACE_JS_REPO)/dd-trace-*.tgz dd-trace-local.tgz
	docker build --build-arg DD_TRACE_TGZ=dd-trace-local.tgz -t $(IMAGE) -f Dockerfile.local .

start:
	docker run --rm -d -p $(PORT):$(PORT) --name $(IMAGE) $(IMAGE)

stop:
	docker stop $(IMAGE)

# `check` smoke-tests a *running* container (start it with `make start` first).
# Fails the target on any non-2xx response.
check:
	@echo "→ GET  /health"
	@curl -fsS $(HOST)/health && echo
	@echo "→ POST $(BASE_PATH)/ready"
	@curl -fsS -X POST $(HOST)$(BASE_PATH)/ready -o /dev/null -w "  HTTP %{http_code}\n"
	@echo "→ POST $(BASE_PATH)/launch"
	@curl -fsS -X POST $(HOST)$(BASE_PATH)/launch \
		-H 'Content-Type: application/json' \
		-d '{"microVmId":"vm-local","meshIpv6Address":"::1"}' \
		-o /dev/null -w "  HTTP %{http_code}\n"
	@echo "→ POST $(BASE_PATH)/resume"
	@curl -fsS -X POST $(HOST)$(BASE_PATH)/resume -o /dev/null -w "  HTTP %{http_code}\n"
	@echo "→ POST $(BASE_PATH)/suspend"
	@curl -fsS -X POST $(HOST)$(BASE_PATH)/suspend -o /dev/null -w "  HTTP %{http_code}\n"
	@echo "→ POST $(BASE_PATH)/terminate"
	@curl -fsS -X POST $(HOST)$(BASE_PATH)/terminate -o /dev/null -w "  HTTP %{http_code}\n"
	@echo "→ POST /execute"
	@curl -fsS -X POST $(HOST)/execute \
		-H 'Content-Type: application/json' \
		-d '{"code":"console.log(1 + 1)"}' && echo
