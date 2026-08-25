"""Timed graphic overlays for social ads: logo bugs, price badges, arrows, lower thirds.

## Why this module exists

A social ad that is only footage plus captions looks unfinished next to the ads it
competes with. Real ads carry a logo bug in the corner, a price badge that pops in on
the CTA, an arrow pointing at the product, a "50% OFF" starburst. AI video models
cannot render any of these reliably — they garble text and invent branding — so the
graphic has to be built as a real asset and composited afterward. Captions already do
this for text; this module is the same capability for graphics, plus the motion that
makes a badge read as designed rather than pasted: scale-in with overshoot, fade out.

## Why Pillow + overlay expressions, not ffmpeg text/draw filters

This machine's ffmpeg (8.1.1, Homebrew) is built without libfreetype/libass: no
drawtext, no subtitles, no ass. captions.py already pays that cost for type — render a
transparent PNG with Pillow, composite with ffmpeg's `overlay`. The same constraint
applies to graphics, so the same pattern applies: Pillow renders the asset, ffmpeg
animates it. One honest difference from the naive plan: `colorchannelmixer=aa=` does
NOT accept expressions on this build (its options are plain doubles — verified on
8.1.1), so animated alpha goes through `geq=a='alpha(X,Y)*<expr>'`, which does support
a per-frame `T`. Static opacity still uses a constant `colorchannelmixer=aa=<c>`.

ffmpeg expressions cannot express arbitrary easing curves. Eased tracks are therefore
SAMPLED in Python at SAMPLE_HZ and emitted as piecewise `if(lt(t,...),v, ...)` ladders
— a 0.5s animation at 30 Hz is 15 rungs. Past MAX_RUNGS the ladder falls back to
piecewise-linear interpolation between coarser samples and says so in the overlay's
`notes`.

Animated overlays are fed as `-loop 1 -framerate fps -t end` inputs, because per-frame
filters (scale=eval=frame, rotate, geq) only run on frames that exist — a single-frame
PNG input is processed once and the "animation" would freeze on its first value.
Static overlays keep the plain single-frame input: overlay's default eof_action=repeat
holds it for the whole window.

## Track semantics

A track animates one property over absolute film time. `x`/`y` values REPLACE the
overlay's `offset` (fractions of frame width/height from the anchor). `scale` and
`opacity` are multipliers on the overlay's static `scale`/`opacity`. `rotation` is
degrees, replacing the static value. Before the first key a track holds the first
value, after the last key it holds the last.

Pillow is imported lazily inside the functions that draw; ffmpeg is only shelled out
to in burn(). Easing, sampling, positioning and filter-string building import and run
under plain python3 with no deps and no network.
"""

from __future__ import annotations

import math
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from captions import (            # DO NOT reimplement these — import them
    find_font, font_for, has_arabic, reshape_arabic, shape,
    ARABIC_FONT_PATHS, LATIN_FONT_PATHS,
    CaptionError,
)
from captions import probe_size


class OverlayError(Exception):
    """Raised when an overlay, track or keyframe is unusable. Message names the overlay id."""


# ─── EASING ──────────────────────────────────────────────────
# back_out/elastic_out deliberately leave [0,1] mid-curve — that overshoot IS the
# effect. Endpoints are still exact 0 and 1 so a sampled track lands where it was
# aimed; elastic_out needs explicit guards because its closed form overshoots the
# endpoint by ~5e-4 (2**-10 * sin(...)).

def _linear(p: float) -> float:
    return p


def _ease_in(p: float) -> float:
    return p * p


def _ease_out(p: float) -> float:
    return 1.0 - (1.0 - p) * (1.0 - p)


def _ease_in_out(p: float) -> float:
    return 2 * p * p if p < 0.5 else 1.0 - ((-2 * p + 2) ** 2) / 2


def _cubic_in_out(p: float) -> float:
    return 4 * p * p * p if p < 0.5 else 1.0 - ((-2 * p + 2) ** 3) / 2


_BACK_C1 = 1.70158
_BACK_C3 = _BACK_C1 + 1.0


def _back_out(p: float) -> float:
    return 1.0 + _BACK_C3 * (p - 1) ** 3 + _BACK_C1 * (p - 1) ** 2


def _back_in(p: float) -> float:
    return _BACK_C3 * p ** 3 - _BACK_C1 * p * p


def _bounce_out(p: float) -> float:
    n1, d1 = 7.5625, 2.75
    if p < 1 / d1:
        return n1 * p * p
    if p < 2 / d1:
        p -= 1.5 / d1
        return n1 * p * p + 0.75
    if p < 2.5 / d1:
        p -= 2.25 / d1
        return n1 * p * p + 0.9375
    p -= 2.625 / d1
    return n1 * p * p + 0.984375


def _elastic_out(p: float) -> float:
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    c4 = (2 * math.pi) / 3
    return 2 ** (-10 * p) * math.sin((p * 10 - 0.75) * c4) + 1


