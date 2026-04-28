FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023 AS build

WORKDIR /build

RUN dnf install -y java-21-amazon-corretto-devel maven && dnf clean all

COPY pom.xml .
COPY src ./src
RUN mvn -B -q package -DskipTests

FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023

WORKDIR /app

# Use the JDK (not the JRE) so the `jdk.jshell` module is available at runtime.
RUN dnf install -y java-21-amazon-corretto-devel \
    lsof net-tools procps-ng htop iproute iputils \
    vim-enhanced nano wget unzip file \
    && dnf clean all

COPY --from=build /build/target/app.jar /app/app.jar

EXPOSE 8080

CMD ["java", "-jar", "/app/app.jar"]
