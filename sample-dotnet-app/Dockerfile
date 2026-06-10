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
COPY --from=datadog/dd-lib-dotnet-init /datadog-init/monitoring-home/ /dd_dotnet/

COPY serverless-init-linux-arm64 /serverless-init
RUN chmod +x /serverless-init

EXPOSE 8080

ENV ASPNETCORE_URLS=http://+:8080
ENV DD_SERVICE=sample-dotnet-app
ENV DD_SOURCE=csharp
ENV DD_LOGS_ENABLED=true
ENV DD_LOGS_INJECTION=true
ENV CORECLR_ENABLE_PROFILING=1
ENV CORECLR_PROFILER={846F5F1C-F9AE-4B07-969E-05C26BC060D8}
ENV CORECLR_PROFILER_PATH=/dd_dotnet/Datadog.Trace.ClrProfiler.Native.so
ENV DD_DOTNET_TRACER_HOME=/dd_dotnet
ENV DD_SERVERLESS_MICROVM_USER_APP_PORT=8080

ENTRYPOINT ["/serverless-init"]
CMD ["dotnet", "sample-dotnet-app.dll"]
