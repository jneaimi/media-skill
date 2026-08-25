"""Keep authored cuts intact when clips become one social-ad timeline.

FFmpeg's xfade overlaps inputs, so treating a transition as an inserted effect makes
every later caption drift.  The catalogue and timing math here stay usable without
FFmpeg so an ad can be planned on a bare machine, while rendering probes media early
enough to turn xfade's cryptic failures into fixes a creator can act on.
"""

from __future__ import annotations

import difflib
import functools
import re
import shutil
import subprocess
import warnings
from dataclasses import dataclass
from pathlib import Path


VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "xfade-easing"

NATIVE: tuple[str, ...] = (
    "custom", "fade", "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown", "circlecrop",
    "rectcrop", "distance", "fadeblack", "fadewhite", "radial", "smoothleft",
    "smoothright", "smoothup", "smoothdown", "circleopen", "circleclose",
    "vertopen", "vertclose", "horzopen", "horzclose", "dissolve", "pixelize",
    "diagtl", "diagtr", "diagbl", "diagbr", "hlslice", "hrslice", "vuslice",
    "vdslice", "hblur", "fadegrays", "wipetl", "wipetr", "wipebl", "wipebr",
    "squeezeh", "squeezev", "zoomin", "fadefast", "fadeslow", "hlwind",
    "hrwind", "vuwind", "vdwind", "coverleft", "coverright", "coverup",
    "coverdown", "revealleft", "revealright", "revealup", "revealdown",
)
DEFAULT_DURATION = 0.4
MIN_DURATION = 0.05
FRAME_TOLERANCE = 0.08


class TransitionError(Exception):
    """Raised when a transition name, duration or chain is unbuildable. Names the clip index."""


# ─── CATALOGUE (pure, no ffmpeg) ──────────────────────────────

def _parse_expr_file(path: Path) -> dict[str, str]:
    """Parse a vendored xfade-easing expression file into {NAME: expression}."""
    if not path.is_file():
        raise TransitionError(
            f"vendored transition catalogue missing: {path}\n"
            "  The repo checkout is incomplete; restore vendor/xfade-easing."
        )
    records: dict[str, str] = {}
    pending: str | None = None
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if pending is None:
            if re.fullmatch(r"[A-Z][A-Z0-9_-]*", line) is None:
                raise TransitionError(f"invalid catalogue name at {path}:{lineno}: {line}")
            pending = line
        else:
            if pending in records:
                raise TransitionError(f"duplicate catalogue name {pending} in {path}")
            records[pending] = line
            pending = None
    if pending is not None:
        raise TransitionError(f"catalogue entry {pending} has no expression in {path}")
    return records


@functools.cache
def eased() -> dict[str, str]:
    """The 106 vendored transition expressions, keyed by UPPERCASE name. Cached."""
    records = _parse_expr_file(VENDOR / "eased-transitions-yuv420p-inline.txt")
    # The source file also names native-only transitions with a NATIVE sentinel.  They
    # are catalogue metadata, not expressions, and cannot safely be passed to expr=.
    return {name: expr for name, expr in records.items() if expr != "NATIVE"}


@functools.cache
def easings() -> dict[str, str]:
    """The 43 vendored easing expressions, keyed by UPPERCASE name. Cached."""
    return _parse_expr_file(VENDOR / "xfade-easings-inline.txt")


def probe_native() -> tuple[str, ...] | None:
    """Return this FFmpeg build's xfade transitions, or a safe fallback."""
    if shutil.which("ffmpeg") is None:
        return None
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-h", "filter=xfade"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return NATIVE
        names = re.findall(r"^\s{5}([a-z][a-z0-9_]*)\s+-?\d+\s", result.stdout, re.MULTILINE)
        return tuple(dict.fromkeys(names)) or NATIVE
    except (OSError, subprocess.SubprocessError):
        return NATIVE


def _usable_eased() -> dict[str, str]:
    return eased()


