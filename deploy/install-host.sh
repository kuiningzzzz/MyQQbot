#!/usr/bin/env bash
# Native deployment: dsh owner commands inherit this service user's OS rights.
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$project_dir"
command -v node >/dev/null || { echo 'Install Node.js >=24 first.' >&2; exit 1; }
node -e 'if (Number(process.versions.node.split(".")[0]) < 24) process.exit(1)'
python3 -m venv .venv
.venv/bin/python -m pip install --index-url https://download.pytorch.org/whl/cpu torch
.venv/bin/python -m pip install -r requirements.txt
(cd dsh && npm ci --omit=dev --no-audit --no-fund)
.venv/bin/python scripts/migrate_ai.py
echo 'Dependencies and migration complete. Install Tesseract (chi_sim/chi_tra/eng), Poppler and fonts-noto-cjk for OCR.'
echo "Run: cd '$project_dir' && .venv/bin/python -m src.main"
echo 'For autostart, customize deploy/myqqbot.service and install it with systemctl.'
