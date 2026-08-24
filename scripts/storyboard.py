"""Storyboard-driven multi-shot video: spec validation, prompt compilation, contact-sheet
slicing, provenance manifest, and final assembly.

Mostly stdlib — Pillow is imported lazily inside slice_contact_sheet() and ffmpeg is only
shelled out to in assemble(), so spec validation and prompt compilation can be imported and
tested under plain `python3` with no deps and no network.

## Why a contact sheet

Nine panels drawn in ONE diffusion pass share one palette, one lighting setup and one
rendering of each character, because they were never separate generations. That is a far
stronger consistency lever than prompting nine images to match — and it costs one image
call instead of nine.

## Why bridging

In `bridge` mode (the default) clip *i* is generated with panel *i* as its first frame and
panel *i+1* as its last frame, so every cut point is pinned to an image you already
approved. Drift cannot accumulate across the film: each clip is clamped at both ends.
N panels therefore produce N-1 clips, and the last shot contributes its panel as the
closing frame only.

`anchor` mode pins only the opening frame of each clip (N panels -> N clips) — use it when
the shot should end somewhere you can't draw in advance.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

# Camera moves the model actually understands as single, unambiguous instructions.
# Stacking two of these in one clip is the most common cause of mush.
CAMERA_VOCABULARY = (
    "slow push in", "fast push in", "controlled zoom in", "controlled zoom out",
    "low tracking shot", "high tracking shot", "slow pan left", "slow pan right",
    "fast pan left", "fast pan right", "truck left", "truck right",
    "slow arc shot", "crane up", "crane down", "locked static shot", "handheld follow",
)

NO_TEXT_GUARD = (
    "Do not render on-screen text, captions, subtitles, logos, watermarks, garbled "
    "characters or misspellings anywhere in the frame."
)

CHAIN_MODES = ("bridge", "anchor")


class StorySpecError(Exception):
    """Raised when a story spec is malformed. Message names the offending JSON path."""


# ─── SPEC ────────────────────────────────────────────────────

def load_spec(path: Path) -> dict:
    try:
        spec = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise StorySpecError(f"{path}: invalid JSON: {e}")
    if not isinstance(spec, dict):
        raise StorySpecError(f"{path}: top level must be a JSON object")
    validate_spec(spec)
    return spec


def validate_spec(spec: dict) -> list[str]:
    """Hard-fail on anything that would break the run; return soft warnings as strings.

    Checked in full up front so a nine-shot spec doesn't fail on shot 7 after paying for
    six clips.
    """
    warnings: list[str] = []

    if spec.get("version") != 1:
        raise StorySpecError('spec.version must be 1')

    known_top = {
        "version", "title", "model", "provider", "aspect", "resolution", "duration",
        "style", "cast", "board", "shots", "chain", "allow_text",
    }
    unknown = sorted(set(spec) - known_top)
    if unknown:
        raise StorySpecError(f"unknown top-level key(s): {', '.join(unknown)}")

    chain = spec.get("chain", "bridge")
    if chain not in CHAIN_MODES:
        raise StorySpecError(f"spec.chain must be one of {', '.join(CHAIN_MODES)}")

    shots = spec.get("shots")
    if not isinstance(shots, list) or not shots:
        raise StorySpecError("spec.shots must be a non-empty array")

    minimum = 2 if chain == "bridge" else 1
    if len(shots) < minimum:
        raise StorySpecError(
            f"chain {chain!r} needs at least {minimum} shots (got {len(shots)}) — "
            "bridge mode consumes shots in pairs"
        )

    seen_ids = set()
    for index, shot in enumerate(shots):
        where = f"shots[{index}]"
        if not isinstance(shot, dict):
            raise StorySpecError(f"{where} must be an object")

        known_shot = {"id", "panel", "action", "camera", "sound", "duration", "ending",
                      "allow_text"}
        unknown_shot = sorted(set(shot) - known_shot)
        if unknown_shot:
            raise StorySpecError(f"{where}: unknown key(s): {', '.join(unknown_shot)}")

        shot_id = shot.get("id")
        if not shot_id or not isinstance(shot_id, str):
            raise StorySpecError(f"{where}.id is required and must be a string")
        if shot_id in seen_ids:
            raise StorySpecError(f"{where}.id {shot_id!r} is duplicated")
        seen_ids.add(shot_id)

        if not shot.get("panel"):
            raise StorySpecError(
                f"{where}.panel is required — it's what gets drawn on the contact sheet"
            )

        is_closing = chain == "bridge" and index == len(shots) - 1
        if not is_closing and not shot.get("action"):
            raise StorySpecError(f"{where}.action is required (what changes over time)")

        camera = shot.get("camera")
        if camera and camera not in CAMERA_VOCABULARY:
            warnings.append(
                f"{where}.camera {camera!r} is outside the known vocabulary — the model "
                f"may ignore or misread it. Known moves: {', '.join(CAMERA_VOCABULARY[:6])}…"
            )

        sound = shot.get("sound")
        if sound is not None:
            if not isinstance(sound, dict):
                raise StorySpecError(f"{where}.sound must be an object")
            unknown_sound = sorted(set(sound) - {"ambience", "dialogue", "music"})
            if unknown_sound:
                raise StorySpecError(
                    f"{where}.sound: unknown key(s): {', '.join(unknown_sound)}"
                )
            for line_index, line in enumerate(sound.get("dialogue") or []):
                if not isinstance(line, dict) or "line" not in line:
                    raise StorySpecError(
                        f"{where}.sound.dialogue[{line_index}] needs at least a 'line'"
                    )

    if chain == "bridge" and shots[-1].get("action"):
        warnings.append(
            f"shots[{len(shots) - 1}] ({shots[-1]['id']}) is the closing frame in bridge "
            "mode — its action/camera are never generated, only its panel is drawn."
        )

    board = spec.get("board") or {}
    if not isinstance(board, dict):
        raise StorySpecError("spec.board must be an object")
    unknown_board = sorted(set(board) - {"cols", "rows", "image_model", "size", "inset", "autotrim"})
    if unknown_board:
        raise StorySpecError(f"spec.board: unknown key(s): {', '.join(unknown_board)}")

    cols, rows = grid_for(spec)
    if cols * rows < len(shots):
        raise StorySpecError(
            f"spec.board grid {cols}x{rows} holds {cols * rows} panels but there are "
            f"{len(shots)} shots — widen the grid or cut shots"
        )
    if cols * rows > len(shots):
        warnings.append(
            f"board grid {cols}x{rows} has {cols * rows - len(shots)} more cells than "
            "shots; the trailing cells will be generated and discarded"
        )

    cast = spec.get("cast") or {}
    if not isinstance(cast, dict):
        raise StorySpecError("spec.cast must be an object of name -> image path")

    return warnings


def grid_for(spec: dict) -> tuple[int, int]:
    """Explicit cols/rows, else the squarest grid that fits every shot."""
    board = spec.get("board") or {}
    cols, rows = board.get("cols"), board.get("rows")
    if cols and rows:
        return int(cols), int(rows)

    count = len(spec.get("shots") or [])
    if cols:
        cols = int(cols)
        return cols, -(-count // cols)
    if rows:
        rows = int(rows)
        return -(-count // rows), rows

    cols = 1
    while cols * cols < count:
        cols += 1
    return cols, -(-count // cols)


def clip_filename(entry: dict) -> str:
    """Name a clip from its position in the FULL plan, never from its position in a
    filtered run. `--only turn` must still write 02-turn.mp4, or assemble — which builds
    its expected names from the whole plan — looks for a file that isn't there."""
    return f"{entry['index'] + 1:02d}-{entry['id']}.mp4"


