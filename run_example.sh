#!/usr/bin/env bash
set -euo pipefail

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
python -m playwright install chromium

echo "Install ffmpeg separately if needed: brew install ffmpeg"
echo "Then run: ig-scraper login"
echo "Then run: ig-scraper --user USERNAME --show-browser --max-posts 10"
