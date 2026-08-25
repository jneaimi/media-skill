"""Timed captions, hook text, CTA end cards and AI-disclosure marks for social video ads.

## Why reshaping

AI image and video models cannot render Arabic. They draw letterforms without applying
Arabic shaping (contextual letter connection) or the Unicode bidirectional algorithm, so
generated Arabic comes out as disconnected, garbled glyphs. The fix is the one this repo
already uses for stills: generate the picture with no text in it, then composite real
type on top — after running the logical string through arabic-reshaper (joining) and
python-bidi (visual reordering). Nothing else in the repo does this for *video*; this
module is that capability.

## Why a safe box

Social platforms paint their own UI over the frame — on TikTok the top ~10%, right ~10%
and bottom ~20% of a 9:16 video are covered by the username, engagement buttons and CTA.
Text placed there is invisible. So every cue is laid out inside a safe box (a SafeZone
dict from adspec.py), never inside the raw frame, and point positions are clamped into
the box rather than rejected.

## Lazy heavy imports

Mostly stdlib — Pillow is imported inside the functions that draw and ffmpeg is only
shelled out to in burn(), so cue parsing, wrapping and SRT export can be imported and
tested under plain `python3` with no deps and no network.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Font search paths (macOS + Linux). IBM Plex Sans Arabic is a better display face for
# ad captions than Noto, so it is searched first.
ARABIC_FONT_PATHS = [
    os.path.expanduser("~/Library/Fonts/IBMPlexSansArabic-Bold.otf"),
    os.path.expanduser("~/Library/Fonts/IBMPlexSansArabic-SemiBold.otf"),
    # User-installed Noto Sans Arabic (variable weight)
    os.path.expanduser("~/Library/Fonts/NotoSansArabic[wdth,wght].ttf"),
    os.path.expanduser("~/Library/Fonts/NotoSansArabic-Bold.ttf"),
    # macOS system
    "/System/Library/Fonts/GeezaPro.ttc",
    "/System/Library/Fonts/Supplemental/Muna.ttc",
    "/System/Library/Fonts/Supplemental/Damascus.ttc",
    # Linux
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
    "/usr/share/fonts/noto/NotoSansArabic-Bold.ttf",
]

LATIN_FONT_PATHS = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]

# U+0600–U+06FF is the base Arabic block; the widened range also catches Arabic
# Supplement (U+0750–U+077F) and the Presentation Forms blocks (U+FB50–U+FDFF,
# U+FE70–U+FEFF), because text that was ALREADY reshaped elsewhere arrives as
# presentation forms and must still be treated as Arabic.
_ARABIC_RE = re.compile("[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")

CUE_ROLES = ("hook", "caption", "cta", "disclosure", "lower_third")

# Where a cue sits when it doesn't say. A point is in FRAME coordinates (see
# CONTRACT.md); render clamps it into the safe box.
#
# The disclosure sits at the TOP, not the bottom. It was at the bottom, and because it
# runs for the whole film it collided with every bottom-anchored caption — both clamp to
# the same edge of the safe box, so "It was the light." rendered straight through
# "AI-generated". Nothing caught it: overlaps() compares roles, and these are different
# roles that happen to want the same pixels. Top is also where Meta and TikTok put their
# own AI labels, so it reads as a label rather than as a caption.
DEFAULT_POSITION = {
    "hook": "center",
    "caption": "bottom",
    "cta": "center",
    "disclosure": [0.5, 0.04],
    "lower_third": "bottom",
}

# Which vertical band a position resolves to, for collision warnings. A point is bucketed
# by where it falls in the frame.
def _band(position) -> str:
    if isinstance(position, str):
        return position
    return "top" if position[1] < 0.34 else ("center" if position[1] < 0.67 else "bottom")


class CaptionError(Exception):
    """Raised when a cue list or caption render is malformed. Message names the cue index or file."""


# Named looks. Sizes are fractions of the frame HEIGHT so one cue list scales from
# 1080p to 4K. A cue's own keys override these, key by key.
CAPTION_STYLES: dict[str, dict] = {
    # The default: white, heavy black stroke, no plate — survives any footage.
    "bold": {
        "font_size_frac": 0.055,
        "color": "#FFFFFF",
        "stroke_color": "#000000",
        "stroke_width_frac": 0.004,
        "bg_color": None,
        "bg_padding_frac": 0.012,
        "line_spacing": 1.18,
        "align": "center",
        "max_width_frac": 1.0,
        "uppercase": False,
    },
    # White on a semi-opaque black plate, for dense text over busy footage.
    "plate": {
        "font_size_frac": 0.045,
        "color": "#FFFFFF",
        "stroke_color": "#000000",
        "stroke_width_frac": 0.0,
        "bg_color": "#000000CC",
        "bg_padding_frac": 0.014,
        "line_spacing": 1.18,
        "align": "center",
        "max_width_frac": 0.92,
        "uppercase": False,
    },
    # Smaller and quieter — the AI-disclosure mark.
    "subtle": {
        "font_size_frac": 0.028,
        "color": "#FFFFFF",
        "stroke_color": "#000000",
        "stroke_width_frac": 0.002,
        "bg_color": None,
        "bg_padding_frac": 0.008,
        "line_spacing": 1.15,
        "align": "center",
        "max_width_frac": 1.0,
        "uppercase": False,
    },
    # End-card shout: large, plated, uppercase (Latin only — see _maybe_upper).
    "cta": {
        "font_size_frac": 0.075,
        "color": "#FFFFFF",
        "stroke_color": "#000000",
        "stroke_width_frac": 0.003,
        "bg_color": "#000000CC",
        "bg_padding_frac": 0.018,
        "line_spacing": 1.15,
        "align": "center",
        "max_width_frac": 0.92,
        "uppercase": True,
    },
}


# ─── FONTS & ARABIC ──────────────────────────────────────────

def find_font(paths: list[str]) -> str | None:
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def has_arabic(text: str) -> bool:
    return bool(_ARABIC_RE.search(text))


def reshape_arabic(text: str) -> str:
    """Reshape Arabic text for correct rendering: connected letters + RTL."""
    import arabic_reshaper
    from bidi.algorithm import get_display
    reshaped = arabic_reshaper.reshape(text)
    return get_display(reshaped)


def font_for(text: str) -> str | None:
    """Pick the Arabic or Latin font list by content — a mixed string gets the Arabic
    font, because its Latin glyphs are adequate and the Arabic ones are not optional."""
    return find_font(ARABIC_FONT_PATHS if has_arabic(text) else LATIN_FONT_PATHS)


def shape(text: str) -> str:
    """Reshape iff the text contains Arabic, else return it unchanged."""
    return reshape_arabic(text) if has_arabic(text) else text


def _maybe_upper(text: str, style: dict) -> str:
    # str.upper() is a no-op on Arabic letters, but on a mixed string it would shout the
    # Latin half while the Arabic half stays lowercase — lopsided. Never uppercase
    # Arabic text.
    if style.get("uppercase") and not has_arabic(text):
        return text.upper()
    return text


# ─── PARSING ─────────────────────────────────────────────────

def _check_position(position, where: str):
    if isinstance(position, str):
        if position in ("top", "center", "bottom"):
            return position
        raise CaptionError(
            f'{where}.position {position!r} must be "top", "center", "bottom" or '
            "[x, y] fractions in 0..1"
        )
    if isinstance(position, (list, tuple)) and len(position) == 2:
        x, y = position
        if (isinstance(x, bool) or isinstance(y, bool)
                or not isinstance(x, (int, float)) or not isinstance(y, (int, float))):
            raise CaptionError(
                f"{where}.position {position!r} must be two numbers in 0..1"
            )
        x, y = float(x), float(y)
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise CaptionError(
                f"{where}.position {position!r} is outside the frame — fractions must "
                "be in 0..1"
            )
        return [x, y]
    raise CaptionError(
        f'{where}.position must be "top", "center", "bottom" or [x, y] fractions, got '
        f"{position!r}"
    )


def parse_cues(cues: list[dict]) -> list[dict]:
    """Validate and normalise a Cue list. Returns a new list, sorted by start then by
    role order; never mutates the input."""
    if not isinstance(cues, list):
        raise CaptionError(
            f"cues must be a list, got {type(cues).__name__}\n"
            "  pass the 'cues' array from the AdPlan, e.g. plan[\"cues\"]"
        )

    parsed = []
    for index, cue in enumerate(cues):
        where = f"cues[{index}]"
        if not isinstance(cue, dict):
            raise CaptionError(f"{where} must be an object, got {type(cue).__name__}")

        for key in ("start", "end", "text"):
            if key not in cue:
                raise CaptionError(
                    f"{where}.{key} is required\n"
                    "  a cue needs at least start, end and text — see CONTRACT.md"
                )

        start, end = cue["start"], cue["end"]
        for name, value in (("start", start), ("end", end)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise CaptionError(f"{where}.{name} must be a number, got {value!r}")
        start, end = float(start), float(end)
        if start < 0:
            raise CaptionError(f"{where}: start ({start}) must be >= 0")
        if end <= start:
            raise CaptionError(
                f"{where}: end ({end}) must be greater than start ({start})"
            )

        if not isinstance(cue["text"], str):
            raise CaptionError(f"{where}.text must be a string, got {type(cue['text']).__name__}")

        role = cue.get("role", "caption")
        if role not in CUE_ROLES:
            raise CaptionError(
                f"{where}.role {role!r} is not a known role — valid: {', '.join(CUE_ROLES)}"
            )

        style = cue.get("style", "bold")
        if style not in CAPTION_STYLES:
            raise CaptionError(
                f"{where}.style {style!r} is not a known style — valid: "
                f"{', '.join(CAPTION_STYLES)}"
            )

        if "position" in cue:
            position = _check_position(cue["position"], where)
        else:
            default = DEFAULT_POSITION[role]
            position = list(default) if isinstance(default, list) else default

        normalised = dict(cue)
        normalised.update(start=start, end=end, role=role, style=style, position=position)
        parsed.append(normalised)

    role_order = {r: i for i, r in enumerate(CUE_ROLES)}
    # Python's sort is stable, so cues sharing a start AND a role keep their input order.
    return sorted(parsed, key=lambda c: (c["start"], role_order[c["role"]]))


def overlaps(cues: list[dict]) -> list[str]:
    """Soft warnings for cues that would share the screen.

    Two kinds, because checking only the first missed a real collision. Same ROLE
    overlapping in time is a spec mistake. But different roles collide too when they
    resolve to the same vertical band — an always-on disclosure anchored bottom rendered
    straight through every bottom caption, and a role-only check called that fine. What
    matters is the pixels, so compare the band as well as the role.
    """
    warnings = []
    for role in CUE_ROLES:
        group = sorted((c for c in cues if c.get("role") == role), key=lambda c: c["start"])
        for prev, curr in zip(group, group[1:]):
            if curr["start"] < prev["end"]:
                warnings.append(
                    f"cues with role {role!r} overlap: "
                    f"[{prev['start']:.3f}, {prev['end']:.3f}) and "
                    f"[{curr['start']:.3f}, {curr['end']:.3f})"
                )

    ordered = sorted(cues, key=lambda c: (c["start"], c["end"]))
    for index, first in enumerate(ordered):
        for second in ordered[index + 1:]:
            if second["start"] >= first["end"]:
                break
            if first.get("role") == second.get("role"):
                continue  # already reported above
            band_a = _band(first.get("position") or DEFAULT_POSITION[first.get("role", "caption")])
            band_b = _band(second.get("position") or DEFAULT_POSITION[second.get("role", "caption")])
            if band_a == band_b:
                warnings.append(
                    f"{first.get('role', 'caption')!r} and {second.get('role', 'caption')!r} "
                    f"both sit in the {band_a} band and overlap in time "
                    f"([{first['start']:.3f}, {first['end']:.3f}) vs "
                    f"[{second['start']:.3f}, {second['end']:.3f})) — they will draw over "
                    f"each other. Move one with an explicit `position`."
                )
    return warnings


# ─── LAYOUT & DRAWING ────────────────────────────────────────

def wrap_text(text: str, font, max_width: int, draw) -> list[str]:
    """Greedy word wrap to a pixel width, measured with the real font — never a
    character count. Explicit \\n are hard breaks. A single word wider than max_width
    goes on its own line and overflows; silently character-splitting a word is worse
    than a slightly wide line."""
    def measure(s: str) -> int:
        bbox = draw.textbbox((0, 0), s, font=font)
        return bbox[2] - bbox[0]

    lines = []
    for hard in text.split("\n"):
        current = ""
        for word in hard.split(" "):
            candidate = word if not current else current + " " + word
            if current and measure(candidate) > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return lines


def _resolve_style(cue: dict) -> dict:
    name = cue.get("style", "bold")
    if name not in CAPTION_STYLES:
        raise CaptionError(
            f"cue style {name!r} is not a known style — valid: {', '.join(CAPTION_STYLES)}"
        )
    style = dict(CAPTION_STYLES[name])
    for key in style:
        if key in cue:
            style[key] = cue[key]
    return style


def _load_font(text: str, font_size: int):
    from PIL import ImageFont

    font_path = font_for(text)
    if font_path:
        try:
            return ImageFont.truetype(font_path, font_size)
        except Exception:
            pass  # unreadable font file — fall through to the default
    try:
        return ImageFont.load_default(font_size)
    except TypeError:  # very old Pillow: load_default() takes no size
        return ImageFont.load_default()


def _draw_cue(img, cue: dict, width: int, height: int, safe: dict | None,
              y_shift: float = 0.0) -> float:
    """Draw one cue onto an existing PIL image; returns the block height in pixels."""
    from PIL import ImageDraw

    style = _resolve_style(cue)
    text = _maybe_upper(cue["text"], style)

    # Absolute sizes scale with frame HEIGHT, so one cue list works at 1080p and 4K.
    font_size = max(12, round(style["font_size_frac"] * height))
    stroke_width = max(0, round(style["stroke_width_frac"] * height))
    padding = max(0, round(style["bg_padding_frac"] * height))
    line_spacing = style["line_spacing"]

    box = tuple(safe["box"]) if safe else (0, 0, width, height)
    box_w, box_h = box[2] - box[0], box[3] - box[1]
    max_width = round(box_w * style["max_width_frac"])

    font = _load_font(text, font_size)
    draw = ImageDraw.Draw(img)

    logical_lines = wrap_text(text, font, max_width, draw)
    # Wrap BEFORE reshaping, never after. Reshaping and the bidi algorithm operate on a
    # complete visual line; reshape first and split on spaces afterwards and you cut
    # ligatures and reverse the reading order of the fragments. So: wrap the logical
    # string, then shape each resulting line independently.
    lines = [shape(line) for line in logical_lines]

    line_h = font_size * line_spacing
    block_h = len(lines) * line_h

    # The text column is max_width wide, centred in the safe box; align positions lines
    # within that column.
    col_x = box[0] + (box_w - max_width) / 2
    position = cue.get("position") or DEFAULT_POSITION[cue.get("role", "caption")]
    if isinstance(position, str):
        if position == "top":
            block_y = float(box[1])
        elif position == "center":
            block_y = box[1] + (box_h - block_h) / 2
        else:  # bottom — the block ENDS at the box's bottom edge
            block_y = box[3] - block_h
    else:
        # A [x_frac, y_frac] position is a point in FRAME coordinates (not box
        # coordinates), block centred on it — then clamped so the block stays inside
        # the box. Clamping rather than raising: a disclosure at y=0.965 must still
        # land somewhere legible on a platform whose bottom inset is 20%.
        cx, cy = position[0] * width, position[1] * height
        block_y = cy - block_h / 2
        col_x = cx - max_width / 2
        block_y = min(max(block_y, box[1]), max(box[1], box[3] - block_h))
        col_x = min(max(col_x, box[0]), max(box[0], box[2] - max_width))
    block_y += y_shift

    align = style["align"]
    placements = []  # (x, y, anchor, line, line_bbox)
    for i, line in enumerate(lines):
        if has_arabic(line) and align == "left":
            # For RTL text the natural edge is the RIGHT one — a Latin "left" aligned
            # Arabic line hangs ragged on the wrong side. Center stays center.
            line_align = "right"
        else:
            line_align = align
        y = block_y + i * line_h
        if line_align == "left":
            x, anchor = col_x, "la"
        elif line_align == "right":
            x, anchor = col_x + max_width, "ra"
        else:
            x, anchor = col_x + max_width / 2, "ma"
        bbox = draw.textbbox((x, y), line, font=font, anchor=anchor,
                             stroke_width=stroke_width)
        placements.append((x, y, anchor, line, bbox))

    # `block_h` is NOMINAL — font_size * line_spacing per line. Real ink can exceed it,
    # and for Arabic it reliably does: descenders on ي/ن/ج drop well past the line box
    # that a Latin face fits inside. Measured on IBM Plex Sans Arabic at 105px, a single
    # bottom-anchored line overshot the safe box by 28px while the same string over two
    # lines fitted, because the second line's spacing absorbed the descender. Placing on
    # the nominal height therefore pushes text under the platform's UI exactly in the
    # case that looks safest. So: measure what was actually laid out, and correct.
    # The plate is drawn with rounded_rectangle, which paints its bottom/right coordinate
    # INCLUSIVELY — so a plate whose bottom sits exactly on the box edge colours the edge
    # pixel and lands one row outside. Count that pixel here rather than shrinking the
    # plate, or every plated caption sits 1px under the platform's UI.
    plate = padding if style["bg_color"] else 0
    inclusive = 1 if style["bg_color"] else 0
    ink_top = min(p[4][1] for p in placements) - plate
    ink_bottom = max(p[4][3] for p in placements) + plate + inclusive

    correction = 0.0
    if ink_bottom > box[3]:
        correction = box[3] - ink_bottom
    if ink_top + correction < box[1]:
        # Pulling it back down would push the bottom out again; a block genuinely taller
        # than the safe box cannot fit either way. Pin to the top so the opening words
        # survive — losing the tail of a caption beats losing its first line.
        correction = box[1] - ink_top

    if correction:
        placements = [(x, y + correction, anchor, line,
                       (bb[0], bb[1] + correction, bb[2], bb[3] + correction))
                      for x, y, anchor, line, bb in placements]

    if style["bg_color"]:
        # ONE rounded plate behind the whole block — not one per line, or the ragged
        # edges of different line widths look broken.
        left = min(p[4][0] for p in placements) - padding
        top = min(p[4][1] for p in placements) - padding
        right = max(p[4][2] for p in placements) + padding
        bottom = max(p[4][3] for p in placements) + padding
        draw.rounded_rectangle([left, top, right, bottom], radius=max(1, padding),
                               fill=style["bg_color"])

    for x, y, anchor, line, _ in placements:
        draw.text((x, y), line, font=font, fill=style["color"], anchor=anchor,
                  stroke_width=stroke_width, stroke_fill=style["stroke_color"])

    return block_h


def render_cue_png(cue: dict, width: int, height: int, safe: dict | None,
                   out_path: Path) -> Path:
    """Render one cue as a full-frame RGBA PNG with a transparent background, text
    placed inside the safe box."""
    from PIL import Image

    out_path = Path(out_path)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    _draw_cue(img, cue, width, height, safe)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, optimize=True)
    return out_path


def render_cues(cues: list[dict], width: int, height: int, safe: dict | None,
                out_dir: Path) -> list[dict]:
    """Render every parsed cue to out_dir/cue-<index>-<role>.png."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered = []
    for i, cue in enumerate(cues):
        png = render_cue_png(cue, width, height, safe,
                             out_dir / f"cue-{i:02d}-{cue.get('role', 'caption')}.png")
        rendered.append({"cue": cue, "png": png, "index": i})
    return rendered


