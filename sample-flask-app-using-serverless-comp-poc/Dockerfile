FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023

WORKDIR /app

# Install Python 3.11 and common debugging tools
RUN dnf install -y \
    python3.11 python3.11-pip \
    lsof net-tools procps-ng htop iproute iputils \
    vim-enhanced nano wget unzip file htop\
    && dnf clean all

# Datadog serverless-compat binary (linux/arm64, statically linked musl).
# Built from: crates/datadog-serverless-compat
# Target:     aarch64-unknown-linux-musl  (opt-level=z, lto=fat, strip=true)
# Statically linked — runs on AL2023 glibc without modification.
COPY datadog-serverless-compat /datadog-serverless-compat
RUN chmod +x /datadog-serverless-compat

COPY requirements.txt .
RUN pip3.11 install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8080 9000
# Port 9000 is reserved for the serverless-compat lifecycle hook server.

# serverless-compat runs as PID 1 (init mode).
# Init mode requires AWS_LAMBDA_FUNCTION_ARN to contain "microvm" (set by the
# Lambda MicroVM runtime) and the binary to be invoked with a child command.
# It wraps "python3.11 app.py": captures stdout/stderr into the Datadog log
# pipeline, forwards Unix signals to the child, and propagates its exit code.
ENV AWS_LAMBDA_FUNCTION_NAME=serverless-compat-poc-id
# Required by the trace-agent's read_cloud_env() to identify the environment.
# In a real MicroVM the Lambda runtime sets this automatically alongside the ARN.
ENV DD_SITE=datadoghq.com
ENV DD_SERVICE=serverless-compat-poc-id
ENV DD_LOGS_ENABLED=true
ENV DD_LOG_LEVEL=debug
ENV DD_TRACE_SAMPLE_RATE=1.0
ENV DD_LOGS_INJECTION=true

ENTRYPOINT ["/datadog-serverless-compat"]
CMD ["python3.11", "app.py"]
