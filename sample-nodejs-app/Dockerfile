FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023

WORKDIR /app

RUN dnf install -y \
    nodejs npm \
    lsof net-tools procps-ng htop iproute iputils \
    vim-enhanced nano wget unzip file \
    && dnf clean all

COPY package.json .
RUN npm install --omit=dev
#RUN npm install --prefix /dd_tracer/node dd-trace  --save

ARG DD_TRACE_TGZ=dd-trace-local.tgz
COPY ${DD_TRACE_TGZ} /tmp/dd-trace-local.tgz
RUN npm install --prefix /dd_tracer/node /tmp/dd-trace-local.tgz

COPY app.js .

COPY serverless-init-linux-arm64 /serverless-init
RUN chmod +x /serverless-init

EXPOSE 8080

ENV AWS_LAMBDA_FUNCTION_NAME=sample-nodejs-app
ENV DD_SERVICE=sample-nodejs-app
ENV DD_SOURCE=nodejs
ENV DD_LOGS_ENABLED=true
ENV DD_LOGS_INJECTION=true
ENV DD_SERVERLESS_MICROVM_USER_APP_PORT=8080

ENTRYPOINT ["/serverless-init"]
CMD ["node", "app.js"]