def _expo_out(p: float) -> float:
    return 1.0 if p >= 1.0 else 1.0 - 2 ** (-10 * p)


EASINGS: dict[str, Callable[[float], float]] = {
    "linear": _linear,
    "ease_in": _ease_in,
    "ease_out": _ease_out,
    "ease_in_out": _ease_in_out,
    "back_out": _back_out,
    "back_in": _back_in,
    "bounce_out": _bounce_out,
    "elastic_out": _elastic_out,
    "expo_out": _expo_out,
    "cubic_in_out": _cubic_in_out,
}


def ease(name: str, p: float) -> float:
    """Apply a named easing to a normalised progress p in [0,1].

    Clamps p into [0,1] before applying — a caller sampling slightly past the end of a
    track must get the end value, not an extrapolated one. back_out and elastic_out
    deliberately return values OUTSIDE [0,1] mid-curve; that overshoot is the effect and
    must not be clamped on the way out.
    """
    if name not in EASINGS:
        raise OverlayError(
            f"unknown easing {name!r} — valid: {', '.join(EASINGS)}"
        )
    p = min(max(p, 0.0), 1.0)
    # Endpoints are exact: the closed forms of back_out/bounce_out miss 0 and 1 by
    # ~2e-16 of float dust, and a sampled track must land exactly where it was aimed.
    if p == 0.0:
        return 0.0
    if p == 1.0:
        return 1.0
    return EASINGS[name](p)


# ─── TRACKS & KEYFRAMES ─────────────────────────────────────

PROPS = ("x", "y", "scale", "opacity", "rotation")


@dataclass(frozen=True)
class Keyframe:
    t: float                   # seconds, absolute on the FILM timeline
    value: float


@dataclass
class Track:
    prop: str                  # "x" | "y" | "scale" | "opacity" | "rotation"
    keys: list[Keyframe]
    easing: str = "ease_out"

    def __post_init__(self):
        if self.prop not in PROPS:
            raise OverlayError(
                f"track prop {self.prop!r} is not animatable — valid: {', '.join(PROPS)}"
            )
        if self.easing not in EASINGS:
            raise OverlayError(
                f"track easing {self.easing!r} is unknown — valid: {', '.join(EASINGS)}"
            )
        if not self.keys:
            raise OverlayError(f"track on {self.prop!r} has zero keyframes")
        self.keys = sorted(self.keys, key=lambda k: (k.t, k.value))
        ts = [k.t for k in self.keys]
        for a, b in zip(ts, ts[1:]):
            if a == b:
                raise OverlayError(
                    f"track on {self.prop!r} has two keys at t={a:.3f} — an instantaneous "
                    "jump is two keys a frame apart, not one ambiguous key"
                )


def sample(track: Track, t: float) -> float:
    """Value of the track at time t.

    Before the first key -> the first key's value. After the last -> the last key's value.
    Between two keys -> eased interpolation. A single key -> that constant.
    """
    keys = track.keys
    if len(keys) == 1 or t <= keys[0].t:
        return keys[0].value
    if t >= keys[-1].t:
        return keys[-1].value
    for a, b in zip(keys, keys[1:]):
        if a.t <= t <= b.t:
            p = (t - a.t) / (b.t - a.t)
            return a.value + (b.value - a.value) * ease(track.easing, p)
    return keys[-1].value  # unreachable: t is bracketed above


# ─── THE OVERLAY ─────────────────────────────────────────────

ANCHORS = ("top-left", "top-center", "top-right",
           "center-left", "center", "center-right",
           "bottom-left", "bottom-center", "bottom-right")


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


