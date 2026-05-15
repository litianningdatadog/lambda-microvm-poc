FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023

WORKDIR /app

RUN dnf install -y \
    python3.12 \
    python3.12-pip \
    lsof net-tools procps-ng htop iproute iputils \
    vim-enhanced nano wget unzip file \
    && dnf clean all

COPY requirements.txt .
RUN python3.12 -m pip install --no-cache-dir -r requirements.txt
RUN python3.12 -m pip install --target /dd_tracer/python/ --no-cache-dir ddtrace

COPY app.py .

COPY serverless-init-linux-arm64 /serverless-init
RUN chmod +x /serverless-init

EXPOSE 8080

ENV AWS_LAMBDA_FUNCTION_NAME=sample-python-app
ENV DD_SERVICE=sample-python-app
ENV DD_SERVERLESS_MICROVM_USER_APP_PORT=8080

ENTRYPOINT ["/serverless-init"]
CMD ["/dd_tracer/python/bin/ddtrace-run", "python3.12", "app.py"]
