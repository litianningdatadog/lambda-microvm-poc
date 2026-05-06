FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023

WORKDIR /app

RUN dnf install -y \
    nodejs npm \
    lsof net-tools procps-ng htop iproute iputils \
    vim-enhanced nano wget unzip file \
    && dnf clean all

COPY package.json .
RUN npm install --omit=dev
RUN npm install dd-trace

COPY app.js .

COPY serverless-init-linux-arm64 /serverless-init
RUN chmod +x /serverless-init
COPY .dd-env /app/.dd-env
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080

ENV AWS_LAMBDA_FUNCTION_ARN=arn:aws:lambda:us-east-1:123456789012:microvm:sample-nodejs-app
ENV AWS_LAMBDA_MICROVM_IMAGE_ARN=arn:aws:lambda:us-east-1:123456789012:microvm:sample-nodejs-app
ENV AWS_LAMBDA_FUNCTION_NAME=sample-nodejs-app
ENV DD_SITE=datadoghq.com
ENV DD_SERVICE=sample-nodejs-app
ENV DD_LOGS_ENABLED=true
ENV DD_LOG_LEVEL=debug
ENV DD_TRACE_SAMPLE_RATE=1.0
ENV DD_LOGS_INJECTION=true
ENV DD_TRACE_ENABLED=true
ENV DD_SERVERLESS_MICROVM_USER_APP_PORT=8080

ENTRYPOINT ["/entrypoint.sh"]
CMD ["node", "--require", "dd-trace/init", "app.js"]