@dataclass
class Overlay:
    asset: Path                # a PNG with alpha
    start: float               # seconds on the film timeline
    end: float
    id: str = ""               # for error messages; defaults to asset stem
    anchor: str = "center"
    offset: tuple[float, float] = (0.0, 0.0)   # FRACTIONS of frame w/h, from the anchor
    scale: float = 1.0                          # fraction of frame WIDTH the asset spans
    opacity: float = 1.0
    rotation: float = 0.0                       # degrees
    tracks: list[Track] = field(default_factory=list)
    fade_in: float = 0.0                        # seconds; sugar for an opacity track
    fade_out: float = 0.0
    notes: list[str] = field(default_factory=list)  # filled by overlay_filter, e.g. rung-cap fallbacks

    def __post_init__(self):
        self.asset = Path(self.asset)
        if not self.id:
            self.id = self.asset.stem
        if not self.asset.is_file():
            raise OverlayError(
                f"overlay {self.id!r}: asset not found: {self.asset}\n"
                "  Render it first (badge(), arrow(), ...) or pass an existing PNG."
            )
        if not _is_num(self.start) or not _is_num(self.end):
            raise OverlayError(
                f"overlay {self.id!r}: start/end must be numbers, got "
                f"{self.start!r}/{self.end!r}"
            )
        self.start, self.end = float(self.start), float(self.end)
        if self.start < 0:
            raise OverlayError(f"overlay {self.id!r}: start ({self.start}) must be >= 0")
        if self.end <= self.start:
            raise OverlayError(
                f"overlay {self.id!r}: end ({self.end}) must be greater than "
                f"start ({self.start})"
            )
        if not _is_num(self.scale) or not (0.0 < float(self.scale) <= 1.0):
            raise OverlayError(
                f"overlay {self.id!r}: scale ({self.scale!r}) must be in (0, 1] — it is "
                "a fraction of the frame WIDTH, so above 1 can never fit"
            )
        if not _is_num(self.opacity) or not (0.0 <= float(self.opacity) <= 1.0):
            raise OverlayError(
                f"overlay {self.id!r}: opacity ({self.opacity!r}) must be in [0, 1]"
            )
        if self.anchor not in ANCHORS:
            raise OverlayError(
                f"overlay {self.id!r}: anchor {self.anchor!r} is unknown — valid: "
                f"{', '.join(ANCHORS)}"
            )
        if not _is_num(self.rotation):
            raise OverlayError(
                f"overlay {self.id!r}: rotation must be a number of degrees, got "
                f"{self.rotation!r}"
            )
        for name, value in (("fade_in", self.fade_in), ("fade_out", self.fade_out)):
            if not _is_num(value) or value < 0:
                raise OverlayError(
                    f"overlay {self.id!r}: {name} must be >= 0 seconds, got {value!r}"
                )
        self.fade_in, self.fade_out = float(self.fade_in), float(self.fade_out)
        if self.fade_in + self.fade_out > self.end - self.start:
            raise OverlayError(
                f"overlay {self.id!r}: fade_in ({self.fade_in}) + fade_out "
                f"({self.fade_out}) = {self.fade_in + self.fade_out:.3f}s exceeds the "
                f"{self.end - self.start:.3f}s window — the fades would overlap and the "
                "overlay never reaches full opacity"
            )
        self.tracks = [t if isinstance(t, Track) else Track(**t) for t in self.tracks]


# ─── POSITIONING & SAFE ZONES ────────────────────────────────
# TikTok's UI covers the top 10%, right 10% and bottom 20% of the frame (see
# adspec.PLATFORMS). A price badge anchored bottom-right sits under the Follow button
# and the caption, invisible. So positions are CLAMPED into safe["box"], never
# rejected, and validate() reports every clamp so the user learns instead of silently
# getting a different layout — the same contract captions.py keeps.

def _track_for(ov: Overlay, prop: str) -> Track | None:
    for track in ov.tracks:
        if track.prop == prop:
            return track
    return None


def _anchor_point(anchor: str, frame_w: int, frame_h: int) -> tuple[float, float]:
    """The frame point the anchor names, and how much of the asset backs up from it."""
    horiz = anchor.split("-")[1] if "-" in anchor else "center"
    vert = anchor.split("-")[0]
    ax = {"left": 0.0, "center": frame_w / 2, "right": float(frame_w)}[horiz]
    ay = {"top": 0.0, "center": frame_h / 2, "bottom": float(frame_h)}[vert]
    return ax, ay


def _align_back(anchor: str, w: float, h: float) -> tuple[float, float]:
    horiz = anchor.split("-")[1] if "-" in anchor else "center"
    vert = anchor.split("-")[0]
    bx = {"left": 0.0, "center": w / 2, "right": w}[horiz]
    by = {"top": 0.0, "center": h / 2, "bottom": h}[vert]
    return bx, by


def _position(ov: Overlay, t: float, *, frame_w: int, frame_h: int,
              asset_w: float, asset_h: float, safe: dict | None,
              clamp: bool) -> tuple[float, float]:
    ox, oy = ov.offset
    xt, yt = _track_for(ov, "x"), _track_for(ov, "y")
    if xt is not None:
        ox = sample(xt, t)
    if yt is not None:
        oy = sample(yt, t)
    ax, ay = _anchor_point(ov.anchor, frame_w, frame_h)
    bx, by = _align_back(ov.anchor, asset_w, asset_h)
    x = ax + ox * frame_w - bx
    y = ay + oy * frame_h - by
    if clamp:
        box = tuple(safe["box"]) if safe else (0, 0, frame_w, frame_h)
        x = min(max(x, box[0]), max(box[0], box[2] - asset_w))
        y = min(max(y, box[1]), max(box[1], box[3] - asset_h))
    return x, y


def resolve_position(ov: Overlay, t: float, *, frame_w: int, frame_h: int,
                     asset_w: int, asset_h: int, safe: dict | None) -> tuple[int, int]:
    """Top-left pixel position of the asset at time t, clamped into the safe box.

    asset_w/asset_h are the DISPLAYED (post-scale, post-rotate-canvas) pixel dims at t;
    callers compute them with _canvas_size(). x/y tracks replace the static offset.
    """
    x, y = _position(ov, t, frame_w=frame_w, frame_h=frame_h,
                     asset_w=asset_w, asset_h=asset_h, safe=safe, clamp=True)
    return round(x), round(y)


