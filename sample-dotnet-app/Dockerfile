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

EXPOSE 8080
ENV ASPNETCORE_URLS=http://+:8080

CMD ["dotnet", "sample-dotnet-app.dll"]