def clip_plan(spec: dict) -> list[dict]:
    """Expand the spec into the concrete clips to generate.

    Each entry: {index, id, shot, next_shot, first_panel, last_panel, duration}.
    Panel numbers are 1-based to match the filenames on disk.
    """
    shots = spec["shots"]
    chain = spec.get("chain", "bridge")
    default_duration = spec.get("duration", 6)

    plan = []
    last = len(shots) - 1 if chain == "bridge" else len(shots)
    for index in range(last):
        shot = shots[index]
        next_shot = shots[index + 1] if chain == "bridge" else None
        plan.append({
            "index": index,
            "id": shot["id"],
            "shot": shot,
            "next_shot": next_shot,
            "first_panel": index + 1,
            "last_panel": index + 2 if chain == "bridge" else None,
            "duration": int(shot.get("duration", default_duration)),
        })
    return plan


# ─── PROMPT COMPILATION ──────────────────────────────────────

def compile_board_prompt(spec: dict) -> str:
    """One image prompt that draws every panel in a single pass."""
    cols, rows = grid_for(spec)
    shots = spec["shots"]
    style = (spec.get("style") or "").strip()

    # Deliberately NOT described as a "storyboard" or "contact sheet". Those words carry a
    # paper metaphor, and the model renders the paper: torn margins, keylines, panel rules.
    # Each cell is sliced out and handed to a video model as a literal first/last frame, so
    # any of that becomes a border baked into every frame of the clip. Describing the same
    # layout as edge-to-edge film stills gets the grid without the stationery.
    lines = [
        f"One single image divided into an exact {cols}x{rows} grid of {cols * rows} "
        f"equal rectangular cells, read left to right, top to bottom.",
        "Each cell is a full-bleed cinematic still that fills its rectangle completely, "
        "edge to edge. The cells butt directly against one another.",
        "Absolutely no gutters, margins, paper, page, mounts, borders, frames, outlines, "
        "keylines, rounded corners, drop shadows or dividing rules anywhere in the image — "
        "the only boundary between two cells is where one image ends and the next begins.",
    ]
    if style:
        lines.append(
            f"Every panel shares one single art style, one colour palette and one lighting "
            f"setup: {style}"
        )
    lines.append(
        "Character identity, wardrobe and set dressing must stay identical across all "
        "cells — the same person must be recognisably the same person in every cell."
    )
    lines.append("Cells in order:")
    for number, shot in enumerate(shots, 1):
        lines.append(f"Cell {number}: {shot['panel']}")

    filler = cols * rows - len(shots)
    if filler:
        lines.append(
            f"Remaining {filler} cell(s): the same environment, empty of characters."
        )

    lines.append(
        "No cell numbers, no captions, no speech bubbles, no borders. " + NO_TEXT_GUARD
    )
    return "\n".join(lines)


