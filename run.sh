#!/bin/bash
# launchd wrapper: runs the monitor once using the project's venv.
cd "$(dirname "$0")" || exit 1
exec ./venv/bin/python tsa_monitor.py "$@"