def available(kind: str = "all") -> list[str]:
    """Sorted transition names. kind: 'native' | 'eased' | 'gl' | 'all'."""
    if kind not in {"native", "eased", "gl", "all"}:
        raise TransitionError("kind must be 'native', 'eased', 'gl' or 'all'")
    native = probe_native()
    native_names = list(native if native is not None else NATIVE)
    expression_names = list(_usable_eased())
    if kind == "native":
        return sorted(native_names)
    if kind == "eased":
        return sorted(expression_names)
    if kind == "gl":
        return sorted(name for name in expression_names if name.startswith("GL_"))
    return sorted(native_names + expression_names)


def _normalise(name: str) -> str:
    return name.strip().replace("-", "_").upper()


def _normalise_easing(name: str) -> str:
    return name.strip().replace("_", "-").upper()


def _closest(name: str, choices: list[str]) -> str:
    canonical = {_normalise(choice): choice for choice in choices}
    matches = difflib.get_close_matches(_normalise(name), list(canonical), n=5)
    return ", ".join(canonical[match] for match in matches) if matches else ", ".join(choices[:5])


# ─── RESOLUTION (pure, no ffmpeg) ─────────────────────────

@dataclass(frozen=True)
class Transition:
    name: str
    duration: float
    native: str | None
    expr: str | None
    easing: str | None

    @property
    def needs_single_thread(self) -> bool:
        return self.expr is not None

    def args(self) -> str:
        """Return xfade parameters excluding duration and offset."""
        if self.native is not None:
            return f"transition={self.native}"
        if self.expr is None:
            raise TransitionError(f"transition {self.name!r} has no native or expression form")
        if "'" in self.expr:
            raise TransitionError(f"transition {self.name!r} expression contains an unsafe quote")
        return f"transition=custom:expr='{self.expr}'"


def resolve(name: str, duration: float = DEFAULT_DURATION,
            easing: str | None = None) -> Transition:
    """Resolve a native or vendored transition, preserving any requested easing."""
    if duration <= 0:
        raise TransitionError(f"transition duration {duration:g}s must be greater than zero")
    if duration < MIN_DURATION:
        warnings.warn(
            f"transition duration {duration:g}s is below {MIN_DURATION:g}s; clamped to {MIN_DURATION:g}s",
            RuntimeWarning, stacklevel=2,
        )
        duration = MIN_DURATION

    key = _normalise(name)
    native_map = {_normalise(item): item for item in (probe_native() or NATIVE)}
    if easing is None and key in native_map:
        return Transition(name, duration, native_map[key], None, None)

    transition_expr = eased().get(key)
    if transition_expr is None:
        if easing is not None and key in native_map:
            can_ease = sorted(_usable_eased())
            raise TransitionError(
                f"transition {name!r} cannot be eased because it has no expression form; "
                f"transitions that can be eased: {', '.join(can_ease)}"
            )
        choices = available()
        raise TransitionError(
            f"unknown transition {name!r}; closest matches: {_closest(name, choices)}"
        )
    easing_name = "LINEAR" if easing is None else _normalise_easing(easing)
    easing_expr = easings().get(easing_name)
    if easing_expr is None:
        choices = sorted(easings())
        raise TransitionError(
            f"unknown easing {easing!r}; closest matches: {_closest(str(easing), choices)}"
        )
    expr = f"{easing_expr};{transition_expr}"
    return Transition(name, duration, None, expr, easing_name)


# ─── CHAIN PLANNING ───────────────────────────────

@dataclass
class Clip:
    path: Path
    duration: float
    transition: Transition | None = None


def _chain_errors(clips: list[Clip]) -> list[str]:
    if not clips:
        return ["no clips"]
    errors: list[str] = []
    elapsed = clips[0].duration
    if elapsed <= 0:
        errors.append(f"clip index 0 has invalid duration {elapsed:g}s")
    for index, clip in enumerate(clips[1:], 1):
        if clip.duration <= 0:
            errors.append(f"clip index {index} has invalid duration {clip.duration:g}s")
        transition = clip.transition
        if transition is None:
            errors.append(f"clip index {index} has no transition into it")
            elapsed += clip.duration
            continue
        t = transition.duration
        previous = clips[index - 1].duration
        if t > elapsed:
            errors.append(
                f"clip index {index} transition is {t:g}s, longer than remaining "
                f"accumulated head {elapsed:g}s"
            )
        if t > previous or t > clip.duration:
            errors.append(
                f"clip index {index} transition is {t:g}s, longer than neighbouring "
                f"durations {previous:g}s and {clip.duration:g}s"
            )
        elapsed += clip.duration - t
    return errors