def _even(v: float) -> int:
    """Round to even, minimum 2 — x264 and several filters demand even dims."""
    return max(2, 2 * round(v / 2))


def _scaled_size(ov: Overlay, t: float, frame_w: int,
                 asset_w: int, asset_h: int) -> tuple[int, int]:
    """Displayed pixel dims before rotation: ov.scale (x scale track) of the frame width.

    Capped at the frame width: an overlay wider than the frame can never be placed, so
    'asset larger than the frame' becomes 'scaled to fit', never an overflow.
    """
    mult = 1.0
    st = _track_for(ov, "scale")
    if st is not None:
        mult = max(0.01, sample(st, t))
    w = min(frame_w * float(ov.scale) * mult, float(frame_w))
    w = _even(w)
    h = _even(w * asset_h / asset_w)
    return w, h


def _canvas_size(ov: Overlay, t: float, frame_w: int, frame_h: int,
                 asset_w: int, asset_h: int) -> tuple[int, int]:
    """The pixel dims of the frame the overlay filter actually composites at time t.

    Rotation runs through ffmpeg's rotate filter, whose ow/oh are evaluated ONCE at
    filter init — rotw(a)/roth(a) referencing the angle are rejected by this build
    (verified ffmpeg 8.1.1). So a rotated overlay rides on a constant square canvas the
    size of the diagonal of its largest scaled frame; the asset stays centred inside it.
    """
    w, h = _scaled_size(ov, t, frame_w, asset_w, asset_h)
    if _track_for(ov, "rotation") is None and float(ov.rotation) == 0.0:
        return w, h
    if not _is_animated(ov):
        ang = math.radians(float(ov.rotation))
        cw = math.ceil(w * abs(math.cos(ang)) + h * abs(math.sin(ang)))
        ch = math.ceil(w * abs(math.sin(ang)) + h * abs(math.cos(ang)))
        return cw, ch
    times = _sample_times(ov.start, ov.end)
    wmax = max(_scaled_size(ov, ts, frame_w, asset_w, asset_h)[0] for ts in times)
    hmax = max(_scaled_size(ov, ts, frame_w, asset_w, asset_h)[1] for ts in times)
    d = _even(math.hypot(wmax, hmax) + 2)
    return d, d


# ─── EXPRESSION LADDERS ─────────────────────────────────────

SAMPLE_HZ = 30       # eased tracks are sampled at this rate for the expression ladder
MAX_RUNGS = 64       # past this, fall back to piecewise-linear between coarser samples


def _sample_times(start: float, end: float) -> list[float]:
    n = max(1, math.ceil((end - start) * SAMPLE_HZ))
    step = (end - start) / n
    return [start + i * step for i in range(n + 1)]


def _fmt(v: float) -> str:
    """3-4 decimals so floats never serialise as 1e-05 inside a filter expression."""
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _const_ladder(times: list[float], values: list[float]) -> str:
    """if(lt(t,t1),v0, if(lt(t,t2),v1, ... vN)) — one constant rung per sample."""
    expr = _fmt(values[-1])
    for i in range(len(times) - 2, -1, -1):
        expr = f"if(lt(t,{times[i + 1]:.3f}),{_fmt(values[i])},{expr})"
    return expr


def _linear_ladder(times: list[float], values: list[float]) -> str:
    """Same ladder shape, but each rung linearly interpolates to the next sample."""
    expr = _fmt(values[-1])
    for i in range(len(times) - 2, -1, -1):
        t0, t1 = times[i], times[i + 1]
        v0, v1 = values[i], values[i + 1]
        seg = f"{_fmt(v0)}+({_fmt(v1)}-({_fmt(v0)}))*(t-{t0:.3f})/{t1 - t0:.3f}"
        expr = f"if(lt(t,{t1:.3f}),{seg},{expr})"
    return expr


def _sampled_expr(ov: Overlay, values: list[float], what: str) -> str:
    """Constant-rung ladder, or a coarser piecewise-linear one past MAX_RUNGS."""
    times = _sample_times(ov.start, ov.end)
    rungs = len(times) - 1
    if rungs <= MAX_RUNGS:
        return _const_ladder(times, values)
    n = MAX_RUNGS
    step = (ov.end - ov.start) / n
    coarse_times = [ov.start + i * step for i in range(n + 1)]
    coarse_values = [values[round(i * rungs / n)] for i in range(n + 1)]
    ov.notes.append(
        f"{what}: {rungs} samples at {SAMPLE_HZ} Hz exceeds MAX_RUNGS={MAX_RUNGS}; "
        f"emitted {n} piecewise-linear rungs instead (easing curve approximated)"
    )
    return _linear_ladder(coarse_times, coarse_values)


# ─── FILTER GENERATION ──────────────────────────────────────

