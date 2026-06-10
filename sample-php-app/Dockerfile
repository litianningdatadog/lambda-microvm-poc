FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023

WORKDIR /app

RUN dnf install -y \
    php \
    php-cli \
    php-json \
    lsof net-tools procps-ng htop iproute iputils \
    vim-enhanced nano wget unzip file curl \
    && dnf clean all

# Install DD PHP tracer (auto-instruments PHP when the extension is loaded)
RUN curl -LO https://github.com/DataDog/dd-trace-php/releases/latest/download/datadog-setup.php \
    && php datadog-setup.php --php-bin=all \
    && rm datadog-setup.php

COPY app.php .

COPY serverless-init-linux-arm64 /serverless-init
RUN chmod +x /serverless-init

EXPOSE 8080

ENV DD_SERVICE=sample-php-app
ENV DD_SOURCE=php
ENV DD_LOGS_ENABLED=true
ENV DD_LOGS_INJECTION=true
ENV DD_AWS_MICROVM_USER_APP_PORT=8080

ENTRYPOINT ["/serverless-init"]
CMD ["php", "-S", "0.0.0.0:8080", "/app/app.php"]
