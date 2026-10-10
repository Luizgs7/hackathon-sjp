#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
"$task_root/.venv/bin/python" "$task_root/tools/frontend.py" "${1:-build}"
