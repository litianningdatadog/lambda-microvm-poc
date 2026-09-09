IMAGE            := sample-flask-app
PORT             := 8080
HOOKS_PORT       := 9000
BASE_PATH        := /aws/lambda-microvms/runtime/v1
HOST             := http://localhost:$(PORT)
HOOKS_HOST       := http://localhost:$(HOOKS_PORT)
DD_TRACE_PY_WHEEL      := /Users/tianning.li/DataDog/dd-trace-py/dist/ddtrace-4.15.0rc2-cp312-cp312-linux_aarch64.whl
DD_TRACE_PY_WHEEL_NAME := $(notdir $(DD_TRACE_PY_WHEEL))

.PHONY: build build-local start stop check

build:
	# Old local serverless-init copy path, kept for easy revert:
	# cp /Users/tianning.li/DataDog/datadog-agent/serverless-init-linux-arm64 .
	cp $(DD_TRACE_PY_WHEEL) .
	docker build \
		--build-arg DD_TRACE_WHEEL=$(DD_TRACE_PY_WHEEL_NAME) \
		-t $(IMAGE) .

build-local:
	# Old local serverless-init copy path, kept for easy revert:
	# cp /Users/tianning.li/DataDog/datadog-agent/serverless-init-linux-arm64 .
	cp $(DD_TRACE_PY_WHEEL) .
	docker build \
		--build-arg DD_TRACE_WHEEL=$(DD_TRACE_PY_WHEEL_NAME) \
		-f Dockerfile.local \
		-t $(IMAGE) .

start:
	docker run --rm -e DD_API_KEY=$$DD_API_KEY -d -p $(PORT):$(PORT) -p $(HOOKS_PORT):$(HOOKS_PORT) --name $(IMAGE) $(IMAGE)

stop:
	docker stop $(IMAGE)

# `check` smoke-tests a *running* container (start it in another terminal
# with `make start` first). Fails the target on any non-2xx response.
check:
	@echo "→ GET  /health"
	@curl -fsS $(HOST)/health && echo
	@echo "→ POST $(BASE_PATH)/validate"
	@curl -fsS -X POST $(HOOKS_HOST)$(BASE_PATH)/validate -o /dev/null -w "  HTTP %{http_code}\n"
	@echo "→ POST $(BASE_PATH)/ready"
	@curl -fsS -X POST $(HOOKS_HOST)$(BASE_PATH)/ready -o /dev/null -w "  HTTP %{http_code}\n"
	@echo "→ POST $(BASE_PATH)/run"
	@curl -fsS -X POST $(HOOKS_HOST)$(BASE_PATH)/run \
		-H 'Content-Type: application/json' \
		-d '{"microVmId":"vm-local","meshIpv6Address":"::1"}' \
		-o /dev/null -w "  HTTP %{http_code}\n"
	@echo "→ POST $(BASE_PATH)/resume"
	@curl -fsS -X POST $(HOOKS_HOST)$(BASE_PATH)/resume -o /dev/null -w "  HTTP %{http_code}\n"
	@echo "→ POST $(BASE_PATH)/suspend"
	@curl -fsS -X POST $(HOOKS_HOST)$(BASE_PATH)/suspend -o /dev/null -w "  HTTP %{http_code}\n"
	@echo "→ POST $(BASE_PATH)/terminate"
	@curl -fsS -X POST $(HOOKS_HOST)$(BASE_PATH)/terminate -o /dev/null -w "  HTTP %{http_code}\n"
	@echo "→ POST /execute"
	@curl -fsS -X POST $(HOST)/execute \
		-H 'Content-Type: application/json' \
		-d '{"code":"print(1 + 1)"}' && echo
