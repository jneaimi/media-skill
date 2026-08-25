"""Within-clip video effects for social ads.

Every clip the pipeline concatenates is currently played exactly as the video
model returned it. Social ads live on punch-ins, flashes at the cut, a shake on
the claim — and the model cannot be asked for those reliably. This module is the
within-clip layer: pure ffmpeg filter-chain fragments that compose into a single
`-vf` so they can sit next to captions without a second input stream.

A filter that is not in *this* ffmpeg build is a broken effect for every user of
the skill, and the failure is a cryptic parse error at render time. Builders
therefore declare `requires`; `build()` refuses with a name, not a log tail.
Text is never drawn here — this ffmpeg has no drawtext/subtitles/ass, and
captions.py already composites Pillow PNGs.
"""

from __future__ import annotations

import difflib
import functools
import math
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path


class EffectError(Exception):
    """Raised when an effect name, parameter or filter dependency is unusable."""


@dataclass(frozen=True)
class Effect:
    name: str
    filters: str               # the comma-joined filter chain fragment
    requires: tuple[str, ...]  # ffmpeg filter names this fragment needs
    notes: str = ""            # anything the caller should know (e.g. "re-encodes")


# ffmpeg named colours we accept. An unparsed colour silently becomes black —
# the opposite of a flash — which is how a shell fixture once produced 320×240
# black clips from an unquoted 0xRRGGBB. Allowlist plus a hex regex; nothing else.
_NAMED_COLOURS = frozenset({
    "white", "black", "red", "green", "blue", "yellow", "cyan", "magenta",
    "orange", "pink", "purple", "gray", "grey", "brown", "navy", "teal",
    "gold", "silver", "coral", "violet", "indigo", "crimson", "snow",
    "azure", "tomato", "lime", "maroon", "olive", "aqua", "khaki",
    "salmon", "beige", "ivory", "tan", "wheat",
})
_HEX_COLOUR = re.compile(r"(?:0x|#)[0-9A-Fa-f]{6}$")

KEN_BURNS_DIRECTIONS = (
    "in", "out", "left", "right", "up", "down", "in-left", "in-right",
)
WHIP_DIRECTIONS = ("horizontal", "vertical")

# Dummy kwargs so available()/unavailable() can read `.requires` without the
# caller supplying clip geometry. Requires is a function of the builder, not
# of width/height.
_PROBE_KW: dict[str, dict] = {
    "zoom_punch": {"width": 16, "height": 16},
    "shake": {"width": 16, "height": 16},
    "ken_burns": {"direction": "in", "duration": 1.0, "width": 16, "height": 16},
    "speed_ramp": {"segments": [(0.0, 1.0, 1.0)]},
    "freeze_frame": {"at": 0.0},
}

_ENABLE_BETWEEN = re.compile(
    r"enable='between\(t,([0-9.+-eE]+),([0-9.+-eE]+)\)'"
)

# ─── HELPERS ─────────────────────────────────────────────────


def _n(x: float) -> str:
    """Compact number for ffmpeg expressions. Avoids 0.1+0.2 ghosts."""
    return format(round(float(x), 6), "g")


def _between(at: float, duration: float) -> str:
    return f"enable='between(t,{_n(at)},{_n(at + duration)})'"


def _identity_enable(at: float, duration: float) -> str:
    """Timeline-gated identity. zoompan/crop/loop do not implement enable= on
    this ffmpeg (8.1.1: 'Not yet implemented in FFmpeg, patches welcome'), but
    timed effects must still emit `enable='between(t,…)'` so the window is
    inspectable and chainable with filters that do honour it."""
    return f"eq={_between(at, duration)}"


def _timed(at: float, duration: float) -> None:
    if at < 0:
        raise EffectError(
            f"at={at} is invalid — at must be >= 0\n"
            "  Pass the start of the effect in seconds from the beginning of the clip."
        )
    if duration <= 0:
        raise EffectError(
            f"duration={duration} is invalid — duration must be > 0\n"
            "  Pass a positive window in seconds."
        )