def _asset_size(path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as img:
        return img.size


def _is_animated(ov: Overlay) -> bool:
    return bool(ov.tracks) or ov.fade_in > 0 or ov.fade_out > 0


def _fade_envelope(ov: Overlay, t: float) -> float:
    """1.0 across the window, ramping 0->1 over fade_in and 1->0 over fade_out."""
    if ov.fade_in > 0 and t < ov.start + ov.fade_in:
        return max(0.0, (t - ov.start) / ov.fade_in)
    if ov.fade_out > 0 and t > ov.end - ov.fade_out:
        return max(0.0, (ov.end - t) / ov.fade_out)
    return 1.0


def _opacity_at(ov: Overlay, t: float) -> float:
    base = float(ov.opacity)
    ot = _track_for(ov, "opacity")
    if ot is not None:
        base *= sample(ot, t)
    return min(max(base * _fade_envelope(ov, t), 0.0), 1.0)


def overlay_filter(ov: Overlay, *, index: int, frame_w: int, frame_h: int,
                   safe: dict | None, fps: int = 30,
                   in_label: str, out_label: str) -> str:
    """One overlay's filter fragment: scale + optional rotate + alpha + timed overlay.

    Static overlays (no tracks, no fades) get a plain
        overlay=x=..:y=..:enable='between(t,start,end)'
    Animated ones get x/y as ffmpeg time expressions, and animated opacity via
    `geq=a='alpha(X,Y)*<expr>'` on the overlay input before compositing (this build's
    colorchannelmixer takes constant doubles only — see the module docstring).
    """
    asset_w, asset_h = _asset_size(ov.asset)
    animated = _is_animated(ov)
    scale_track = _track_for(ov, "scale")
    rot_track = _track_for(ov, "rotation")
    has_rotate = rot_track is not None or float(ov.rotation) != 0.0
    opacity_animated = _track_for(ov, "opacity") is not None or ov.fade_in > 0 or ov.fade_out > 0

    chain = []
    if scale_track is not None:
        times = _sample_times(ov.start, ov.end)
        ws = [_scaled_size(ov, ts, frame_w, asset_w, asset_h)[0] for ts in times]
        hs = [_scaled_size(ov, ts, frame_w, asset_w, asset_h)[1] for ts in times]
        w_expr = _sampled_expr(ov, ws, "scale track (width)")
        h_expr = _sampled_expr(ov, hs, "scale track (height)")
        chain.append(f"scale=w='{w_expr}':h='{h_expr}':eval=frame")
    else:
        w, h = _scaled_size(ov, ov.start, frame_w, asset_w, asset_h)
        chain.append(f"scale={w}:{h}")

    if has_rotate:
        cw, ch = _canvas_size(ov, ov.start, frame_w, frame_h, asset_w, asset_h)
        if rot_track is not None:
            times = _sample_times(ov.start, ov.end)
            rads = [math.radians(sample(rot_track, ts)) for ts in times]
            a_expr = _sampled_expr(ov, rads, "rotation track")
        else:
            a_expr = f"{math.radians(float(ov.rotation)):.6f}"
        # c=none: rotate's default fill is opaque black, which would box the asset.
        chain.append(f"rotate=a='{a_expr}':ow={cw}:oh={ch}:c=none")

    if opacity_animated:
        times = _sample_times(ov.start, ov.end)
        alphas = [_opacity_at(ov, ts) for ts in times]
        a_expr = _sampled_expr(ov, alphas, "opacity")
        # geq's time variable is T (t is not bound there); the ladder uses lt(t,..), so
        # rebind: the ladder text references t, geq evaluates it per frame as T.
        a_expr = a_expr.replace("lt(t,", "lt(T,").replace("*(t-", "*(T-")
        chain.append(
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*({a_expr})'"
        )
    elif float(ov.opacity) < 1.0:
        chain.append(f"colorchannelmixer=aa={float(ov.opacity):.4f}")

    enable = f"enable='between(t,{ov.start:.3f},{ov.end:.3f})'"
    if animated:
        times = _sample_times(ov.start, ov.end)
        xs, ys = [], []
        for ts in times:
            cw, ch = _canvas_size(ov, ts, frame_w, frame_h, asset_w, asset_h)
            x, y = resolve_position(ov, ts, frame_w=frame_w, frame_h=frame_h,
                                    asset_w=cw, asset_h=ch, safe=safe)
            xs.append(x)
            ys.append(y)
        x_expr = _sampled_expr(ov, xs, "x position")
        y_expr = _sampled_expr(ov, ys, "y position")
        pos = f"x='{x_expr}':y='{y_expr}'"
    else:
        cw, ch = _canvas_size(ov, ov.start, frame_w, frame_h, asset_w, asset_h)
        x, y = resolve_position(ov, ov.start, frame_w=frame_w, frame_h=frame_h,
                                asset_w=cw, asset_h=ch, safe=safe)
        pos = f"x={x}:y={y}"

    ov_label = f"ov{index}"
    prep = f"[{index}:v]{','.join(chain)}[{ov_label}]"
    link = f"[{in_label}][{ov_label}]overlay={pos}:{enable}[{out_label}]"
    return f"{prep};{link}"


def compose(overlays: list[Overlay], *, frame_w: int, frame_h: int,
            safe: dict | None = None, fps: int = 30,
            base_label: str = "0:v", out_label: str = "vout") -> tuple[str, list[str]]:
    """Full -filter_complex body for N overlays, plus the ffmpeg input args they need.

    Returns (filter_complex, input_args) where input_args is ['-i', 'badge.png', ...] in
    the order the labels assume. The caller prepends its own video input, so overlay
    asset k is ffmpeg input k+1. Animated overlays get a looped, finite input instead
    (see the module docstring for why).
    """
    if not overlays:
        return f"[{base_label}]null[{out_label}]", []

    fragments = []
    input_args: list[str] = []
    prev = base_label
    for k, ov in enumerate(overlays):
        index = k + 1
        out = out_label if k == len(overlays) - 1 else f"v{index}"
        fragments.append(overlay_filter(ov, index=index, frame_w=frame_w,
                                        frame_h=frame_h, safe=safe, fps=fps,
                                        in_label=prev, out_label=out))
        prev = out
        if _is_animated(ov):
            input_args += ["-loop", "1", "-framerate", str(fps),
                           "-t", f"{ov.end:.3f}", "-i", str(ov.asset)]
        else:
            input_args += ["-i", str(ov.asset)]
    return ";".join(fragments), input_args


# ─── VALIDATION ──────────────────────────────────────────────

def _has_alpha(path: Path) -> bool:
    from PIL import Image

    with Image.open(path) as img:
        if img.mode in ("RGBA", "LA", "PA"):
            return True
        return img.mode == "P" and "transparency" in img.info


def _box_at(ov: Overlay, t: float, frame_w: int, frame_h: int,
            safe: dict | None) -> tuple[int, int, int, int]:
    asset_w, asset_h = _asset_size(ov.asset)
    cw, ch = _canvas_size(ov, t, frame_w, frame_h, asset_w, asset_h)
    x, y = resolve_position(ov, t, frame_w=frame_w, frame_h=frame_h,
                            asset_w=cw, asset_h=ch, safe=safe)
    return x, y, x + cw, y + ch


def validate(overlays: list[Overlay], *, frame_w: int, frame_h: int,
             safe: dict | None, film_duration: float | None = None) -> list[str]:
    """Every problem, as human-readable strings. Empty list == fine.

    Warnings, not errors: a window past film_duration is legal while the film is still
    being assembled; an opaque asset is legal but almost never meant; a clamped position
    is corrected but the user must learn it moved — silent clamping is how a badge ends
    up under the Follow button with nobody knowing why.
    """
    warnings = []
    for ov in overlays:
        if film_duration is not None and ov.end > film_duration:
            warnings.append(
                f"overlay {ov.id!r}: window [{ov.start:.3f}, {ov.end:.3f}) ends past "
                f"the {film_duration:.3f}s film"
            )
        if not _has_alpha(ov.asset):
            warnings.append(
                f"overlay {ov.id!r}: asset {ov.asset} has no alpha channel — it will "
                "cover the frame as an opaque rectangle"
            )
        t_mid = (ov.start + ov.end) / 2
        asset_w, asset_h = _asset_size(ov.asset)
        cw, ch = _canvas_size(ov, t_mid, frame_w, frame_h, asset_w, asset_h)
        rx, ry = _position(ov, t_mid, frame_w=frame_w, frame_h=frame_h,
                           asset_w=cw, asset_h=ch, safe=safe, clamp=False)
        cx, cy = _position(ov, t_mid, frame_w=frame_w, frame_h=frame_h,
                           asset_w=cw, asset_h=ch, safe=safe, clamp=True)
        if round(rx) != round(cx) or round(ry) != round(cy):
            warnings.append(
                f"overlay {ov.id!r}: requested position ({round(rx)}, {round(ry)}) is "
                f"outside the safe box; clamped to ({round(cx)}, {round(cy)})"
            )

    ordered = sorted(overlays, key=lambda o: (o.start, o.end))
    for i, first in enumerate(ordered):
        for second in ordered[i + 1:]:
            if second.start >= first.end:
                break
            t_mid = (max(first.start, second.start) + min(first.end, second.end)) / 2
            a = _box_at(first, t_mid, frame_w, frame_h, safe)
            b = _box_at(second, t_mid, frame_w, frame_h, safe)
            if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                warnings.append(
                    f"overlays {first.id!r} and {second.id!r} overlap in both time and "
                    f"pixels at t={t_mid:.3f} "
                    f"({a} vs {b}) — they will draw over each other"
                )
    return warnings


# ─── BURNING ─────────────────────────────────────────────────

def _probe_fps(path: Path) -> int:
    """Integer frame rate for looped overlay inputs; falls back to 30."""
    if shutil.which("ffprobe") is None:
        return 30
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=avg_frame_rate", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        num, den = result.stdout.strip().splitlines()[0].split("/")
        fps = float(num) / float(den)
        return max(1, round(fps))
    except (ValueError, IndexError, ZeroDivisionError):
        return 30


def burn(video: Path, overlays: list[Overlay], output: Path, *,
         safe: dict | None = None) -> Path:
    """Render. Probes the video for size and fps. Raises OverlayError with stderr tail."""
    video, output = Path(video), Path(output)

    if not overlays:
        # Nothing to burn: copy the file and return — re-encoding for nothing is a
        # quality loss, and this path needs no ffmpeg at all.
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(video, output)
        return output

    if shutil.which("ffmpeg") is None:
        raise OverlayError(
            "ffmpeg not found on PATH — needed to burn overlays. "
            "Install: brew install ffmpeg"
        )

    probed = probe_size(video)
    if probed is None:
        raise OverlayError(
            f"could not probe the size of {video}\n"
            "  pass a video ffprobe can read"
        )
    frame_w, frame_h = probed
    fps = _probe_fps(video)

    filter_complex, input_args = compose(overlays, frame_w=frame_w, frame_h=frame_h,
                                         safe=safe, fps=fps)

    output.parent.mkdir(parents=True, exist_ok=True)
    # A list, never a shell string: asset paths with spaces or Windows separators must
    # survive untouched.
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video), *input_args,
        "-filter_complex", filter_complex,
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
        raise OverlayError(
            f"ffmpeg overlay burn failed (exit {result.returncode}) for {video}:\n{tail}"
        )
    return output


