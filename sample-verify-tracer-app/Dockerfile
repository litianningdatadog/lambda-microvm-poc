FROM --platform=linux/arm64 public.ecr.aws/lambda/microvms:al2023-minimal
WORKDIR /app
RUN dnf install -y python3.12 python3.12-pip && dnf clean all
COPY requirements.txt .
RUN python3.12 -m pip install --no-cache-dir -r requirements.txt
ARG DD_TRACE_WHEEL=ddtrace-4.17.0rc1-cp312-cp312-linux_aarch64.whl
COPY ${DD_TRACE_WHEEL} /tmp/${DD_TRACE_WHEEL}
RUN python3.12 -m pip install --target /dd_tracer/python/ --no-cache-dir /tmp/${DD_TRACE_WHEEL} \
    && sha256sum /tmp/${DD_TRACE_WHEEL} > /app/wheel.sha256 \
    && rm /tmp/${DD_TRACE_WHEEL}
COPY app.py intake.py delivery.py delivery_checks.py features.py feature_checks.py lifecycle_checks.py lifecycle_control.py .
# Copied into the build context by make prepare, matching sample-verify-app.
COPY serverless-init-linux-arm64 /serverless-init
RUN chmod +x /serverless-init
ENV PYTHONPATH=/dd_tracer/python PYTHONUNBUFFERED=1 \
    DD_SERVICE=sample-verify-tracer-app DD_SOURCE=python \
    DD_TRACE_AGENT_URL=http://127.0.0.1:18126 DD_DOGSTATSD_URL=udp://127.0.0.1:18125 \
    DD_TRACE_API_VERSION=v0.4 DD_TRACE_SAMPLE_RATE=1 \
    DD_INSTRUMENTATION_TELEMETRY_ENABLED=true DD_RUNTIME_METRICS_ENABLED=true \
    DD_RUNTIME_METRICS_RUNTIME_ID_ENABLED=true DD_TRACE_EXPERIMENTAL_RUNTIME_ID_ENABLED=true \
    DD_AWS_MICROVM_USER_APP_PORT=8080 DD_AWS_MICROVM_FORWARD_TIMEOUT_MS=15000
EXPOSE 8080 9000
ENTRYPOINT ["/serverless-init"]
CMD ["/dd_tracer/python/bin/ddtrace-run", "python3.12", "app.py"]
