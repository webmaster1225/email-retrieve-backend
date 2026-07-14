#!/bin/bash
set -e
cd /home/site/wwwroot

if [ -f /home/site/wwwroot/antenv/bin/activate ]; then
  # shellcheck disable=SC1091
  source /home/site/wwwroot/antenv/bin/activate
fi

export PORT="${PORT:-8000}"

exec python -m gunicorn app.main:app \
  --workers 1 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind "0.0.0.0:${PORT}" \
  --timeout 600 \
  --access-logfile - \
  --error-logfile -