def _require_chain(clips: list[Clip]) -> None:
    errors = _chain_errors(clips)
    if errors:
        raise TransitionError(errors[0])


def total_duration(clips: list[Clip]) -> float:
    """Return sum of clip durations minus the overlapping transitions."""
    _require_chain(clips)
    return sum(clip.duration for clip in clips) - sum(
        clip.transition.duration for clip in clips[1:] if clip.transition is not None
    )


def _probe_video(path: Path) -> tuple[tuple[int, int], float] | None:
    if shutil.which("ffprobe") is None:
        return None
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,avg_frame_rate", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        width, height, rate = result.stdout.strip().splitlines()[0].split(",")
        numerator, denominator = rate.split("/")
        return (int(width), int(height)), float(numerator) / float(denominator)
    except (ValueError, IndexError, ZeroDivisionError):
        return None


def _has_audio(path: Path) -> bool:
    if shutil.which("ffprobe") is None:
        return False
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


def validate(clips: list[Clip]) -> list[str]:
    """Return every error or actionable warning found in a transition chain."""
    problems = _chain_errors(clips)
    if not clips:
        return problems
    if clips[0].transition is not None:
        problems.append("clip index 0 transition is ignored; only cuts into later clips transition")

    probes = [_probe_video(clip.path) for clip in clips]
    known = [(index, probe) for index, probe in enumerate(probes) if probe is not None]
    if known:
        base_index, base = known[0]
        for index, probe in known[1:]:
            if probe[0] != base[0]:
                problems.append(
                    f"clip index {index} size {probe[0][0]}x{probe[0][1]} differs from clip "
                    f"index {base_index} size {base[0][0]}x{base[0][1]} — it will be scaled "
                    "and centre-cropped to match, losing the edges. Normalise the panels "
                    "with storyboard.fit_aspect to keep them."
                )
            if abs(probe[1] - base[1]) > 0.001:
                problems.append(
                    f"clip index {index} frame rate {probe[1]:g} differs from clip "
                    f"index {base_index} frame rate {base[1]:g}; xfade uses the first input's rate"
                )
    return problems


def _audio_enabled(clips: list[Clip], requested: bool) -> bool:
    if not requested:
        return False
    enabled = all(_has_audio(clip.path) for clip in clips)
    if not enabled:
        warnings.warn(
            "audio omitted because at least one clip has no audio stream",
            RuntimeWarning, stacklevel=3,
        )
    return enabled


def _normalise_filters(clips: list[Clip]) -> tuple[list[str], dict[int, str]]:
    """Scale-and-crop every clip to the first one's size, when they disagree.

    xfade does not merely prefer matching dimensions — it refuses:
    "First input link main parameters (size 768x1376) do not match ... (size 768x1216)".
    Concatenation tolerates a mismatch by re-encoding, so a spec that assembled fine with
    hard cuts would fail the moment a transition was added, which is a bad trade for the
    user. H3 really does return four different heights across one five-shot ad, so this is
    the normal case, not the pathological one.

    Cropped, never padded, matching storyboard.fit_aspect: padding puts black bars inside
    the frame and the transition then animates them as part of the picture.

    FRAME RATE IS PART OF THIS, and it is the half that is easy to miss. xfade also
    rejects mismatched timebases — "First input link main timebase (1/12288) do not match
    ... (1/15360)" — which is a different error from the size one and fires even when
    every clip is the same size. It shows up the moment an effect re-encodes one clip at a
    different rate from its source, so the effects stage can introduce it into footage
    that was previously uniform. When anything differs, every input is put through the
    same scale/crop/fps/settb so the whole chain is uniform by construction.
    """
    probed = [_probe_video(clip.path) for clip in clips]
    sizes = [entry[0] if entry else None for entry in probed]
    rates = [entry[1] if entry else None for entry in probed]
    target = next((size for size in sizes if size), None)
    rate = next((r for r in rates if r), None)
    if target is None:
        return [], {}
    sizes_differ = any(size is not None and size != target for size in sizes)
    rates_differ = rate is not None and any(
        r is not None and abs(r - rate) > 0.001 for r in rates)
    if not sizes_differ and not rates_differ:
        return [], {}
    width, height = target
    filters, labels = [], {}
    for index in range(len(clips)):
        label = f"n{index}"
        chain = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                 f"crop={width}:{height},setsar=1")
        if rates_differ:
            chain += f",fps={rate:g},settb=AVTB"
        filters.append(f"[{index}:v]{chain}[{label}]")
        labels[index] = label
    return filters, labels


