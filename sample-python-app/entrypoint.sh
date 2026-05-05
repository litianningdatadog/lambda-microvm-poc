#!/bin/sh
# Source .dd-env only when DD_API_KEY isn't already in the environment
# (locally it comes from `docker run -e`; on AWS it comes from this file).
[ -z "$DD_API_KEY" ] && [ -f /app/.dd-env ] && . /app/.dd-env
exec /serverless-init "$@"