# ─── BURNING ─────────────────────────────────────────────────

def probe_size(path: Path) -> tuple[int, int] | None:
    """(width, height) of a clip's video stream, or None if ffprobe can't say."""
    if shutil.which("ffprobe") is None:
        return None
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
        capture_output=True, text=True,
    )
    try:
        width, height = result.stdout.strip().splitlines()[0].split("x")
        return int(width), int(height)
    except (ValueError, IndexError):
        return None


def burn(video: Path, cues: list[dict], output: Path, *, width=None, height=None,
         safe=None, work_dir=None) -> Path:
    """Burn the cues into the video with ONE ffmpeg invocation — one chained overlay
    filter graph, so the video is re-encoded exactly once no matter how many cues."""
    video, output = Path(video), Path(output)

    if not cues:
        # Nothing to burn: copy the file and return. Re-encoding for nothing is a
        # quality loss for no reason — and needs no ffmpeg at all.
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(video, output)
        return output

    if shutil.which("ffmpeg") is None:
        raise CaptionError(
            "ffmpeg not found on PATH — needed to burn captions. "
            "Install: brew install ffmpeg"
        )

    if width is None or height is None:
        probed = probe_size(video)
        if probed is None:
            raise CaptionError(
                f"could not probe the size of {video}\n"
                "  pass width= and height= explicitly"
            )
        width, height = probed

    work_dir = Path(work_dir) if work_dir is not None else output.parent / "captions"
    rendered = render_cues(cues, width, height, safe, work_dir)

    inputs = []
    for entry in rendered:
        # No -loop: a single-frame PNG input still covers every timestamp because
        # overlay's default eof_action=repeat repeats its last frame — while a looped
        # input is infinite and the encode never terminates.
        inputs += ["-i", str(entry["png"])]

    chain = []
    prev = "[0:v]"
    for i, entry in enumerate(rendered, 1):
        label = "[vout]" if i == len(rendered) else f"[v{i}]"
        start, end = entry["cue"]["start"], entry["cue"]["end"]
        # 3 decimal places so floats never serialise as 1e-05.
        chain.append(
            f"{prev}[{i}:v]overlay=0:0:enable='between(t,{start:.3f},{end:.3f})'{label}"
        )
        prev = label

    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-i", str(video), *inputs,
        "-filter_complex", ";".join(chain),
        "-map", "[vout]",
        # The ? matters: a silent clip has no audio stream and an unqualified
        # `-map 0:a` fails the whole render.
        "-map", "0:a?", "-c:a", "copy",
        # yuv420p is not optional — without it the file will not play on most social
        # platforms.
        "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
        str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # The whole log is unreadable; the tail is where the error is.
        tail = "\n".join(result.stderr.strip().splitlines()[-20:])
        raise CaptionError(
            f"ffmpeg caption burn failed (exit {result.returncode}) for {video}:\n{tail}"
        )
    return output


