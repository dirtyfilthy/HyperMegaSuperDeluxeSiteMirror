#!/bin/sh

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)   
. "$SCRIPT_DIR/venv/bin/activate"
python3 "$SCRIPT_DIR/Main/app.py" "$@"
