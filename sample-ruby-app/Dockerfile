FROM --platform=linux/arm64 public.ecr.aws/lambda/microvms:al2023-minimal

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

ENV DD_SERVICE=sample-ruby-app
ENV DD_SOURCE=ruby
ENV DD_LOGS_ENABLED=true
ENV DD_LOGS_INJECTION=true
ENV DD_AWS_MICROVM_USER_APP_PORT=8080
ENV GEM_PATH=/dd_tracer/ruby

ENTRYPOINT ["/serverless-init"]
CMD ["ruby", "app.rb"]
