#!/bin/bash
set -e
cd "$(dirname "$0")"
set -a
. ./.env
set +a
exec /opt/homebrew/bin/python3 monitor.py
