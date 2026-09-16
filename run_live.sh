#!/bin/bash
set -e
cd /root/weatherbotLive0915
set -a
[ -f .env ] && . ./.env
export YES2RE_LIVE_ENABLE_SUBMIT=1
export LIVE_SUBMIT_ENABLED=1
export YES2RE_LIVE_CONFIRM=$(/root/preyes-live/.venv/bin/python -c 'from live import submit; print(submit.phrase())')
set +a
exec /root/preyes-live/.venv/bin/python reversal_runner.py run --config config/yes2re_reversal.json
