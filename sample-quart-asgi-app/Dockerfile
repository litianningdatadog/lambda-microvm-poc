FROM --platform=linux/arm64 public.ecr.aws/lambda/microvms:al2023-minimal

WORKDIR /app

RUN dnf install -y \
    python3.12 python3.12-pip \
    lsof net-tools procps-ng htop iproute iputils \
    vim-enhanced nano wget unzip file \
    && dnf clean all

COPY requirements.txt .
RUN python3.12 -m pip install --no-cache-dir -r requirements.txt
ARG DD_TRACE_WHEEL=ddtrace-4.15.0rc1-cp312-cp312-linux_aarch64.whl
COPY ${DD_TRACE_WHEEL} /tmp/${DD_TRACE_WHEEL}
RUN python3.12 -m pip install --target /dd_tracer/python/ --no-cache-dir /tmp/${DD_TRACE_WHEEL} \
    && rm -f /tmp/${DD_TRACE_WHEEL}

COPY app.py .

COPY serverless-init-linux-arm64 /serverless-init
RUN chmod +x /serverless-init

EXPOSE 8080

ENV DD_SERVICE=sample-quart-asgi-app
ENV DD_SOURCE=python
ENV DD_LOGS_ENABLED=true
ENV DD_LOGS_INJECTION=true
ENV PYTHONUNBUFFERED=1
ENV DD_AWS_MICROVM_USER_APP_PORT=8080

ENTRYPOINT ["/serverless-init"]
CMD ["/dd_tracer/python/bin/ddtrace-run", "python3.12", "app.py"]
