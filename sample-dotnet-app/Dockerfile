FROM --platform=linux/arm64 mcr.microsoft.com/dotnet/sdk:8.0 AS build

WORKDIR /build

COPY sample-dotnet-app.csproj .
RUN dotnet restore

COPY Program.cs .
RUN dotnet publish -c Release -o /out

FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023

WORKDIR /app

# AL2023 ships ASP.NET Core 8 directly via dnf; libicu is a transitive
# dependency of the .NET runtime.
RUN dnf install -y aspnetcore-runtime-8.0 libicu \
    lsof net-tools procps-ng htop iproute iputils \
    vim-enhanced nano wget unzip file \
    && dnf clean all

COPY --from=build /out /app

COPY serverless-init-linux-arm64 /serverless-init
RUN chmod +x /serverless-init
COPY .dd-env /app/.dd-env
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080

ENV ASPNETCORE_URLS=http://+:8080
ENV AWS_LAMBDA_FUNCTION_ARN=arn:aws:lambda:us-east-1:123456789012:microvm:sample-dotnet-app
ENV AWS_LAMBDA_MICROVM_IMAGE_ARN=arn:aws:lambda:us-east-1:123456789012:microvm:sample-dotnet-app
ENV AWS_LAMBDA_FUNCTION_NAME=sample-dotnet-app
ENV DD_SITE=datadoghq.com
ENV DD_SERVICE=sample-dotnet-app
ENV DD_LOGS_ENABLED=true
ENV DD_LOG_LEVEL=debug
ENV DD_TRACE_SAMPLE_RATE=1.0
ENV DD_LOGS_INJECTION=true
ENV DD_SERVERLESS_MICROVM_USER_APP_PORT=8080

ENTRYPOINT ["/entrypoint.sh"]
CMD ["dotnet", "sample-dotnet-app.dll"]
