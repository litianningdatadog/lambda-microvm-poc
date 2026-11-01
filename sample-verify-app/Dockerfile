FROM --platform=linux/arm64 public.ecr.aws/lambda/microvms:al2023-minimal

WORKDIR /app

RUN dnf install -y python3.12 python3.12-pip && dnf clean all

COPY requirements.txt .
RUN python3.12 -m pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Local serverless-init build (copied in by `./verify.sh deploy` from $SERVERLESS_INIT_BIN).
COPY serverless-init-linux-arm64 /serverless-init
RUN chmod +x /serverless-init
# Released image, kept for easy revert:
# COPY --from=datadog/serverless-init:1.10.4 /datadog-init /serverless-init

EXPOSE 8080

# DD_API_KEY / DD_SITE / DD_VERSION are passed as image environment variables
# by verify.sh. DD_AWS_MICROVM_USER_APP_PORT is NOT set here: serverless-init
# answers every lifecycle hook itself, except on the `fwd` image, where
# verify.sh sets it (and HOOK_PORT) so hooks are forwarded to app.py.
ENV DD_SERVICE=sample-verify-app
ENV DD_SOURCE=python
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/serverless-init"]
CMD ["python3.12", "app.py"]