# ─── EXPORTS & GUIDES ────────────────────────────────────────

def srt_from_cues(cues: list[dict]) -> str:
    """Standard SRT: 1-based index, HH:MM:SS,mmm --> HH:MM:SS,mmm, text, blank line.
    Disclosure cues are skipped — they are burned marks, not subtitles."""

    def stamp(t: float) -> str:
        ms = round(t * 1000)
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    blocks = []
    for cue in cues:
        if cue.get("role") == "disclosure":
            continue
        # Emit the LOGICAL text, never the reshaped form — an SRT consumer (player,
        # platform captioner) does its own shaping, and a pre-reshaped file renders as
        # mojibake everywhere.
        blocks.append(
            f"{len(blocks) + 1}\n{stamp(cue['start'])} --> {stamp(cue['end'])}\n{cue['text']}"
        )
    return "\n\n".join(blocks) + ("\n\n" if blocks else "")


def cta_card(text: str, width: int, height: int, out_path: Path, *, style="cta",
             safe=None, bg="#101010", sub=None) -> Path:
    """A standalone end-card still: solid bg, the text laid out inside the safe box,
    optional smaller sub line beneath. The CLI turns it into a held clip elsewhere."""
    from PIL import Image

    out_path = Path(out_path)
    img = Image.new("RGB", (width, height), bg)
    main = {"text": text, "role": "cta", "style": style, "position": "center"}
    block_h = _draw_cue(img, main, width, height, safe)
    if sub:
        sub_cue = {"text": sub, "role": "caption", "style": "subtle", "position": "center"}
        # Centred a full main-block height lower, so it opens directly beneath.
        _draw_cue(img, sub_cue, width, height, safe, y_shift=block_h)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, optimize=True)
    return out_path


