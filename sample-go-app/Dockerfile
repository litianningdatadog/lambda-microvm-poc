FROM --platform=linux/arm64 public.ecr.aws/docker/library/golang:1.22 AS build

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

EXPOSE 8080

CMD ["/app/server"]
