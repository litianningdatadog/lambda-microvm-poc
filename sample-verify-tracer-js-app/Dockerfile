FROM --platform=linux/arm64 public.ecr.aws/lambda/microvms:al2023-minimal
WORKDIR /app
# AL2023's Node links snapsafe libcrypto. Official node images bundle OpenSSL
# and cannot prove clone-safe entropy even when local lifecycle checks pass.
RUN dnf install -y nodejs22 nodejs22-npm && dnf clean all
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY app.js intake.js features.js feature-checks.js delivery.js ./
COPY --from=datadog/serverless-init:1.11.4 /datadog-init /serverless-init
ENV DD_SERVICE=sample-verify-tracer-js-app DD_SOURCE=nodejs \
    DD_LOGS_ENABLED=true DD_LOGS_INJECTION=true \
    DD_TRACE_AGENT_URL=http://127.0.0.1:18126 DD_DOGSTATSD_PORT=18125 \
    DD_RUNTIME_METRICS_ENABLED=true DD_RUNTIME_METRICS_RUNTIME_ID_ENABLED=true \
    DD_INSTRUMENTATION_TELEMETRY_ENABLED=true \
    DD_AWS_MICROVM_USER_APP_PORT=8080 DD_AWS_MICROVM_FORWARD_TIMEOUT_MS=15000
EXPOSE 8080 9000
ENTRYPOINT ["/serverless-init"]
CMD ["node", "app.js"]
