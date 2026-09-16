#!/bin/bash
set -e
cd /root/weatherbotPreYes0910
set -a
[ -f .env ] && . ./.env
unset YES2RE_LIVE_ENABLE_SUBMIT
unset LIVE_SUBMIT_ENABLED
unset YES2RE_LIVE_CONFIRM
export YES2RE_MODE="paper"
set +a
exec /root/preyes-live/.venv/bin/python reversal_runner.py run --config config/yes2re_reversal.json
