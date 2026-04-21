IMAGE := flask-sidecar-poc
# BINARY_SRC := $(shell git -C "$(CURDIR)" rev-parse --show-toplevel 2>/dev/null)/target/aarch64-unknown-linux-musl/release/datadog-serverless-compat

.PHONY: build run

build:
# 	cp "$(BINARY_SRC)" datadog-serverless-compat
	docker build -t $(IMAGE) .

run:
	docker run --rm -it -p 8080:8080 -p 9000:9000 $(IMAGE)
