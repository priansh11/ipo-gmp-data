#!/usr/bin/env bash
#
# refresh_gmp.sh — scrape IPO GMP data and push it to your data repo.
#
# One-time setup:
#   1. Create a PUBLIC GitHub repo, e.g.  ipo-gmp-data
#   2. Clone it somewhere, e.g.  ~/ipo-gmp-data
#   3. Copy ipo_gmp_service.py into that repo (or set SCRAPER path below)
#   4. Make this script executable:  chmod +x refresh_gmp.sh
#
# Usage:
#   ./refresh_gmp.sh
#
# Then set in Variables.js:
#   IPO_GMP_URL: "https://raw.githubusercontent.com/<user>/ipo-gmp-data/main/ipo_gmp.json"
#
# To run it automatically every 15 min during market mornings, add to crontab
# (crontab -e):
#   */15 8-10 * * 1-5  /full/path/to/refresh_gmp.sh >> /tmp/gmp_refresh.log 2>&1
#
set -euo pipefail

# ---- config (edit these) --------------------------------------------------
REPO_DIR="${REPO_DIR:-$HOME/ipo-gmp-data}"     # path to your cloned data repo
SCRAPER="${SCRAPER:-$REPO_DIR/ipo_gmp_service.py}"
OUTPUT="${OUTPUT:-$REPO_DIR/ipo_gmp.json}"
BRANCH="${BRANCH:-main}"
PYTHON="${PYTHON:-python3}"
# ---------------------------------------------------------------------------

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Refreshing IPO GMP data"

if [[ ! -f "$SCRAPER" ]]; then
  echo "ERROR: scraper not found at $SCRAPER" >&2
  echo "Copy ipo_gmp_service.py into $REPO_DIR (or set SCRAPER=...)." >&2
  exit 1
fi

# 1) Scrape and write the JSON
if ! "$PYTHON" "$SCRAPER" --write "$OUTPUT"; then
  echo "ERROR: scrape failed - keeping previous JSON, not pushing." >&2
  exit 1
fi

cd "$REPO_DIR"

# 2) Only commit if the file actually changed
if git diff --quiet -- "$(basename "$OUTPUT")"; then
  echo "No change in GMP data - nothing to push."
  exit 0
fi

# 3) Commit and push
git add "$(basename "$OUTPUT")"
git commit -m "gmp: update $(date '+%Y-%m-%d %H:%M')"
git push origin "$BRANCH"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pushed updated GMP data."
