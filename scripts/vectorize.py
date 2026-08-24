"""Raster → SVG vectorize helper for generate_media.py.

Stdlib-only (no google-genai / PIL / etc.) so this module — and its behavior — can be
imported and tested directly under plain `python3`, without the uv-managed deps that
generate_media.py needs. generate_media.py imports this lazily inside its command
functions; keep it that way.
"""

import shutil
import subprocess
import sys
from pathlib import Path


def _vectorize_file(path: Path) -> Path | None:
    """vtracer + svgo a raster into <stem>.svg beside it. Returns the svg path, or None if
    the tools are unavailable (warned on stderr)."""
    if path.suffix.lower() == ".svg":
        svg_path = path
    else:
        if shutil.which("vtracer") is None:
            print(
                "Warning: vtracer not found — skipping vectorize. "
                "Install: cargo install vtracer (or download a release binary)",
                file=sys.stderr,
            )
            return None

        svg_path = path.with_suffix(".svg")
        proc = subprocess.run(
            ["vtracer", "--input", str(path), "--output", str(svg_path)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print(
                f"Warning: vtracer failed on {path} (exit {proc.returncode}): "
                f"{proc.stderr.strip()} — falling back to the raster.",
                file=sys.stderr,
            )
            return None

    if shutil.which("svgo") is not None:
        svgo_cmd = ["svgo", str(svg_path), "-o", str(svg_path)]
    elif shutil.which("npx") is not None:
        svgo_cmd = ["npx", "-y", "svgo", str(svg_path), "-o", str(svg_path)]
    else:
        print(
            "Warning: svgo not found (and no npx to fall back to) — keeping the "
            "unoptimized SVG. Install: npm i -g svgo",
            file=sys.stderr,
        )
        return svg_path

    proc = subprocess.run(svgo_cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(
            f"Warning: svgo failed on {svg_path} (exit {proc.returncode}): "
            f"{proc.stderr.strip()} — keeping the unoptimized SVG.",
            file=sys.stderr,
        )

    return svg_path
