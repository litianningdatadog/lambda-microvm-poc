FROM --platform=linux/arm64 public.ecr.aws/amazonlinux/amazonlinux:2023

# =============================================================================
# PLATFORM LAYER — everything here would live in a base image
# ("microvm-dd-base:latest"). A real user would never see or touch this.
# =============================================================================

WORKDIR /opt/platform

# --- Base tools, Python 3.11, envsubst ------------------------------------
# Note: `supervisor` is NOT in AL2023's default dnf repos; we install it
# via pip below (it's pure Python, works the same way).
RUN dnf install -y \
      python3.11 python3.11-pip \
      gettext \
      procps-ng iproute iputils htop \
      shadow-utils tar \
    && dnf clean all

# --- Datadog Agent 7 (aarch64 channel) -------------------------------------
# Installed at build time, started at /launch time. auth_token is wiped on
# every boot by entrypoint.sh so each cloned MicroVM regenerates its own.
RUN printf '%s\n' \
      '[datadog]' \
      'name=Datadog, Inc.' \
      'baseurl=https://yum.datadoghq.com/stable/7/aarch64/' \
      'enabled=1' 'gpgcheck=1' 'repo_gpgcheck=1' 'priority=1' \
      'gpgkey=https://keys.datadoghq.com/DATADOG_RPM_KEY_CURRENT.public' \
      > /etc/yum.repos.d/datadog.repo \
  && dnf install -y datadog-agent \
  && dnf clean all \
  && rm -f /etc/datadog-agent/auth_token

# --- Platform Python deps --------------------------------------------------
# supervisor  — PID 1 supervisor (not in AL2023 dnf; pip has it)
# flask       — hook_server.py HTTP server
# datadog     — DogStatsD client for lifecycle events
# ddtrace     — APM auto-instrumentation wrapper (`ddtrace-run`)
# These are separate from the user-app's requirements.txt on purpose: the
# user doesn't need to know the platform uses any of these internally.
RUN pip3.11 install --no-cache-dir \
      "supervisor>=4.2.0" \
      "flask>=2.0.0" \
      "datadog>=0.47.0" \
      "ddtrace>=2.0.0"

# --- Platform config + hook-server binary ----------------------------------
COPY datadog.yaml.template /etc/datadog-agent/datadog.yaml.template
COPY user-app-logs.yaml    /etc/datadog-agent/conf.d/user-app.d/conf.yaml
RUN chown -R dd-agent:dd-agent /etc/datadog-agent \
 && mkdir -p /var/run/datadog \
 && chown -R dd-agent:dd-agent /var/run/datadog

COPY hook_server.py    /opt/platform/hook_server.py
COPY supervisord.conf  /etc/supervisord.conf
COPY entrypoint.sh     /opt/platform/entrypoint.sh
# Optional: deploy-microvm.sh generates .dd-env containing DD_API_KEY at
# zip time. Wildcard makes this a no-op if the file is absent (local
# docker build without running deploy-microvm.sh first).
COPY .dd-env*          /opt/platform/
RUN chmod +x /opt/platform/entrypoint.sh \
 && mkdir -p /var/log/app /var/log/supervisor \
 && chown -R dd-agent:dd-agent /var/log/app

# =============================================================================
# USER-APP LAYER — in reality this is everything a real app would need to
# ship: their requirements.txt, their app.py, and that's it. Their Dockerfile
# would read `FROM microvm-dd-base:latest` followed by these three lines.
# =============================================================================

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip3.11 install --no-cache-dir -r /app/requirements.txt
COPY app.py /app/app.py

# =============================================================================
# --- Ports -----------------------------------------------------------------
# 9000: lifecycle hooks (platform — owned by hook_server.py)
# 8080: user app
# 8125/udp: DogStatsD
# 8126: APM trace-agent
EXPOSE 8080 9000 8125/udp 8126

# --- Faked-for-local env (remove in real MicroVM deploys) ------------------
# Required by the trace-agent's read_cloud_env() to identify the environment.
# In a real MicroVM the Lambda runtime sets this automatically alongside the ARN.
ENV AWS_LAMBDA_FUNCTION_NAME=sample-flask-app-datadog-agent-poc
# DD_API_KEY comes from /opt/platform/.dd-env (shipped by
# deploy-microvm.sh at zip time), sourced in entrypoint.sh.
# For local `docker run`, pass it via `-e DD_API_KEY=...` instead.
ENV DD_SITE=datadoghq.com
ENV DD_SERVICE=sample-flask-app-datadog-agent-poc
ENV DD_LOGS_ENABLED=true
ENV DD_LOG_LEVEL=debug

ENTRYPOINT ["/opt/platform/entrypoint.sh"]
