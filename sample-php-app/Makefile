IMAGE     := sample-php-app
PORT      := 8080
BASE_PATH := /aws/lambda-microvms/runtime/beta/v1
HOST      := http://localhost:$(PORT)

.PHONY: build start stop check

build:
	cp /Users/tianning.li/DataDog/datadog-agent-internal/serverless-init-linux-arm64 .
	docker build -t $(IMAGE) .

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
		-d '{"code":"echo 1 + 1;"}' && echo
