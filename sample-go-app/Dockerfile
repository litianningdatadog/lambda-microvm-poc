FROM --platform=linux/arm64 golang:1.24 AS build

WORKDIR /build

COPY go.mod ./
COPY main.go ./
RUN go mod tidy && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o /out/server .

FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023

WORKDIR /app

RUN dnf install -y \
    lsof net-tools procps-ng htop iproute iputils \
    vim-enhanced nano wget unzip file \
    && dnf clean all

COPY --from=build /out/server /app/server

COPY serverless-init-linux-arm64 /serverless-init
RUN chmod +x /serverless-init

EXPOSE 8080

ENV DD_SERVICE=sample-go-app
ENV DD_SOURCE=go
ENV DD_LOGS_ENABLED=true
ENV DD_LOGS_INJECTION=true
ENV DD_SERVERLESS_MICROVM_USER_APP_PORT=8080

ENTRYPOINT ["/serverless-init"]
CMD ["/app/server"]
