"""Hook-variant assembly and audio muxing for social ads.

A story is N clips in a line. An ad is the same line with the opening clip swapped
out 15–40 times a week: the body and the CTA stay put, only the hook changes. Rendering
the body once and stitching it behind every hook is the cost function — N variants of a
5-clip ad cost 4+N clips, not 5N. The planning math is pure and has to stay that way, so
a dry run can print the bill before anything is spent.

Voiceover on a music bed at equal level is unintelligible. The voice is loudness-
normalised to the same I=-16:TP=-1.5:LRA=11 target the existing ElevenLabs path already
uses, and the bed is ducked under speech with a sidechain compressor so the words stay
in front. A raw mix would fight the platforms' own normalisation and bury the line.

ffmpeg is only shelled out to inside the functions that need it (assemble, fit, mux,
mix). variant_plan, variant_filename and the error types import and test under bare
python3 with no binaries and no network.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


VOICE_TARGET_LUFS = -16.0     # what social platforms normalise toward
VOICE_TRUE_PEAK = -1.5
VOICE_LRA = 11.0
MUSIC_DUCK_DB = -12.0         # how far the bed drops under speech
MUSIC_BED_DB = -20.0          # the bed's resting level relative to the voice


class MuxError(Exception):
    """Raised when mux planning or ffmpeg assembly fails. Message names the file or index."""


# ─── PLANNING (pure, no I/O) ─────────────────────────────────

def variant_plan(clip_ids: list[str], variants: int, *, hook_index: int = 0) -> dict:
    """Cost the hook-variant render: body clips once, one new hook per variant.

    The clip at hook_index is the one that varies — some formats open on `before`,
    not `hook`. Everything else is reused. variants==1 is a single alternate hook
    and saves nothing; that is still a legal plan.
    """
    if not clip_ids:
        raise MuxError(
            "clip_ids is empty — need the ordered clip ids of the base ad\n"
            "  Pass at least one clip id, e.g. [\"hook\", \"body\", \"cta\"]."
        )
    if variants < 1:
        raise MuxError(
            f"variants={variants} is invalid — need at least 1 hook variant\n"
            "  Pass variants>=1 (1 is a single alternate hook)."
        )
    if hook_index < 0 or hook_index >= len(clip_ids):
        raise MuxError(
            f"hook_index={hook_index} is out of range for {len(clip_ids)} clips "
            f"(valid: 0..{len(clip_ids) - 1})\n"
            "  Pass a hook_index that points at the beat that varies."
        )

    hook_id = clip_ids[hook_index]
    base_clips = [c for i, c in enumerate(clip_ids) if i != hook_index]
    variant_rows = []
    for n in range(1, variants + 1):
        vid = f"{hook_id}-v{n}"
        clips = list(clip_ids)
        clips[hook_index] = vid
        variant_rows.append({"n": n, "id": vid, "clips": clips})

    clips_to_render = len(base_clips) + variants
    clips_if_naive = len(clip_ids) * variants
    return {
        "base_clips": base_clips,
        "hook_id": hook_id,
        "hook_index": hook_index,
        "variants": variant_rows,
        "clips_to_render": clips_to_render,
        "clips_if_naive": clips_if_naive,
        "clips_saved": clips_if_naive - clips_to_render,
    }


def variant_filename(base: str, variant_id: str) -> str:
    """Stable on-disk name the CLI concatenates; do not format this at the call site."""
    return f"{base}-{variant_id}.mp4"


# ─── PROBE ───────────────────────────────────────────────────

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


def duration_of(path: Path) -> float | None:
    """Container duration in seconds, or None if ffprobe is absent or cannot say."""
    if shutil.which("ffprobe") is None:
        return None
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def has_audio(path: Path) -> bool:
    """True when the container has an audio stream. False if ffprobe is absent."""
    if shutil.which("ffprobe") is None:
        return False
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


# ─── FFMPEG RUNNER ───────────────────────────────────────────

def _require_ffmpeg(what: str) -> None:
    if shutil.which("ffmpeg") is None:
        raise MuxError(
            f"ffmpeg not found on PATH — needed to {what}.\n"
            "  Install: brew install ffmpeg"
        )


def run_ffmpeg(args: list[str], what: str) -> None:
    """One shared runner so every ffmpeg failure names `what` and tails stderr."""
    _require_ffmpeg(what)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-20:])
        raise MuxError(f"ffmpeg {what} failed (exit {result.returncode}):\n{tail}")


def sidecar(path: Path, data: dict) -> Path:
    """Pair `path` with path.suffix + '.meta.json', matching the rest of the skill."""
    meta = {**data, "tool": "admux", "output": str(path)}
    dest = path.with_suffix(path.suffix + ".meta.json")
    dest.write_text(json.dumps(meta, indent=2) + "\n")
    return dest


def _loudnorm() -> str:
    """Reproduce the existing voice path's filter: loudnorm=I=-16:TP=-1.5:LRA=11."""
    return (
        f"loudnorm=I={VOICE_TARGET_LUFS:g}"
        f":TP={VOICE_TRUE_PEAK:g}"
        f":LRA={VOICE_LRA:g}"
    )


