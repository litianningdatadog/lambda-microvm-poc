IMAGE ?= sample-verify-tracer-js-app
PORT ?= 8080
OUTPUT ?= results/local.json

.PHONY: build package start stop check smoke test
build:
	docker build --platform linux/arm64 -t "$(IMAGE)" .

package:
	rm -f ../sample-verify-tracer-js-app.zip
	zip ../sample-verify-tracer-js-app.zip Dockerfile app.js intake.js features.js feature-checks.js delivery.js package.json package-lock.json

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
