#!/usr/bin/env bash
# Headless Blender for the render pipeline (render_scene.py / compose.py).
# The binary is NOT committed — this script puts a known-good version on any machine.
#
# Linux / WSL2: downloads the portable build from download.blender.org and symlinks
# it into ~/.local/bin. macOS: portable tarballs are not published, use the cask.
set -euo pipefail

VER=5.1.2
SERIES=5.1

case "$(uname -s)" in
  Darwin)
    echo "macOS: run  brew install --cask blender"
    echo "then check:  /Applications/Blender.app/Contents/MacOS/Blender --version"
    echo "and link it: ln -sfn /Applications/Blender.app/Contents/MacOS/Blender ~/.local/bin/blender"
    exit 0
    ;;
esac

DEST="${BLENDER_HOME:-$HOME/.local/opt}"
DIR="$DEST/blender-$VER-linux-x64"

if [ -x "$DIR/blender" ]; then
  echo "already present: $DIR"
else
  mkdir -p "$DEST"
  tmp="$DEST/blender-$VER.tar.xz.partial"
  curl -fL "https://download.blender.org/release/Blender$SERIES/blender-$VER-linux-x64.tar.xz" -o "$tmp"
  tar -xJf "$tmp" -C "$DEST"
  rm "$tmp"
fi

mkdir -p "$HOME/.local/bin"
ln -sfn "$DIR/blender" "$HOME/.local/bin/blender"
"$HOME/.local/bin/blender" --version | head -1
echo "blender -> $DIR (symlinked into ~/.local/bin; ensure ~/.local/bin is on PATH)"
