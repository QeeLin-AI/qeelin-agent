#!/usr/bin/env bash
# 用法: ./run.sh demo
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
export PYTHONPATH="$PWD/src"
exec "${PYTHON:-python3}" -m cryptoagent "$@"