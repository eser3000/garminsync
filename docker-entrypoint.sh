#!/bin/sh
set -e
export PYTHONPATH=/app
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}"