def _positive_size(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise EffectError(
            f"width={width}, height={height} is invalid — both must be > 0\n"
            "  Pass the clip's frame size in pixels."
        )


def _zoom_upscale(width: int) -> int:
    """Intermediate width for the zoompan jitter fix.

    zoompan truncates the crop window to whole SOURCE pixels, so a slow move
    stutters. Upscaling first makes one source pixel a fraction of an output
    pixel. Cap at min(8000, width*4): 4× already puts the rounding error below
    one output pixel (enough at 4K), and 8000-wide intermediates on an already-4K
    clip are enormous and slow. Use -2 (not -1) on the height so libx264+yuv420p
    never sees an odd dimension.
    """
    return min(8000, max(int(width) * 4, int(width)))


def _validate_colour(color: str) -> str:
    token = color.strip()
    if token.lower() in _NAMED_COLOURS or _HEX_COLOUR.fullmatch(token):
        return token.lower() if token.lower() in _NAMED_COLOURS else token
    raise EffectError(
        f"color={color!r} is not a named colour or 0xRRGGBB/#RRGGBB hex\n"
        "  ffmpeg treats an unparsed colour as black, which is the opposite of a flash."
    )


def _require_ffmpeg(what: str) -> None:
    if shutil.which("ffmpeg") is None:
        raise EffectError(
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
        raise EffectError(f"ffmpeg {what} failed (exit {result.returncode}):\n{tail}")


# ─── FILTER PROBE ────────────────────────────────────────────


@functools.lru_cache(maxsize=1)
def probe_filters() -> frozenset[str]:
    """Filter names in `ffmpeg -filters`. Empty frozenset when ffmpeg is absent. Cached."""
    if shutil.which("ffmpeg") is None:
        return frozenset()
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"],
        capture_output=True, text=True,
    )
    names: list[str] = []
    for line in result.stdout.splitlines():
        if "->" not in line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            names.append(parts[1])
    return frozenset(names)


def _probe_effect(name: str) -> Effect:
    builder = EFFECTS[name]
    return builder(**_PROBE_KW.get(name, {}))


def available() -> list[str]:
    """Sorted effect names that this ffmpeg build can actually render."""
    present = probe_filters()
    if not present:
        return []
    names = []
    for name in sorted(EFFECTS):
        req = _probe_effect(name).requires
        if all(f in present for f in req):
            names.append(name)
    return names


def unavailable() -> dict[str, tuple[str, ...]]:
    """{effect name: missing filters} for effects this build cannot render."""
    present = probe_filters()
    out: dict[str, tuple[str, ...]] = {}
    for name in sorted(EFFECTS):
        req = _probe_effect(name).requires
        if not present:
            out[name] = req
            continue
        missing = tuple(f for f in req if f not in present)
        if missing:
            out[name] = missing
    return out


def _missing_note(effect: Effect) -> Effect:
    extra = (
        "ffmpeg absent — fragment not validated against a live build; "
        "apply_to/preview will refuse until ffmpeg is on PATH."
    )
    notes = f"{effect.notes} {extra}".strip() if effect.notes else extra
    return replace(effect, notes=notes)


def build(name: str, **params) -> Effect:
    """Dispatch to a builder, validating params and filter availability.

    Unknown name -> EffectError listing close matches (difflib.get_close_matches).
    Missing filter -> EffectError naming the filter and the effect.
    Out-of-range param -> EffectError naming the param, the value and the valid range.
    """
    if name not in EFFECTS:
        matches = difflib.get_close_matches(name, EFFECTS.keys(), n=3, cutoff=0.4)
        hint = (
            f" Did you mean {', '.join(matches)}?"
            if matches
            else f" Known effects: {', '.join(sorted(EFFECTS))}."
        )
        raise EffectError(
            f"unknown effect {name!r}.{hint}\n"
            "  Pass one of the names returned by available()."
        )
    try:
        effect = EFFECTS[name](**params)
    except TypeError as exc:
        raise EffectError(
            f"{name}: {exc}\n"
            "  Pass builder params as keywords (width=, height=, at=, …)."
        ) from exc

    present = probe_filters()
    if not present:
        return _missing_note(effect)
    missing = [f for f in effect.requires if f not in present]
    if missing:
        listed = ", ".join(missing)
        raise EffectError(
            f"effect {name!r} needs ffmpeg filter(s) {listed} which this build does not have\n"
            "  Rebuild ffmpeg with the missing filter, or pick an effect from available()."
        )
    return effect


def chain(effects: list[Effect]) -> str:
    """Join effect fragments into one filter chain, comma-separated, in order.

    Collapses duplicate trailing `format=yuv420p` — several effects each append it
    and a chain with five of them is wasteful and, with zoompan in the mix,
    changes the result. One copy is kept, at the end.
    """
    if not effects:
        return ""
    parts: list[str] = []
    had_format = False
    for effect in effects:
        frag = effect.filters.strip().strip(",")
        while True:
            stripped = frag.rstrip(", ")
            if stripped.endswith("format=yuv420p"):
                had_format = True
                frag = stripped[: -len("format=yuv420p")].rstrip(", ")
            else:
                frag = stripped
                break
        if frag:
            parts.append(frag)
    if had_format:
        parts.append("format=yuv420p")
    return ",".join(parts)


# ─── FLASH ───────────────────────────────────────────────────


def flash(*, color: str = "white", at: float = 0.0, duration: float = 0.12,
          peak: float = 0.85) -> Effect:
    """A single-frame-ish blow-out at a cut. Ramps up fast and decays.

    `eq=brightness` with a `t`-dependent expression — not `blend` against a
    solid — because these fragments have to compose into a single-input filter
    chain. eval=frame is required: eq defaults to eval=init, which would freeze
    the first sample and look like a slow fade (or nothing).

    Color is validated (named + 0xRRGGBB) so an unparsed value cannot silently
    become black. The blow-out itself is a brightness pulse: a second colour
    source would break composition.
    """
    _timed(at, duration)
    _validate_colour(color)
    if not 0 < peak <= 1:
        raise EffectError(
            f"peak={peak} is out of range — valid: (0, 1]\n"
            "  0.85 is a full blow-out; keep it under 1 so eq does not clip to noise."
        )
    # Half-sine: verified on a 0x2a3d45 clip, luma 55 → 255 → 55 inside 0.12s.
    # A linear fade over the same window stays near the source luma.
    env = f"{_n(peak)}*sin(PI*(t-{_n(at)})/{_n(duration)})"
    filt = f"eq=brightness='{env}':eval=frame:{_between(at, duration)}"
    return Effect("flash", filt, ("eq",))


# ─── ZOOM & CAMERA ───────────────────────────────────────────


def zoom_punch(*, scale: float = 1.15, at: float = 0.0, duration: float = 0.4,
               fps: int = 30, width: int, height: int) -> Effect:
    """A quick push in and settle — the CapCut 'punch' on a beat.

    Cosine punch that returns: z='1+(scale-1)*(1-cos(2*PI*on/N))/2'. Linear
    zoom reads as a mistake. d=1 so each input frame produces one output frame;
    d=N (the still-image Ken-Burns default) would multiply clip duration by N.
    """
    _timed(at, duration)
    _positive_size(width, height)
    if scale <= 1:
        raise EffectError(
            f"scale={scale} is out of range — valid: > 1\n"
            "  1.15 is a punch; 1.0 is a no-op."
        )
    if fps <= 0:
        raise EffectError(
            f"fps={fps} is invalid — fps must be > 0\n"
            "  Pass the clip's frame rate so on/N lines up with the beat."
        )
    up = _zoom_upscale(width)
    n_frames = max(1, round(duration * fps))  # round(), not int() — truncation drops a frame
    start_on = max(0, round(at * fps))
    # zoompan has no working enable= on this build; gate in z and stamp the
    # window on an identity eq so the fragment stays inspectable.
    z = (
        f"if(between(on,{start_on},{start_on + n_frames}),"
        f"1+{_n(scale - 1)}*(1-cos(2*PI*(on-{start_on})/{n_frames}))/2,1)"
    )
    filt = (
        f"scale={up}:-2,"
        f"zoompan=z='{z}':d=1:s={width}x{height}:fps={fps}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)',"
        f"{_identity_enable(at, duration)},"
        f"format=yuv420p"
    )
    return Effect("zoom_punch", filt, ("scale", "zoompan", "eq", "format"))


def shake(*, amplitude: float = 12, at: float = 0.0, duration: float = 0.5,
          frequency: float = 14, width: int, height: int) -> Effect:
    """Camera shake for an impact moment.

    Scale up then crop with x/y driven by two sines at incommensurate
    frequencies (1 and the golden ratio). random() is not reproducible — two
    renders of the same ad would differ. amplitude must be > 0; 0 raises
    EffectError rather than emitting a no-op crop (a zero shake is a caller
    mistake, not a look).

    crop's enable= is unimplemented on this ffmpeg; the window is applied
    inside the x/y expressions with between(), and stamped on identity eq.
    """
    _timed(at, duration)
    _positive_size(width, height)
    if amplitude <= 0:
        raise EffectError(
            f"amplitude={amplitude} is invalid — amplitude must be > 0\n"
            "  Pass a pixel offset (12 is a hit; 0 is a no-op we refuse)."
        )
    if frequency <= 0:
        raise EffectError(
            f"frequency={frequency} is invalid — frequency must be > 0\n"
            "  14 Hz reads as an impact; keep it in the 8–20 range."
        )
    # Margin on EACH axis (left and right, top and bottom). Must exceed
    # amplitude or the crop walks off the frame and ffmpeg errors.
    margin = math.ceil(amplitude) * 2 + 2
    sw = width + 2 * margin
    sh = height + 2 * margin
    # yuv420p libx264 refuses odd dimensions.
    sw += sw % 2
    sh += sh % 2
    amp = _n(amplitude)
    fr = _n(frequency)
    # 1.6180339887 ≈ φ so the x/y periods never close over a short window.
    x = (
        f"{margin}+{amp}*sin(2*PI*{fr}*t)*between(t,{_n(at)},{_n(at + duration)})"
    )
    y = (
        f"{margin}+{amp}*sin(2*PI*{fr}*1.6180339887*t)"
        f"*between(t,{_n(at)},{_n(at + duration)})"
    )
    filt = (
        f"scale={sw}:{sh},"
        f"crop=w={width}:h={height}:x='{x}':y='{y}',"
        f"{_identity_enable(at, duration)}"
    )
    return Effect("shake", filt, ("scale", "crop", "eq"))


def ken_burns(*, direction: str, zoom_from: float = 1.0, zoom_to: float = 1.15,
              duration: float, width: int, height: int, fps: int = 30) -> Effect:
    """Slow zoom/pan across a held shot — the product beauty shot.

    Same zoompan jitter fix as zoom_punch: scale=UP:-2 first, format=yuv420p
    after. Cosine ease so it starts and ends gently. d=1 preserves clip
    duration (the still-image default d=90 would stretch a 3s clip to minutes).
    Does not take `at` — it transforms geometry for the whole clip.
    """
    if duration <= 0:
        raise EffectError(
            f"duration={duration} is invalid — duration must be > 0\n"
            "  Pass the clip length in seconds so the ease lands on the last frame."
        )
    _positive_size(width, height)
    if fps <= 0:
        raise EffectError(
            f"fps={fps} is invalid — fps must be > 0\n"
            "  Pass the clip's frame rate."
        )
    if direction not in KEN_BURNS_DIRECTIONS:
        raise EffectError(
            f"direction={direction!r} is invalid — valid: {', '.join(KEN_BURNS_DIRECTIONS)}\n"
            "  'in'/'out' zoom; 'left'/'right'/'up'/'down' pan; 'in-left'/'in-right' combine."
        )
    if zoom_from <= 0 or zoom_to <= 0:
        raise EffectError(
            f"zoom_from={zoom_from}, zoom_to={zoom_to} is invalid — both must be > 0\n"
            "  1.0 → 1.15 is a slow push-in."
        )
    up = _zoom_upscale(width)
    n_frames = max(1, round(duration * fps))
    # ease 0→1, holds at 1 after N so a longer clip does not reverse.
    ease = f"if(lt(on,{n_frames}),(1-cos(PI*on/{n_frames}))/2,1)"
    if direction == "out":
        z = f"{_n(zoom_to)}+({_n(zoom_from)}-{_n(zoom_to)})*({ease})"
    else:
        z = f"{_n(zoom_from)}+({_n(zoom_to)}-{_n(zoom_from)})*({ease})"

    # x/y are the top-left of the crop in input pixels. max_x = iw*(1-1/zoom).
    cx = "iw/2-(iw/zoom/2)"
    cy = "ih/2-(ih/zoom/2)"
    left = f"(iw-iw/zoom)*(1-({ease}))"
    right = f"(iw-iw/zoom)*({ease})"
    up_p = f"(ih-ih/zoom)*(1-({ease}))"
    down = f"(ih-ih/zoom)*({ease})"
    if direction in ("in", "out"):
        x, y = cx, cy
    elif direction in ("left", "in-left"):
        x, y = left, cy
    elif direction in ("right", "in-right"):
        x, y = right, cy
    elif direction == "up":
        x, y = cx, up_p
    else:  # down
        x, y = cx, down

    filt = (
        f"scale={up}:-2,"
        f"zoompan=z='{z}':d=1:s={width}x{height}:fps={fps}:"
        f"x='{x}':y='{y}',"
        f"format=yuv420p"
    )
    return Effect("ken_burns", filt, ("scale", "zoompan", "format"))


# ─── GLITCH & BLUR ───────────────────────────────────────────


def glitch(*, at: float = 0.0, duration: float = 0.25, intensity: float = 0.6) -> Effect:
    """RGB-split digital glitch. `rgbashift` + `noise`, gated.

    intensity 0..1 maps to shift pixels and noise strength. Keep it short — a
    glitch longer than ~0.4s stops reading as a transition artifact and starts
    reading as a broken file. all_seed is fixed so two renders match.
    """
    _timed(at, duration)
    if not 0 <= intensity <= 1:
        raise EffectError(
            f"intensity={intensity} is out of range — valid: 0..1\n"
            "  0.6 is a visible split; 1.0 is a broken-file look."
        )
    shift = max(1, round(intensity * 20))
    gv = max(1, round(shift / 2))
    noise_s = max(1, round(intensity * 60))
    enable = _between(at, duration)
    filt = (
        f"rgbashift=rh={shift}:bh=-{shift}:gv={gv}:{enable},"
        f"noise=alls={noise_s}:allf=t:all_seed=42:{enable}"
    )
    return Effect("glitch", filt, ("rgbashift", "noise"))


def whip_blur(*, at: float = 0.0, duration: float = 0.2, sigma: float = 24,
              direction: str = "horizontal") -> Effect:
    """Directional motion blur for a whip-pan cut. `gblur` with sigma ramped by t.

    gblur's sigma is a float, not an expression (ffmpeg 8.1.1 rejects
    `sigma='24*sin(…)'` as 'Unable to parse'). Approximate the cosine pulse
    with a handful of enable-gated gblur steps so sigma still ramps up and
    back down. A constant blur is not a whip, it is a mistake. sigmaV=0
    (horizontal) / sigma≈0 (vertical) keeps it directional.
    """
    _timed(at, duration)
    if sigma <= 0:
        raise EffectError(
            f"sigma={sigma} is invalid — sigma must be > 0\n"
            "  24 is a whip; 1 is a mist."
        )
    if direction not in WHIP_DIRECTIONS:
        raise EffectError(
            f"direction={direction!r} is invalid — valid: {', '.join(WHIP_DIRECTIONS)}\n"
            "  Match the pan you are cutting to."
        )
    steps = 7
    parts: list[str] = []
    for i in range(steps):
        t0 = at + duration * i / steps
        t1 = at + duration * (i + 1) / steps
        phase = (i + 0.5) / steps
        sig = max(0.01, sigma * math.sin(math.pi * phase))
        if direction == "horizontal":
            blur = f"gblur=sigma={_n(sig)}:sigmaV=0"
        else:
            blur = f"gblur=sigma=0.01:sigmaV={_n(sig)}"
        parts.append(f"{blur}:{_between(t0, t1 - t0)}")
    return Effect("whip_blur", ",".join(parts), ("gblur",))


# ─── GRADE & EMPHASIS ────────────────────────────────────────


def vignette_pulse(*, at: float = 0.0, duration: float = 0.6,
                   strength: float = 0.4) -> Effect:
    """A darkening squeeze at the edges — cheap dramatic emphasis on a claim."""
    _timed(at, duration)
    if not 0 < strength <= 1:
        raise EffectError(
            f"strength={strength} is out of range — valid: (0, 1]\n"
            "  0.4 is a squeeze; 1.0 tunnels the frame to a letterbox."
        )
    # eval=frame: vignette defaults to eval=init, which would freeze angle at t=0
    # (outside the window) and the pulse would never fire.
    angle = f"PI/5+{_n(strength)}*sin(PI*(t-{_n(at)})/{_n(duration)})"
    filt = (
        f"vignette=angle='{angle}':eval=frame:{_between(at, duration)}"
    )
    return Effect("vignette_pulse", filt, ("vignette",))


def color_pop(*, saturation: float = 1.3, contrast: float = 1.1,
              at: float | None = None, duration: float | None = None) -> Effect:
    """Lift saturation and contrast, optionally only over a window. `eq`.

    When at/duration are None the effect applies to the whole clip and no
    `enable=` is emitted — an always-on grade should not pay for a per-frame
    predicate.
    """
    if saturation <= 0:
        raise EffectError(
            f"saturation={saturation} is invalid — saturation must be > 0\n"
            "  1.0 is identity; 1.3 is a pop."
        )
    if contrast <= 0:
        raise EffectError(
            f"contrast={contrast} is invalid — contrast must be > 0\n"
            "  1.0 is identity; 1.1 is a mild lift."
        )
    if (at is None) != (duration is None):
        raise EffectError(
            f"at={at}, duration={duration} — pass both for a window, or neither for always-on\n"
            "  An always-on grade should not emit enable=."
        )
    filt = f"eq=saturation={_n(saturation)}:contrast={_n(contrast)}"
    if at is not None and duration is not None:
        _timed(at, duration)
        filt = f"{filt}:{_between(at, duration)}"
    return Effect("color_pop", filt, ("eq",))


# ─── TIME ────────────────────────────────────────────────────


def speed_ramp(*, segments: list[tuple[float, float, float]]) -> Effect:
    """Variable speed. segments is [(start, end, factor), ...].

    `setpts` with a piecewise expression. factor > 1 is faster. This changes
    the clip's duration and the caller must re-measure — the ad pipeline plans
    captions against clip boundaries and a speed ramp invalidates that plan.
    AUDIO IS NOT HANDLED by this fragment (atempo is a separate chain).
    """
    if not segments:
        raise EffectError(
            "segments is empty — need at least one (start, end, factor)\n"
            "  Pass [(0, clip_duration, 2)] to double speed over the whole clip."
        )
    for i, (start, end, factor) in enumerate(segments):
        if factor <= 0:
            raise EffectError(
                f"factor={factor} in segment {i} ({start:g}–{end:g}) is invalid — factor must be > 0\n"
                "  factor > 1 is faster, 0 < factor < 1 is slower, 1 is identity."
            )
        if end <= start:
            raise EffectError(
                f"segment {i} ({start:g}–{end:g}) has end <= start\n"
                "  Pass a half-open window with end > start."
            )
        if start < 0:
            raise EffectError(
                f"segment {i} starts at {start:g} — start must be >= 0\n"
                "  Times are seconds from the beginning of the clip."
            )
    for i in range(1, len(segments)):
        prev_s, prev_e, _ = segments[i - 1]
        s, e, _ = segments[i]
        if s < prev_s:
            raise EffectError(
                f"segments unsorted: ({prev_s:g}–{prev_e:g}) then ({s:g}–{e:g})\n"
                "  Sort by start time."
            )
        if s < prev_e:
            raise EffectError(
                f"segments overlap: ({prev_s:g}–{prev_e:g}) and ({s:g}–{e:g})\n"
                "  Give each window a unique range; gaps play at 1x."
            )

    # Implicit 1x before / between / after the caller-supplied windows.
    filled: list[tuple[float, float, float]] = []
    cursor = 0.0
    for start, end, factor in segments:
        if start > cursor:
            filled.append((cursor, start, 1.0))
        filled.append((start, end, factor))
        cursor = end
    # After the last segment we keep 1x via a catch-all so a clip longer than
    # the last `end` still plays (at original speed).
    filled.append((cursor, cursor + 1e9, 1.0))

    # output_seconds(T) = offset + (T-start)/factor inside each window.
    # setpts wants PTS = output_seconds / TB. (T/2)*TB was the bug that
    # collapsed a 3s clip to two frames; divide by TB, do not multiply.
    offset = 0.0
    clauses: list[str] = []
    for start, end, factor in filled:
        out_at_t = f"{_n(offset)}+(T-{_n(start)})/{_n(factor)}"
        clauses.append((end, out_at_t))
        offset += (end - start) / factor

    expr = clauses[-1][1]
    for end, out_at_t in reversed(clauses[:-1]):
        expr = f"if(lt(T,{_n(end)}),{out_at_t},{expr})"
    filt = f"setpts='{expr}/TB'"
    notes = (
        "Changes the clip's duration; the caller must re-measure — caption "
        "plans against clip boundaries are now invalid. AUDIO IS NOT HANDLED "
        "(atempo is a separate chain)."
    )
    return Effect("speed_ramp", filt, ("setpts",), notes=notes)


def freeze_frame(*, at: float, duration: float = 0.5, fps: int = 30) -> Effect:
    """Hold one frame — the 'record scratch' beat.

    `loop` inserts extra copies of the frame at `at`. This LENGTHENS the clip
    by `duration` and the caller must re-measure. fps defaults to 30 because
    loop's count is in frames; pass fps= to match the clip.
    """
    _timed(at, duration)
    if fps <= 0:
        raise EffectError(
            f"fps={fps} is invalid — fps must be > 0\n"
            "  loop counts frames; pass the clip's frame rate."
        )
    n_extra = max(1, round(duration * fps))
    start = max(0, round(at * fps))
    # time= is seconds and survives a fps mismatch better than start= alone.
    filt = (
        f"loop=loop={n_extra}:size=1:start={start}:time={_n(at)},"
        f"{_identity_enable(at, duration)}"
    )
    notes = (
        f"LENGTHENS the clip by {duration:g}s; the caller must re-measure. "
        "Audio is not held (loop is video-only)."
    )
    return Effect("freeze_frame", filt, ("loop", "eq"), notes=notes)


# ─── REGISTRY ────────────────────────────────────────────────

EFFECTS: dict[str, Callable[..., Effect]] = {
    "flash": flash,
    "zoom_punch": zoom_punch,
    "shake": shake,
    "glitch": glitch,
    "whip_blur": whip_blur,
    "ken_burns": ken_burns,
    "speed_ramp": speed_ramp,
    "vignette_pulse": vignette_pulse,
    "color_pop": color_pop,
    "freeze_frame": freeze_frame,
}


# ─── RENDER ──────────────────────────────────────────────────


def duration_of(path: Path) -> float | None:
    """Copy the shape of admux.duration_of — ffprobe format=duration, None on absence."""
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


def _probe_size(path: Path) -> tuple[int, int] | None:
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


def _window_end(effects: list[Effect], filt: str) -> float | None:
    """Latest enable= window end, ignoring duration-changing effects.

    freeze_frame's window is the hold itself and is allowed to extend past the
    source duration — that is the point. speed_ramp has no enable=.
    """
    skip = {id(e) for e in effects if e.name in {"freeze_frame", "speed_ramp"}}
    # Search the chain; duration-changing fragments still contain enable= on
    # the identity eq, so strip those fragments first.
    searchable = filt
    for e in effects:
        if id(e) in skip and e.filters:
            searchable = searchable.replace(e.filters, "", 1)
    ends = [float(m.group(2)) for m in _ENABLE_BETWEEN.finditer(searchable)]
    return max(ends) if ends else None


def apply_to(clip: Path, effects: list[Effect], output: Path) -> Path:
    """Render a clip with the effect chain applied. Raises EffectError with stderr tail."""
    clip = Path(clip)
    output = Path(output)
    if not clip.is_file():
        raise EffectError(
            f"clip not found: {clip}\n"
            "  Pass a path to an existing video file."
        )
    _require_ffmpeg("apply effects")
    output.parent.mkdir(parents=True, exist_ok=True)

    if not effects:
        # Empty chain: stream-copy, do not re-encode.
        run_ffmpeg(["-i", str(clip), "-c", "copy", str(output)], "apply effects (copy)")
        return output

    filt = chain(effects)
    clip_dur = duration_of(clip)
    end = _window_end(effects, filt)
    if clip_dur is not None and end is not None and end > clip_dur + 0.05:
        raise EffectError(
            f"effect window ends at {end:g}s but the clip is only {clip_dur:g}s long\n"
            "  Shorten at+duration so the window fits inside the clip."
        )

    changes_dur = any(e.name in {"speed_ramp", "freeze_frame"} for e in effects)
    args = ["-i", str(clip), "-vf", filt, "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if changes_dur:
        # Audio would keep the original duration and lie about the container
        # length; atempo is a separate chain (see speed_ramp notes).
        args += ["-map", "0:v:0", "-an"]
    else:
        args += ["-c:a", "copy"]
    args.append(str(output))
    run_ffmpeg(args, f"apply {', '.join(e.name for e in effects)}")
    return output


def preview(effect: Effect, output: Path, *, seconds: float = 2.0,
            width: int = 384, height: int = 384) -> Path:
    """Render the effect over a generated test pattern — for eyeballing one effect without
    a real clip. Uses lavfi testsrc2, so it needs no input file."""
    output = Path(output)
    _require_ffmpeg("preview an effect")
    if seconds <= 0:
        raise EffectError(
            f"seconds={seconds} is invalid — seconds must be > 0\n"
            "  Pass how long the test pattern should run."
        )
    _positive_size(width, height)
    output.parent.mkdir(parents=True, exist_ok=True)
    filt = chain([effect]) or "null"
    src = f"testsrc2=s={width}x{height}:r=30:d={_n(seconds)}"
    args = [
        "-f", "lavfi", "-i", src,
        "-vf", filt,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-an",
        str(output),
    ]
    run_ffmpeg(args, f"preview {effect.name}")
    return output
