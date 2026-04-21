FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023

WORKDIR /app

# Install Python 3.11 and common debugging tools
RUN dnf install -y \
    python3.11 python3.11-pip \
    lsof net-tools procps-ng htop iproute iputils \
    vim-enhanced nano wget unzip file \
    && dnf clean all

# Datadog serverless-init binary (linux/arm64).
# Download: https://github.com/DataDog/serverless-init (release asset: serverless-init-linux-arm64)
# Statically linked — runs on AL2023 without extra runtime dependencies.
COPY serverless-init-linux-arm64 /serverless-init
RUN chmod +x /serverless-init

COPY requirements.txt .
RUN pip3.11 install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 9000 8080
# 9000 = serverless-init lifecycle hook server; 8080 = user-app port.

# serverless-init runs as PID 1 (init mode).
# Init mode requires AWS_LAMBDA_FUNCTION_ARN to contain "microvm" (set by the
# Lambda MicroVM runtime) and the binary to be invoked with a child command.
# It wraps "python3.11 app.py": captures stdout/stderr into the Datadog log
# pipeline, forwards Unix signals to the child, and propagates its exit code.
# Fake MicroVM ARN so serverless-init activates init mode locally.
# Remove this ENV line when deploying to a real Lambda MicroVM environment.
ENV AWS_LAMBDA_FUNCTION_ARN=arn:aws:lambda:us-east-1:123456789012:microvm:serverless-init-poc-id
# Required by the trace-agent's read_cloud_env() to identify the environment.
# In a real MicroVM the Lambda runtime sets this automatically alongside the ARN.
ENV AWS_LAMBDA_FUNCTION_NAME=serverless-init-poc-id
ENV DD_API_KEY=<YOUR_API_KEY>
ENV DD_SITE=datadoghq.com
ENV DD_SERVICE=serverless-init-poc-id
ENV DD_LOGS_ENABLED=true

ENTRYPOINT ["/serverless-init"]
CMD ["python3.11", "app.py"]