def compile_shot_prompt(entry: dict, spec: dict) -> str:
    """Turn one clip-plan entry into a prompt in the six-element order the model expects:
    format -> opening composition -> action -> one camera move -> sound -> ending state."""
    shot = entry["shot"]
    next_shot = entry["next_shot"]
    style = (spec.get("style") or "").strip()
    allow_text = shot.get("allow_text", spec.get("allow_text", False))

    parts = []
    if style:
        parts.append(style)

    if next_shot is not None:
        # Frame-bridge prompts must describe the MOTION between the two stills. Describing
        # them as two separate images is the classic failure — the model cuts instead of
        # moving.
        parts.append(
            "Picture 1 aligns with the opening frame and Picture 2 aligns with the final "
            "frame. Render the single continuous motion that connects them. Preserve the "
            "characters, wardrobe, set dressing and lighting of Picture 1 throughout, and "
            "land exactly on the pose, spacing and composition of Picture 2."
        )

    parts.append(f"Opening composition: {shot['panel'].rstrip('.')}.")

    if shot.get("action"):
        parts.append(f"Action: {shot['action'].rstrip('.')}.")

    camera = shot.get("camera") or "locked static shot"
    parts.append(
        f"Camera: {camera} — one continuous camera move for the whole clip, no additional "
        f"moves, no cuts."
    )

    sound = _compile_sound(shot.get("sound"))
    if sound:
        parts.append(sound)

    ending = shot.get("ending")
    if next_shot is not None:
        parts.append(f"Ending state: {next_shot['panel'].rstrip('.')}.")
    elif ending:
        parts.append(f"Ending state: {ending.rstrip('.')}.")

    if not allow_text:
        parts.append(NO_TEXT_GUARD)

    return " ".join(parts)


def _compile_sound(sound: dict | None) -> str:
    """Three named layers, because 'epic audio' carries no timing information."""
    if not sound:
        return ""

    segments = []
    ambience = sound.get("ambience")
    if ambience:
        segments.append(f"Soundscape: {ambience.rstrip('.')}.")

    for line in sound.get("dialogue") or []:
        speaker = line.get("speaker")
        language = line.get("lang", "English")
        text = line["line"]
        delivery = line.get("delivery")
        who = f"{speaker}" if speaker else "The speaker"
        how = f", {delivery}," if delivery else ""
        segments.append(f"{who}{how} says: <d>[{language}] {text}</d>")

    music = sound.get("music")
    segments.append(f"Non-diegetic music: {music.rstrip('.') if music else 'N/A'}.")

    return " ".join(segments)


# ─── CONTACT SHEET SLICING ───────────────────────────────────

