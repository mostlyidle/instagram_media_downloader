#!/usr/bin/env bash
set -euo pipefail

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium

echo "Install ffmpeg separately if needed: brew install ffmpeg"
echo "Then run: python -m ig_media_downloader login"
echo "Then run: python -m ig_media_downloader download 'https://www.instagram.com/USERNAME/' --show-browser --max-posts 10"
