FROM --platform=linux/arm64 ubuntu:24.04

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 \
    python3-pip \
    lsof net-tools procps htop iproute2 iputils-ping \
    vim nano wget unzip file curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python3.12 -m pip install --no-cache-dir --break-system-packages -r requirements.txt
ARG DD_TRACE_WHEEL=ddtrace-4.9.0rc2-cp312-cp312-linux_aarch64.whl
COPY ${DD_TRACE_WHEEL} /tmp/${DD_TRACE_WHEEL}
RUN python3.12 -m pip install --target /dd_tracer/python/ --no-cache-dir --break-system-packages /tmp/${DD_TRACE_WHEEL} \
    && rm -f /tmp/${DD_TRACE_WHEEL}

COPY app.py .

COPY serverless-init-linux-arm64 /serverless-init
RUN chmod +x /serverless-init

EXPOSE 8080

ENV AWS_LAMBDA_FUNCTION_NAME=sample-python-app
ENV DD_SERVICE=sample-python-app
ENV DD_SERVERLESS_MICROVM_USER_APP_PORT=8080

ENTRYPOINT ["/serverless-init"]
CMD ["/dd_tracer/python/bin/ddtrace-run", "python3.12", "app.py"]