def autotrim_borders(image, max_fraction: float = 0.12, uniformity: float = 6.0):
    """Eat inward from each edge while the edge line is a flat band of one colour.

    Image models draw panel frames — a keyline, a rule, a torn-paper margin — no matter how
    firmly the prompt forbids it, and a drawn frame becomes a black bar baked into every
    frame of the resulting clip. Prompting can't be relied on here, so detect it instead:
    a border row is, by construction, near-uniform in colour, while the first row of real
    artwork is not. Walking in until the variance rises finds the artwork edge whether the
    border is a black rule, a white margin, or both stacked.

    `max_fraction` caps the bite per edge so a genuinely flat panel — an empty sky, a plain
    wall — can never be trimmed to nothing.
    """
    from PIL import ImageStat

    grey = image.convert("L")
    width, height = grey.size
    max_x, max_y = int(width * max_fraction), int(height * max_fraction)

    def flat(box) -> bool:
        stats = ImageStat.Stat(grey.crop(box))
        mean, deviation = stats.mean[0], stats.stddev[0]
        if deviation < uniformity:
            return True
        # A drawn rule is rarely clean: the model's own compression noise pushes a solid
        # black keyline to a stddev of 10-18. Mean is the reliable signal there — near-
        # black or near-white with only modest variation is a border, not artwork.
        return (mean < 35 or mean > 220) and deviation < 25

    left = 0
    while left < max_x and flat((left, 0, left + 1, height)):
        left += 1
    right = width
    while right > width - max_x and flat((right - 1, 0, right, height)):
        right -= 1
    top = 0
    while top < max_y and flat((0, top, width, top + 1)):
        top += 1
    bottom = height
    while bottom > height - max_y and flat((0, bottom - 1, width, bottom)):
        bottom -= 1

    if right - left < width * 0.5 or bottom - top < height * 0.5:
        return image  # something went wrong; better a framed panel than a destroyed one
    return image.crop((left, top, right, bottom))


def slice_contact_sheet(sheet: Path, cols: int, rows: int, out_dir: Path,
                        inset: float = 0.03, count: int | None = None,
                        autotrim: bool = True) -> list[Path]:
    """Cut an evenly-gridded contact sheet into panel-NN.png files.

    Two stages, because two different things go wrong. `inset` trims a fixed fraction off
    each cell to absorb the model never landing the gutter on the exact pixel it was asked
    for — that's what stops a neighbour's edge bleeding in. `autotrim` then removes any
    frame the model drew inside the cell, which is variable and can't be handled by a fixed
    fraction.
    """
    from PIL import Image

    if not 0 <= inset < 0.5:
        raise StorySpecError(f"board.inset must be in [0, 0.5), got {inset}")

    image = Image.open(sheet)
    width, height = image.size
    cell_w, cell_h = width / cols, height / rows
    trim_x, trim_y = cell_w * inset, cell_h * inset

    out_dir.mkdir(parents=True, exist_ok=True)
    limit = count if count is not None else cols * rows
    panels = []

    for position in range(min(limit, cols * rows)):
        row, col = divmod(position, cols)
        box = (
            int(col * cell_w + trim_x),
            int(row * cell_h + trim_y),
            int((col + 1) * cell_w - trim_x),
            int((row + 1) * cell_h - trim_y),
        )
        panel = image.crop(box)
        if autotrim:
            panel = autotrim_borders(panel)

        panel_path = out_dir / f"panel-{position + 1:02d}.png"
        panel.save(panel_path)
        panels.append(panel_path)

    return panels


# ─── PROVENANCE ──────────────────────────────────────────────

def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def merge_manifest(workdir: Path, updates: dict) -> dict:
    """Read-modify-write manifest.json so a partial re-run (one clip, one phase) updates
    just its own entries instead of erasing the rest of the record."""
    manifest_path = workdir / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            print(f"Warning: {manifest_path} was unreadable — rewriting it",
                  file=sys.stderr)

    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(manifest.get(key), dict):
            manifest[key].update(value)
        else:
            manifest[key] = value

    workdir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


# ─── ASSEMBLY ────────────────────────────────────────────────

def assemble(clips: list[Path], output: Path) -> Path:
    """Concatenate clips losslessly with ffmpeg's concat demuxer."""
    if not clips:
        raise StorySpecError("no clips to assemble — run `story shots` first")

    missing = [str(c) for c in clips if not c.is_file()]
    if missing:
        raise StorySpecError(
            "missing clip(s): " + ", ".join(missing) + " — re-run `story shots`"
        )

    if shutil.which("ffmpeg") is None:
        raise StorySpecError(
            "ffmpeg not found on PATH — needed to concatenate clips. "
            "Install: brew install ffmpeg"
        )

    list_file = output.parent / "concat.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    # The concat demuxer takes single-quoted paths with internal quotes escaped.
    list_file.write_text(
        "".join(f"file '{c.resolve().as_posix().replace(chr(39), chr(39) * 3)}'\n"
                for c in clips)
    )

    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c", "copy", str(output)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-6:])
        raise StorySpecError(f"ffmpeg concat failed (exit {result.returncode}):\n{tail}")

    list_file.unlink(missing_ok=True)
    return output