# ─── GRAPHIC PRESETS (Pillow-rendered assets) ───────────────
# Text rules, learned the hard way in captions.py and binding here:
#   * pick the font with font_for(text) — a mixed string gets the Arabic font, because
#     its Latin glyphs are adequate and its Arabic ones are not optional;
#   * shape() BEFORE measuring or drawing;
#   * wrap BEFORE reshaping, never after — reshaping then splitting cuts ligatures
#     apart and reverses reading order;
#   * never .upper() Arabic — a no-op on Arabic letters, so a mixed string shouts the
#     Latin half and leaves the Arabic half alone.

def _load_font(text: str, size: int):
    from PIL import ImageFont

    font_path = font_for(text)
    if font_path:
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            pass  # unreadable font file — fall through to the default
    try:
        return ImageFont.load_default(size)
    except TypeError:  # very old Pillow: load_default() takes no size
        return ImageFont.load_default()


def _rgba(color: str) -> tuple[int, int, int, int]:
    """'#rgb' | '#rrggbb' | '#rrggbbaa' -> RGBA tuple."""
    from PIL import ImageColor

    rgb = ImageColor.getrgb(color)
    if len(rgb) == 4:
        return rgb
    return (*rgb, 255)


def badge(text: str, out_path: Path, *, style: str = "pill", fill: str = "#d4713a",
          text_color: str = "#ffffff", font_size: int = 96,
          padding: int = 28) -> Path:
    """A price/offer badge. style: 'pill' | 'rect' | 'starburst' | 'circle'."""
    from PIL import Image, ImageDraw

    if style not in ("pill", "rect", "starburst", "circle"):
        raise OverlayError(
            f"badge style {style!r} is unknown — valid: pill, rect, starburst, circle"
        )
    out_path = Path(out_path)
    shaped = shape(text)
    font = _load_font(text, font_size)

    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    tb = probe.textbbox((0, 0), shaped, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]

    if style in ("pill", "rect"):
        w, h = tw + 2 * padding, th + 2 * padding
    else:
        side = max(tw, th) + 2 * padding
        w = h = side
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # rounded_rectangle paints its bottom/right coordinate INCLUSIVELY — the same 1px
    # trap captions.py already paid for. Draw to w-1/h-1 or the plate overshoots the
    # measured box by exactly one pixel.
    if style == "pill":
        draw.rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=_rgba(fill))
    elif style == "rect":
        draw.rounded_rectangle([0, 0, w - 1, h - 1], radius=max(2, padding // 3),
                               fill=_rgba(fill))
    elif style == "circle":
        draw.ellipse([0, 0, w - 1, h - 1], fill=_rgba(fill))
    else:  # starburst
        cx = cy = side / 2
        r_out, r_in = side / 2, side * 0.38
        points = []
        spikes = 12
        for i in range(spikes * 2):
            r = r_out if i % 2 == 0 else r_in
            ang = math.pi * i / spikes - math.pi / 2
            points.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
        draw.polygon(points, fill=_rgba(fill))

    tx = (w - tw) / 2 - tb[0]
    ty = (h - th) / 2 - tb[1]
    draw.text((tx, ty), shaped, font=font, fill=_rgba(text_color))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, optimize=True)
    return out_path