def _require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise MuxError(
            f"{label} not found: {path}\n"
            "  Pass a path to an existing file."
        )


# ─── ASSEMBLE ────────────────────────────────────────────────

def assemble_variant(clips: list[Path], output: Path) -> Path:
    """Concatenate clips with ffmpeg's concat demuxer.

    Stream-copies when every clip shares one frame size — lossless and instant. Falls back
    to a re-encode when they don't, because `-c copy` cannot reconcile mismatched
    dimensions: it writes the first clip's size into the container header and lets the
    later segments disagree with it, producing a file that decodes without error but plays
    wrong. H3 does this in practice — regenerating a 1376x768 draft returned 2592x1440 for
    two clips and 2560x1440 for a third.

    The concat list is named per output stem (`concat-<stem>.txt`) so two variants
    assembling into the same directory cannot overwrite each other's list mid-run.
    """
    if not clips:
        raise MuxError("no clips to assemble — run `ad shots` first")

    missing = [str(c) for c in clips if not c.is_file()]
    if missing:
        raise MuxError(
            "missing clip(s): " + ", ".join(missing) + " — re-run `ad shots`"
        )

    _require_ffmpeg("concatenate clips")

    output.parent.mkdir(parents=True, exist_ok=True)
    list_file = output.parent / f"concat-{output.stem}.txt"
    # The concat demuxer takes single-quoted paths with internal quotes escaped.
    list_file.write_text(
        "".join(f"file '{c.resolve().as_posix().replace(chr(39), chr(39) * 3)}'\n"
                for c in clips)
    )

    sizes = {c: probe_size(c) for c in clips}
    known = {s for s in sizes.values() if s}
    uniform = len(known) <= 1

    if uniform:
        codec_args = ["-c", "copy"]
        filter_graph = None
        encode = "copy"
    else:
        # Normalise to the largest frame, padding rather than stretching so nothing is
        # distorted by a few pixels of aspect drift.
        target = max(known, key=lambda wh: wh[0] * wh[1])
        odd = {str(c.name): f"{s[0]}x{s[1]}" for c, s in sizes.items() if s and s != target}
        print(
            f"Warning: clips differ in size — {', '.join(f'{k} is {v}' for k, v in odd.items())}"
            f". Re-encoding everything to {target[0]}x{target[1]} (stream copy would "
            f"produce a file that plays wrong).",
            file=sys.stderr,
        )
        width, height = target
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:-1:-1:color=black,setsar=1"
        )
        codec_args = [
            "-vf", vf,
            "-c:v", "libx264", "-crf", "17", "-preset", "medium", "-c:a", "aac", "-b:a", "192k",
        ]
        filter_graph = vf
        encode = "reencode"

    run_ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", str(list_file), *codec_args, str(output)],
        "concatenate clips",
    )
    # Keep the per-variant list: two variants in one directory must not share
    # concat.txt, and the surviving files are how we prove they didn't.
    sidecar(output, {
        "inputs": [str(c) for c in clips],
        "filter_graph": filter_graph,
        "encode": encode,
        "concat_list": str(list_file),
        "uniform": uniform,
    })
    return output


# ─── AUDIO ───────────────────────────────────────────────────

def fit_audio(audio: Path, target_seconds: float, output: Path, *, mode: str = "pad") -> Path:
    """Make `audio` exactly `target_seconds` long.

    apad is applied *before* `-t`. Reversing them pads after the cut, which does
    nothing — a 3s line asked to fill a 15s ad would stay 3s and the rest would be
    dead air the filter never reached.
    """
    _require_file(audio, "audio")
    if target_seconds <= 0:
        raise MuxError(
            f"target_seconds={target_seconds} is not positive\n"
            "  Pass a duration in seconds greater than 0."
        )
    if mode not in ("pad", "trim"):
        raise MuxError(
            f"mode={mode!r} is invalid — use 'pad' or 'trim'\n"
            "  pad extends a short file with silence; trim refuses to."
        )

    if mode == "trim":
        dur = duration_of(audio)
        if dur is None:
            raise MuxError(
                f"{audio}: cannot probe duration — needed to verify trim mode\n"
                "  Install ffprobe (brew install ffmpeg) or use mode='pad'."
            )
        if dur < target_seconds:
            raise MuxError(
                f"{audio}: audio is {dur:.2f}s, shorter than target {target_seconds}s\n"
                "  Use mode='pad' to pad with silence, or supply a longer voiceover."
            )
        args = ["-i", str(audio), "-t", str(target_seconds), str(output)]
    else:
        # Always apad before -t, even when the source is already longer: -t then
        # cuts to the target and the pad is a no-op on the discarded tail.
        args = ["-i", str(audio), "-af", "apad", "-t", str(target_seconds), str(output)]

    output.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(args, "fit audio")
    return output


