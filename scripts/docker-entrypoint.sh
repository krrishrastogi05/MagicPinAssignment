#!/bin/sh
set -eu

mkdir -p /data
chown -R vera:vera /data

exec runuser -u vera -- uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8080}" \
  --workers 1
