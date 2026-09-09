FROM --platform=linux/arm64 public.ecr.aws/lambda/microvms:al2023-minimal AS build

WORKDIR /build

RUN dnf install -y java-21-amazon-corretto-devel maven && dnf clean all

ENV JAVA_HOME=/usr/lib/jvm/java-21-amazon-corretto.aarch64

COPY pom.xml .
COPY src ./src
RUN mvn -B -q package -DskipTests

FROM --platform=linux/arm64 public.ecr.aws/lambda/microvms:al2023-minimal

WORKDIR /app

# Use the JDK (not the JRE) so the `jdk.jshell` module is available at runtime.
RUN dnf install -y java-21-amazon-corretto-devel \
    lsof net-tools procps-ng htop iproute iputils \
    vim-enhanced nano wget unzip file \
    && dnf clean all

COPY --from=build /build/target/app.jar /app/app.jar
RUN mkdir -p /dd_tracer/java
#curl -L https://dtdg.co/latest-java-tracer -o /dd_tracer/java/dd-java-agent.jar
COPY dd-java-agent-1.64.0-SNAPSHOT.jar /dd_tracer/java/dd-java-agent.jar

# Old local serverless-init copy path, kept for easy revert:
# COPY serverless-init-linux-arm64 /serverless-init
# RUN chmod +x /serverless-init
COPY --from=datadog/serverless-init:1.10.4 /datadog-init /serverless-init

EXPOSE 8080

ENV DD_SERVICE=sample-java-app
ENV DD_SOURCE=java
ENV DD_LOGS_ENABLED=true
ENV DD_LOGS_INJECTION=true
ENV DD_AWS_MICROVM_USER_APP_PORT=8080

ENTRYPOINT ["/serverless-init"]
CMD ["java", "-javaagent:/dd_tracer/java/dd-java-agent.jar", "-jar", "/app/app.jar"]
