#!/usr/bin/env python3
"""Generate deterministic test clips. Python, not shell — zsh word-splitting mangled
colour args and concatenated `darkslateblue`+`6` into `darkslateblue6` last time."""
import subprocess, sys
from pathlib import Path

COLOURS = ["0x2a3d45", "0xd4713a", "0x6b8f7a", "0x8a6f9e", "0xc4a35a"]

def make(out_dir: Path, n=3, seconds=3, w=384, h=384, fps=30, audio=True):
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(n):
        p = out_dir / f"clip{i}.mp4"
        args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"color=c={COLOURS[i % len(COLOURS)]}:s={w}x{h}:r={fps}"]
        if audio:
            args += ["-f", "lavfi", "-i", f"sine=frequency={200*(i+1)}"]
        args += ["-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        if audio:
            args += ["-c:a", "aac"]
        args += [str(p)]
        subprocess.run(args, check=True)
        paths.append(p)
    return paths

if __name__ == "__main__":
    d = Path(sys.argv[1] if len(sys.argv) > 1 else "fixtures")
    for p in make(d):
        print(p)
