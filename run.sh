#!/usr/bin/env bash
# start the p4-merger web app
cd "$(dirname "$0")" && exec python3 server.py "$@"
