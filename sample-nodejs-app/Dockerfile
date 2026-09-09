FROM --platform=linux/arm64 public.ecr.aws/lambda/microvms:al2023-minimal

WORKDIR /app

RUN dnf install -y \
    nodejs22 nodejs22-npm \
    lsof net-tools procps-ng htop iproute iputils \
    vim-enhanced nano wget unzip file \
    && dnf clean all

# Old local-tgz tracer install path, kept for easy revert:
# COPY package.json .
# RUN npm install --omit=dev
# #RUN npm install --prefix /dd_tracer/node dd-trace  --save
# ARG DD_TRACE_TGZ=dd-trace-local.tgz
# COPY ${DD_TRACE_TGZ} /tmp/dd-trace-local.tgz
# RUN npm install --prefix /dd_tracer/node /tmp/dd-trace-local.tgz

COPY package*.json ./
RUN npm ci --omit=dev

COPY app.js .

# Old local serverless-init copy path, kept for easy revert:
# COPY serverless-init-linux-arm64 /serverless-init
# RUN chmod +x /serverless-init
COPY --from=datadog/serverless-init:1.10.4 /datadog-init /serverless-init

EXPOSE 8080

ENV NODE_OPTIONS="--require dd-trace/init"
ENV DD_SERVICE=sample-nodejs-app
ENV DD_SOURCE=nodejs
ENV DD_LOGS_ENABLED=true
ENV DD_LOGS_INJECTION=true
ENV DD_AWS_MICROVM_USER_APP_PORT=8080

ENTRYPOINT ["/serverless-init"]
CMD ["node", "app.js"]
