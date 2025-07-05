#!/bin/sh
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)   
rm -rf $SCRIPT_DIR/venv
python -m venv "$SCRIPT_DIR/venv"
. "$SCRIPT_DIR/venv/bin/activate"
pip3 install -r "$SCRIPT_DIR/requirements.txt"