def arrow(out_path: Path, *, direction: str = "down", length: int = 240,
          thickness: int = 28, color: str = "#ffffff",
          style: str = "straight") -> Path:
    """A pointer. style: 'straight' | 'curved'."""
    from PIL import Image, ImageDraw

    if direction not in ("down", "up", "left", "right"):
        raise OverlayError(
            f"arrow direction {direction!r} is unknown — valid: down, up, left, right"
        )
    if style not in ("straight", "curved"):
        raise OverlayError(
            f"arrow style {style!r} is unknown — valid: straight, curved"
        )
    out_path = Path(out_path)
    rgba = _rgba(color)
    head_w = thickness * 2.4

    if style == "straight":
        head_h = max(thickness * 1.8, length * 0.25)
        w = int(math.ceil(head_w))
        img = Image.new("RGBA", (w, length), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        cx = w / 2
        shaft_bottom = length - head_h
        draw.rectangle([cx - thickness / 2, 0, cx + thickness / 2 - 1, shaft_bottom],
                       fill=rgba)
        draw.polygon([(cx - head_w / 2, shaft_bottom), (cx + head_w / 2, shaft_bottom),
                      (cx, length - 1)], fill=rgba)
    else:  # curved: a quarter-arc sweeping to point down at its end
        img = Image.new("RGBA", (length, length), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        pad = head_w / 2
        bbox = [pad, pad, length - pad, length - pad]
        draw.arc(bbox, start=180, end=270, fill=rgba, width=thickness)
        # Arrowhead at the arc's end (top of the bbox), tangent pointing right.
        tip_x, tip_y = (bbox[0] + bbox[2]) / 2, pad
        draw.polygon([(tip_x - head_w * 0.45, tip_y - head_w * 0.55),
                      (tip_x - head_w * 0.45, tip_y + head_w * 0.55),
                      (tip_x + head_w * 0.55, tip_y)], fill=rgba)

    # Base drawing points... for 'curved' the head points right; rotate into place.
    rotates = {"down": 0, "left": 90, "up": 180, "right": 270} if style == "straight" \
        else {"right": 0, "down": 90, "left": 180, "up": 270}
    angle = rotates[direction]
    if angle:
        img = img.rotate(-angle, expand=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, optimize=True)
    return out_path


_LT_TITLE_SIZE = 44
_LT_SUB_SIZE = 30
_LT_PADDING = 24
_LT_LINE_GAP = 6
_LT_PARA_GAP = 12


def _wrap_logical(text: str, font, max_width: int, draw) -> list[str]:
    """Greedy word wrap on the LOGICAL string. Wrap before reshaping, never after:
    reshaping and the bidi algorithm operate on a complete visual line; reshape first
    and split on spaces afterwards and you cut ligatures and reverse the reading order
    of the fragments."""
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


def lower_third_plate(title: str, subtitle: str, out_path: Path, *,
                      width: int = 900, fill: str = "#000000",
                      opacity: float = 0.72) -> Path:
    """A name plate for a testimonial ad: bold title over a wrapped subtitle on a
    semi-opaque rounded plate."""
    from PIL import Image, ImageDraw

    out_path = Path(out_path)
    title_font = _load_font(title, _LT_TITLE_SIZE)
    sub_font = _load_font(subtitle, _LT_SUB_SIZE)
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    wrap_w = width - 2 * _LT_PADDING

    blocks = []
    for text, font in ((title, title_font), (subtitle, sub_font)):
        if not text:
            continue
        logical = _wrap_logical(text, font, wrap_w, probe)
        shaped = [shape(line) for line in logical]
        heights = []
        for line in shaped:
            bb = probe.textbbox((0, 0), line, font=font)
            heights.append((line, font, bb, bb[3] - bb[1]))
        blocks.append(heights)

    n_lines = sum(len(b) for b in blocks)
    text_h = (sum(h for b in blocks for _, _, _, h in b)
              + _LT_LINE_GAP * (n_lines - 1)
              + _LT_PARA_GAP * (len(blocks) - 1))
    plate_h = text_h + 2 * _LT_PADDING

    img = Image.new("RGBA", (width, plate_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    plate_fill = (*_rgba(fill)[:3], round(255 * opacity))
    # Inclusive bottom coordinate again: [.., plate_h - 1] or the plate is 1px taller
    # than the measured text + padding.
    draw.rounded_rectangle([0, 0, width - 1, plate_h - 1], radius=_LT_PADDING,
                           fill=plate_fill)

    y = _LT_PADDING
    for bi, block in enumerate(blocks):
        for line, font, bb, lh in block:
            lw = bb[2] - bb[0]
            x = (width - lw) / 2 - bb[0]
            draw.text((x, y - bb[1]), line, font=font, fill=(255, 255, 255, 255))
            y += lh + _LT_LINE_GAP
        if bi < len(blocks) - 1:
            y += _LT_PARA_GAP - _LT_LINE_GAP

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, optimize=True)
    return out_path


def progress_bar(out_path: Path, *, width: int, height: int = 12,
                 color: str = "#ffffff", track: str = "#ffffff33") -> Path:
    """A full-width bar asset: translucent track with the fill bar on top. Animate its
    `scale`/crop via a track to fill over time."""
    from PIL import Image, ImageDraw

    out_path = Path(out_path)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=height // 2,
                           fill=_rgba(track))
    draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=height // 2,
                           fill=_rgba(color))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, optimize=True)
    return out_path
