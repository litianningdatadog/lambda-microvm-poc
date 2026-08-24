IMAGE      := sample-nodejs-skill-driven-app
PORT       := 8080
HOOKS_PORT := 9000
BASE_PATH  := /aws/lambda-microvms/runtime/v1
HOST       := http://localhost:$(PORT)
HOOKS_HOST := http://localhost:$(HOOKS_PORT)

.PHONY: build start stop check

build:
	docker build -t $(IMAGE) .

start:
	docker run --rm -d -p $(PORT):$(PORT) -p $(HOOKS_PORT):$(HOOKS_PORT) --name $(IMAGE) $(IMAGE)

stop:
	docker stop $(IMAGE)

# `check` smoke-tests a *running* container (start it with `make start` first).
check:
	@echo "→ GET  /health"
	@curl -fsS $(HOST)/health && echo
	@echo "→ POST $(BASE_PATH)/ready"
	@curl -fsS -X POST $(HOOKS_HOST)$(BASE_PATH)/ready -o /dev/null -w "  HTTP %{http_code}\n"
	@echo "→ POST $(BASE_PATH)/run"
	@curl -fsS -X POST $(HOOKS_HOST)$(BASE_PATH)/run \
		-H 'Content-Type: application/json' \
		-d '{"microVmId":"vm-local","meshIpv6Address":"::1"}' \
		-o /dev/null -w "  HTTP %{http_code}\n"
	@echo "→ POST /execute"
	@curl -fsS -X POST $(HOST)/execute \
		-H 'Content-Type: application/json' \
		-d '{"code":"console.log(1 + 1)"}' && echo