def mux_voice(
    video: Path,
    voice: Path,
    output: Path,
    *,
    normalize: bool = True,
    keep_original: bool = False,
    original_db: float = -18.0,
) -> Path:
    """Put a voiceover onto a film. Video is stream-copied — re-encoding it to
    change the soundtrack is pure loss.

    keep_original=False (the default) replaces the clip audio, which is the right
    call for a faceless narrated ad. keep_original=True mixes the original under
    the voice; a silent film has nothing to mix, so that path falls back to
    replace and records the fallback in the sidecar.

    apad on the voice plus -shortest keeps a short line from cutting the film and
    a long line from running past the last frame.
    """
    _require_file(video, "video")
    _require_file(voice, "voice")
    output.parent.mkdir(parents=True, exist_ok=True)

    voice_chain = f"{_loudnorm()},apad" if normalize else "apad"
    fell_back = False
    mix_original = keep_original and has_audio(video)
    if keep_original and not mix_original:
        fell_back = True

    if mix_original:
        graph = [
            f"[0:a]volume={original_db:g}dB[orig]",
            f"[1:a]{voice_chain}[voice]",
            "[orig][voice]amix=inputs=2:duration=longest:dropout_transition=0[mixed]",
        ]
    else:
        graph = [f"[1:a]{voice_chain}[mixed]"]

    filter_graph = ";".join(graph)
    run_ffmpeg(
        [
            "-i", str(video), "-i", str(voice),
            "-filter_complex", filter_graph,
            "-map", "0:v:0", "-map", "[mixed]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(output),
        ],
        "mux voice",
    )
    sidecar(output, {
        "inputs": {"video": str(video), "voice": str(voice)},
        "filter_graph": filter_graph,
        "normalize": normalize,
        "keep_original": keep_original,
        "keep_original_fallback": fell_back,
        "original_db": original_db,
    })
    return output


def mix_tracks(
    video: Path,
    output: Path,
    *,
    voice: Path | None = None,
    music: Path | None = None,
    duck: bool = True,
) -> Path:
    """Film + voice + music bed. Ducking drops the bed under speech so the line
    stays intelligible; without it the compressor never runs and the mix is just
    two levels amixed.

    The filter graph is built as a list of labelled chains joined with `;` and
    stored on the sidecar — an ffmpeg graph you cannot read is one you cannot debug.
    """
    if voice is None and music is None:
        raise MuxError(
            "mix_tracks requires at least one of voice/music\n"
            "  Pass voice= and/or music= as Path arguments."
        )
    _require_file(video, "video")
    if voice is not None:
        _require_file(voice, "voice")
    if music is not None:
        _require_file(music, "music")

    if voice is not None and music is None:
        return mux_voice(video, voice, output, normalize=True)

    output.parent.mkdir(parents=True, exist_ok=True)

    if music is not None and voice is None:
        graph = [f"[1:a]volume={MUSIC_BED_DB:g}dB,apad[mixed]"]
        inputs = ["-i", str(video), "-i", str(music)]
    elif duck:
        # [voice] is consumed twice (sidechain key + mix). asplit is required;
        # reusing a label is a graph error, not a silent no-op.
        graph = [
            f"[1:a]{_loudnorm()},apad,asplit=2[voice][voice_sc]",
            f"[2:a]volume={MUSIC_BED_DB:g}dB[bed]",
            "[bed][voice_sc]sidechaincompress=threshold=0.05:ratio=8:attack=5:release=300[ducked]",
            "[ducked][voice]amix=inputs=2:duration=longest:dropout_transition=0[mixed]",
        ]
        inputs = ["-i", str(video), "-i", str(voice), "-i", str(music)]
    else:
        graph = [
            f"[1:a]{_loudnorm()},apad[voice]",
            f"[2:a]volume={MUSIC_BED_DB:g}dB[bed]",
            "[bed][voice]amix=inputs=2:duration=longest:dropout_transition=0[mixed]",
        ]
        inputs = ["-i", str(video), "-i", str(voice), "-i", str(music)]

    filter_graph = ";".join(graph)
    run_ffmpeg(
        [
            *inputs,
            "-filter_complex", filter_graph,
            "-map", "0:v:0", "-map", "[mixed]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(output),
        ],
        "mix tracks",
    )
    sidecar(output, {
        "inputs": {
            "video": str(video),
            "voice": str(voice) if voice is not None else None,
            "music": str(music) if music is not None else None,
        },
        "filter_graph": filter_graph,
        "duck": duck,
        "music_bed_db": MUSIC_BED_DB,
        "music_duck_db": MUSIC_DUCK_DB,
    })
    return output
