IMAGE ?= sample-verify-tracer-app
PORT ?= 8080
WHEEL ?= /Users/tianning.li/DataDog/dd-trace-py/dist/ddtrace-4.17.0rc1-cp312-cp312-linux_aarch64.whl
WHEEL_NAME = $(notdir $(WHEEL))
SERVERLESS_INIT_BIN ?= /Users/tianning.li/DataDog/datadog-agent/serverless-init-linux-arm64
OUTPUT ?= results/latest.json

.PHONY: prepare build start stop check package test
prepare:
	cp "$(WHEEL)" "$(WHEEL_NAME)"
	cp "$(SERVERLESS_INIT_BIN)" serverless-init-linux-arm64
	chmod +x serverless-init-linux-arm64

build: prepare
	docker build --platform linux/arm64 --build-arg DD_TRACE_WHEEL="$(WHEEL_NAME)" -t "$(IMAGE)" .

# Bypass serverless-init locally: intake is captured in-process; no API key needed.
start:
	docker run --rm -d --name "$(IMAGE)" -p "$(PORT):8080" \
		-e AWS_LAMBDA_MICROVM_IMAGE_ARN=local-verification \
		--entrypoint /dd_tracer/python/bin/ddtrace-run "$(IMAGE)" python3.12 app.py

stop:
	docker stop "$(IMAGE)"

check:
	python3 verify.py --url "http://localhost:$(PORT)" --output "$(OUTPUT)"

test:
	python3 -m unittest -v test_check test_feature_checks test_lifecycle_checks test_retirement_checks test_delivery test_delivery_checks test_cloud

package: prepare
	zip ../sample-verify-tracer-app.zip Dockerfile app.py intake.py delivery.py delivery_checks.py features.py feature_checks.py lifecycle_checks.py lifecycle_control.py retirement_checks.py retirement_control.py requirements.txt "$(WHEEL_NAME)" serverless-init-linux-arm64

# Runs separate processes so mode detection happens before ddtrace is imported.
.PHONY: check-lifecycle
check-lifecycle:
	docker run --rm -e AWS_LAMBDA_MICROVM_IMAGE_ARN= --entrypoint python3.12 "$(IMAGE)" lifecycle_control.py regular
	docker run --rm -e AWS_LAMBDA_MICROVM_IMAGE_ARN=local-verification --entrypoint python3.12 "$(IMAGE)" lifecycle_control.py blocked-send
	docker run --rm -e AWS_LAMBDA_MICROVM_IMAGE_ARN= --entrypoint python3.12 "$(IMAGE)" retirement_control.py
	docker run --rm -e AWS_LAMBDA_MICROVM_IMAGE_ARN=local-verification --entrypoint python3.12 "$(IMAGE)" retirement_control.py
