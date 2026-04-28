#!/bin/bash
echo "Starting on port: $PORT"
exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
