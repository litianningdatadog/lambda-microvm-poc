FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023

WORKDIR /app

RUN dnf install -y \
    ruby rubygems ruby-devel gcc make \
    lsof net-tools procps-ng htop iproute iputils \
    vim-enhanced nano wget unzip file \
    && dnf clean all

RUN gem install bundler --no-document
RUN GEM_HOME=/dd_tracer/ruby gem install datadog --no-document

COPY app.rb Gemfile ./

COPY serverless-init-linux-arm64 /serverless-init
RUN chmod +x /serverless-init

EXPOSE 8080

ENV AWS_LAMBDA_FUNCTION_NAME=sample-ruby-app
ENV DD_SERVICE=sample-ruby-app
ENV DD_SERVERLESS_MICROVM_USER_APP_PORT=8080
ENV GEM_PATH=/dd_tracer/ruby

ENTRYPOINT ["/serverless-init"]
CMD ["ruby", "app.rb"]
