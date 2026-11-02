IMAGE ?= sample-verify-tracer-js-app
PORT ?= 8080
SERVERLESS_INIT_BIN ?= /Users/tianning.li/DataDog/datadog-agent/serverless-init-linux-arm64
OUTPUT ?= results/local.json

.PHONY: prepare build package start stop check smoke test
prepare:
	cp "$(SERVERLESS_INIT_BIN)" serverless-init-linux-arm64
	chmod +x serverless-init-linux-arm64

build: prepare
	docker build --platform linux/arm64 -t "$(IMAGE)" .

package: prepare
	zip ../sample-verify-tracer-js-app.zip Dockerfile app.js intake.js features.js feature-checks.js delivery.js package.json package-lock.json serverless-init-linux-arm64

# Capture-only mode; intentionally bypass the Linux serverless-init binary.
start:
	docker run --rm -d --name "$(IMAGE)" -p "$(PORT):8080" \
		-e AWS_LAMBDA_MICROVM_IMAGE_ARN=local-simulation \
		--entrypoint node "$(IMAGE)" app.js

stop:
	docker stop "$(IMAGE)"

# Local simulation explicitly calls ready/run; do not use --simulate on AWS.
check:
	node verify.js --url "http://127.0.0.1:$(PORT)" --simulate --output "$(OUTPUT)"

smoke:
	npm ci --no-audit --no-fund
	node run-local.js

test:
	python3 -m unittest -v test_check test_cloud
	node --test test-delivery.js
