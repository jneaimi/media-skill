#!/usr/bin/env bash
# Fetch the CC0 source packs the cast bible builds from. Assets are NOT committed —
# this script recreates skills/media/cast/assets/ on any machine. Requires: python3 + pip.
# All three packs are Quaternius, CC0 1.0 (see licenses.md).
set -euo pipefail
cd "$(dirname "$0")"

VENV=.fetch-venv
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet gdown

fetch() { # name, drive folder id
  # Download into a temp dir and move into place atomically — a failed/interrupted gdown must
  # not leave a partial assets/$1 that poisons every retry's "already present" check.
  [ -d "assets/$1" ] && { echo "assets/$1 already present, skipping"; return; }
  mkdir -p assets
  tmp=$(mktemp -d "assets/.$1.partial.XXXXXX")
  "$VENV/bin/gdown" --folder "https://drive.google.com/drive/folders/$2" -O "$tmp"
  mv "$tmp" "assets/$1"
}

fetch animatedmen   17LibivOaUidsQhSkcxP3YYvDr0n7wIwu
fetch animatedwomen 1c13R--fMqdR6r2MRlcKKsbPky0__T-yJ
fetch furniture     1CLWStkb7cipC1ZdTunYJXKVqEVwtXWXK

echo "Done. Verify each pack's License.txt says CC0 1.0 (cross-check licenses.md)."