def chain_filter(clips: list[Clip], *, audio: bool = True,
                 video_label: str = "vout", audio_label: str = "aout",
                 normalise: bool = True) -> str:
    """Build the filter_complex body for the whole transition chain."""
    _require_chain(clips)
    with_audio = _audio_enabled(clips, audio)
    if len(clips) == 1:
        filters = [f"[0:v]null[{video_label}]"]
        if with_audio:
            filters.append(f"[0:a]anull[{audio_label}]")
        return ";".join(filters)

    filters: list[str] = []
    fixups, relabel = _normalise_filters(clips) if normalise else ([], {})
    filters.extend(fixups)
    elapsed = clips[0].duration
    video_in = relabel.get(0, "0:v")
    audio_in = "0:a"
    for index, clip in enumerate(clips[1:], 1):
        transition = clip.transition
        assert transition is not None
        last = index == len(clips) - 1
        next_video = video_label if last else f"v{index - 1}{index}"
        offset = elapsed - transition.duration
        filters.append(
            f"[{video_in}][{relabel.get(index, f'{index}:v')}]xfade={transition.args()}:"
            f"duration={transition.duration:g}:offset={offset:g}[{next_video}]"
        )
        if with_audio:
            next_audio = audio_label if last else f"a{index - 1}{index}"
            filters.append(
                f"[{audio_in}][{index}:a]acrossfade=d={transition.duration:g}[{next_audio}]"
            )
            audio_in = next_audio
        video_in = next_video
        elapsed += clip.duration - transition.duration
    return ";".join(filters)


def chain_args(clips: list[Clip], output: Path, *, audio: bool = True,
               extra: list[str] | None = None) -> list[str]:
    """Return full ffmpeg arguments, excluding the leading executable."""
    _require_chain(clips)
    with_audio = audio and all(_has_audio(clip.path) for clip in clips)
    args: list[str] = []
    for clip in clips:
        args.extend(["-i", str(clip.path)])
    if any(clip.transition and clip.transition.needs_single_thread for clip in clips[1:]):
        args.extend(["-filter_complex_threads", "1"])
    args.extend(["-filter_complex", chain_filter(clips, audio=audio), "-map", "[vout]"])
    if with_audio:
        args.extend(["-map", "[aout]", "-c:a", "aac"])
    args.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p"])
    if extra:
        args.extend(extra)
    args.append(str(output))
    return args


# ─── RENDERING ───────────────────────────────────

def render(clips: list[Clip], output: Path, *, audio: bool = True) -> Path:
    """Render a transition chain, reporting a useful stderr tail on failure."""
    if shutil.which("ffmpeg") is None:
        raise TransitionError(
            "ffmpeg not found on PATH — needed to render transitions.\n"
            "  Install: brew install ffmpeg"
        )
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           *chain_args(clips, output, audio=audio)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-20:])
        raise TransitionError(
            f"ffmpeg transition render failed (exit {result.returncode}):\n{tail}"
        )
    return output


def measure(path: Path) -> float | None:
    """Return container duration in seconds, or None when ffprobe cannot say."""
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
