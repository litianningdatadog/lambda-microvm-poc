IMAGE := flask-serverless-compat-poc
CONTAINER := flask-serverless-compat-poc
# BINARY_SRC := $(shell git -C "$(CURDIR)" rev-parse --show-toplevel 2>/dev/null)/target/aarch64-unknown-linux-musl/release/datadog-serverless-compat
DD_API_KEY ?= dummy-api-key
DD_LOG_LEVEL ?= debug
RUN_FLAGS := -p 8080:8080 -p 9000:9000 \
	-e DD_API_KEY=$(DD_API_KEY) \
	-e DD_LOG_LEVEL=$(DD_LOG_LEVEL)

.PHONY: build run

build:
# 	cp "$(BINARY_SRC)" datadog-serverless-compat
	docker build -t $(IMAGE) .

run:
	docker run --rm -it --name $(CONTAINER)  $(RUN_FLAGS) $(IMAGE)
