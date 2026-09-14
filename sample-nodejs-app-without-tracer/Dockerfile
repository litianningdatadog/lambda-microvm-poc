FROM --platform=linux/arm64 public.ecr.aws/lambda/microvms:al2023-minimal

WORKDIR /app

RUN dnf install -y \
    nodejs22 nodejs22-npm \
    lsof net-tools procps-ng htop iproute iputils \
    vim-enhanced nano wget unzip file \
    && dnf clean all

COPY package*.json ./
RUN npm ci --omit=dev

COPY app.js .

# Old local serverless-init copy path, kept for easy revert:
# COPY serverless-init-linux-arm64 /serverless-init
# RUN chmod +x /serverless-init
COPY --from=datadog/serverless-init:1.10.4 /datadog-init /serverless-init

EXPOSE 8080

ENV DD_SERVICE=sample-nodejs-app-without-tracer
ENV DD_SOURCE=nodejs
ENV DD_LOGS_ENABLED=true
ENV DD_AWS_MICROVM_USER_APP_PORT=8080
ENV DD_ENHANCED_METRICS_ENABLED=true

ENTRYPOINT ["/serverless-init"]
CMD ["node", "app.js"]