def safe_guide(width: int, height: int, safe: dict, out_path: Path) -> Path:
    """A translucent guide overlay: unsafe insets filled with 40%-opacity red, the safe
    box left clear with a 2px outline, inset percentages labelled. Lets someone check a
    panel BEFORE paying for video."""
    from PIL import Image, ImageDraw, ImageFont

    out_path = Path(out_path)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    box = tuple(int(v) for v in safe["box"])

    safe_w, safe_h = safe.get("width", width), safe.get("height", height)
    regions = [
        ("top", (0, 0, width, box[1]), safe.get("top", 0) / safe_h),
        ("bottom", (0, box[3], width, height), safe.get("bottom", 0) / safe_h),
        ("left", (0, box[1], box[0], box[3]), safe.get("left", 0) / safe_w),
        ("right", (box[2], box[1], width, box[3]), safe.get("right", 0) / safe_w),
    ]
    try:
        font = ImageFont.load_default(max(14, round(0.02 * height)))
    except TypeError:
        font = ImageFont.load_default()

    for name, rect, frac in regions:
        if rect[2] <= rect[0] or rect[3] <= rect[1]:
            continue
        draw.rectangle(rect, fill=(255, 0, 0, 102))  # 40% opacity red
        if frac > 0:
            label = f"{name} {round(frac * 100)}%"
            tb = draw.textbbox((0, 0), label, font=font)
            lw, lh = tb[2] - tb[0], tb[3] - tb[1]
            rw, rh = rect[2] - rect[0], rect[3] - rect[1]
            if lw <= rw and lh <= rh:
                draw.text((rect[0] + (rw - lw) / 2, rect[1] + (rh - lh) / 2 - tb[1]),
                          label, font=font, fill=(255, 255, 255, 230))

    draw.rectangle(box, outline=(255, 0, 0, 255), width=2)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, optimize=True)
    return out_path
