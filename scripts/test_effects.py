#!/usr/bin/env python3
"""Tests for effects.py.

Stdlib only and fully offline. ffmpeg-backed tests synthesise their own media
via make_fixtures.py (Python, never shell — zsh once concatenated colour args)
and skip when ffmpeg/ffprobe are absent. Run with
`python3 -m unittest discover -s scripts -p 'test_*.py'`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import effects as ef
from make_fixtures import make as make_clips


HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

TIMED = ("flash", "zoom_punch", "shake", "glitch", "whip_blur", "vignette_pulse", "freeze_frame")
ALL_NAMES = (
    "flash", "zoom_punch", "shake", "glitch", "whip_blur",
    "ken_burns", "speed_ramp", "vignette_pulse", "color_pop", "freeze_frame",
)


def _params(name: str, *, w: int = 384, h: int = 384, dur: float = 3.0) -> dict:
    """Minimal kwargs that satisfy each builder."""
    if name == "zoom_punch":
        return {"width": w, "height": h, "at": 0.2, "duration": 0.4}
    if name == "shake":
        return {"width": w, "height": h, "at": 0.2, "duration": 0.5}
    if name == "ken_burns":
        return {"direction": "in", "duration": dur, "width": w, "height": h}
    if name == "speed_ramp":
        return {"segments": [(0.0, dur, 1.0)]}
    if name == "freeze_frame":
        return {"at": 0.5, "duration": 0.2}
    if name == "flash":
        return {"at": 0.3, "duration": 0.12}
    if name == "glitch":
        return {"at": 0.3, "duration": 0.25}
    if name == "whip_blur":
        return {"at": 0.3, "duration": 0.2}
    if name == "vignette_pulse":
        return {"at": 0.3, "duration": 0.6}
    if name == "color_pop":
        return {}
    return {}


def _build(name: str, **over) -> ef.Effect:
    size = {}
    if "w" in over:
        size["w"] = over.pop("w")
    if "h" in over:
        size["h"] = over.pop("h")
    if "dur" in over:
        size["dur"] = over.pop("dur")
    kw = _params(name, **size)
    kw.update(over)
    return ef.build(name, **kw)


def _probe_size(path: Path) -> tuple[int, int] | None:
    if shutil.which("ffprobe") is None:
        return None
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
        capture_output=True, text=True,
    )
    try:
        w, h = result.stdout.strip().splitlines()[0].split("x")
        return int(w), int(h)
    except (ValueError, IndexError):
        return None


def _grab_rgb(path: Path, t: float, w: int, h: int, dest: Path) -> bytes:
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", str(t), "-i", str(path), "-vframes", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", str(dest)],
        check=True, capture_output=True, text=True,
    )
    data = dest.read_bytes()
    expected = w * h * 3
    if len(data) != expected:
        raise AssertionError(f"raw frame {dest} is {len(data)} bytes, want {expected}")
    return data


def _luma_mean(rgb: bytes) -> float:
    n = len(rgb) // 3
    acc = 0.0
    for i in range(0, len(rgb), 3):
        acc += 0.299 * rgb[i] + 0.587 * rgb[i + 1] + 0.114 * rgb[i + 2]
    return acc / n


def _luma_var(rgb: bytes) -> float:
    n = len(rgb) // 3
    mean = _luma_mean(rgb)
    acc = 0.0
    for i in range(0, len(rgb), 3):
        y = 0.299 * rgb[i] + 0.587 * rgb[i + 1] + 0.114 * rgb[i + 2]
        acc += (y - mean) ** 2
    return acc / n


def _region_luma(rgb: bytes, w: int, h: int, x0: int, y0: int, x1: int, y1: int) -> float:
    acc = 0.0
    n = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            i = (y * w + x) * 3
            acc += 0.299 * rgb[i] + 0.587 * rgb[i + 1] + 0.114 * rgb[i + 2]
            n += 1
    return acc / n


# ─── builders: shape ─────────────────────────────────────────

class TestBuildersReturnEffect(unittest.TestCase):
    def test_every_builder_returns_effect_with_filters_and_requires(self):
        for name in ALL_NAMES:
            with self.subTest(name=name):
                effect = ef.EFFECTS[name](**_params(name))
                self.assertIsInstance(effect, ef.Effect)
                self.assertEqual(effect.name, name)
                self.assertTrue(effect.filters, f"{name} emitted an empty filters string")
                self.assertTrue(effect.requires, f"{name} declared no required filters")

    def test_effects_registry_covers_every_builder(self):
        self.assertEqual(set(ef.EFFECTS), set(ALL_NAMES))

    def test_build_dispatches_to_the_named_builder(self):
        effect = _build("flash", color="white", at=0.1, duration=0.12)
        self.assertEqual(effect.name, "flash")
        self.assertIn("eq=", effect.filters)


# ─── timed enable= ───────────────────────────────────────────

class TestTimedEnable(unittest.TestCase):
    def test_every_timed_effect_emits_enable_between_with_the_right_numbers(self):
        at, duration = 0.4, 0.25
        for name in TIMED:
            with self.subTest(name=name):
                kw = _params(name)
                kw["at"] = at
                kw["duration"] = duration
                effect = ef.EFFECTS[name](**kw)
                token = f"enable='between(t,{at:g},{at + duration:g})'"
                self.assertIn(
                    "enable='between(t,",
                    effect.filters,
                    f"{name} is timed but has no enable='between(t,'",
                )
                # whip_blur steps the window; at least one enable must name the
                # overall start, and the last step must land on at+duration.
                self.assertIn(f"between(t,{at:g},", effect.filters.replace("between(t,0.4,", "between(t,0.4,"))
                self.assertIn(f",{at + duration:g})", effect.filters)

    def test_color_pop_with_no_window_emits_no_enable(self):
        effect = ef.color_pop(saturation=1.3, contrast=1.1)
        self.assertNotIn("enable=", effect.filters)

    def test_color_pop_with_window_emits_enable(self):
        effect = ef.color_pop(at=0.2, duration=0.8)
        self.assertIn("enable='between(t,0.2,1)'", effect.filters)

    def test_ken_burns_does_not_take_at_and_emits_no_enable(self):
        effect = ef.ken_burns(direction="in", duration=3, width=384, height=384)
        self.assertNotIn("enable=", effect.filters)


# ─── zoompan order & jitter fix ──────────────────────────────

class TestZoompanOrder(unittest.TestCase):
    def test_zoom_punch_scale_before_zoompan_before_format(self):
        effect = ef.zoom_punch(width=384, height=384, at=0.0, duration=0.4)
        f = effect.filters
        i_scale = f.find("scale=")
        i_zp = f.find("zoompan")
        i_fmt = f.find("format=yuv420p")
        self.assertGreaterEqual(i_scale, 0)
        self.assertGreater(i_zp, i_scale)
        self.assertGreater(i_fmt, i_zp)
        # 384*4 = 1536, capped below 8000
        self.assertIn("scale=1536:-2", f)
        self.assertNotIn("scale=8000", f)

    def test_ken_burns_scale_before_zoompan_before_format(self):
        effect = ef.ken_burns(direction="in", duration=3, width=384, height=384)
        f = effect.filters
        i_scale = f.find("scale=")
        i_zp = f.find("zoompan")
        i_fmt = f.find("format=yuv420p")
        self.assertGreater(i_zp, i_scale)
        self.assertGreater(i_fmt, i_zp)
        self.assertIn("scale=1536:-2", f)

    def test_four_k_upscale_caps_at_8000(self):
        effect = ef.zoom_punch(width=3840, height=2160, at=0.0, duration=0.4)
        i_scale = effect.filters.find("scale=")
        i_zp = effect.filters.find("zoompan")
        self.assertIn("scale=8000:-2", effect.filters)
        self.assertLess(i_scale, i_zp)

    def test_hd_upscale_is_width_times_four(self):
        effect = ef.ken_burns(direction="in", duration=2, width=1920, height=1080)
        # min(8000, 1920*4) = 7680
        self.assertIn("scale=7680:-2", effect.filters)

    def test_zoom_punch_uses_round_not_truncation_for_frame_count(self):
        # 0.15s * 25fps = 3.75 → int()=3, round()=4. The cosine period is N.
        effect = ef.zoom_punch(
            width=320, height=320, at=0.0, duration=0.15, fps=25, scale=1.2,
        )
        self.assertIn("/4)", effect.filters)  # (on-0)/N with N=4
        self.assertNotIn("/3)", effect.filters)

    def test_zoom_punch_cosine_return_curve(self):
        effect = ef.zoom_punch(width=384, height=384)
        self.assertIn("1-cos(2*PI", effect.filters)
        self.assertIn("d=1", effect.filters)

    def test_ken_burns_cosine_ease(self):
        effect = ef.ken_burns(direction="in", duration=3, width=384, height=384)
        self.assertIn("1-cos(PI", effect.filters)


# ─── shake ───────────────────────────────────────────────────

class TestShake(unittest.TestCase):
    def test_uses_sin_never_random(self):
        effect = ef.shake(width=384, height=384)
        self.assertIn("sin(", effect.filters)
        self.assertNotIn("random(", effect.filters)

    def test_margin_exceeds_amplitude(self):
        # margin = ceil(12)*2+2 = 26; scaled = 384+52 = 436, even.
        effect = ef.shake(width=384, height=384, amplitude=12)
        self.assertIn("scale=436:436", effect.filters)
        self.assertIn("crop=w=384:h=384", effect.filters)

    def test_two_incommensurate_frequencies(self):
        effect = ef.shake(width=384, height=384, frequency=14)
        self.assertIn("1.618", effect.filters)


# ─── chain() ─────────────────────────────────────────────────

class TestChain(unittest.TestCase):
    def test_empty_returns_empty_string(self):
        self.assertEqual(ef.chain([]), "")

    def test_joins_with_commas(self):
        a = ef.color_pop()
        b = ef.flash(at=0.1, duration=0.12)
        joined = ef.chain([a, b])
        self.assertIn(",", joined)
        self.assertTrue(joined.startswith(a.filters) or a.filters.split(",")[0] in joined)

    def test_collapses_duplicate_format_yuv420p_to_one_at_the_end(self):
        a = ef.zoom_punch(width=384, height=384)
        b = ef.ken_burns(direction="in", duration=2, width=384, height=384)
        self.assertGreaterEqual(a.filters.count("format=yuv420p"), 1)
        self.assertGreaterEqual(b.filters.count("format=yuv420p"), 1)
        joined = ef.chain([a, b])
        self.assertEqual(joined.count("format=yuv420p"), 1)
        self.assertTrue(joined.endswith("format=yuv420p"))

    def test_chain_of_one_is_the_fragment(self):
        effect = ef.color_pop()
        self.assertEqual(ef.chain([effect]), effect.filters)


# ─── notes ───────────────────────────────────────────────────

class TestNotes(unittest.TestCase):
    def test_speed_ramp_notes_mention_remeasure_and_audio(self):
        effect = ef.speed_ramp(segments=[(0.0, 3.0, 2.0)])
        self.assertTrue(effect.notes)
        lower = effect.notes.lower()
        self.assertIn("re-measure", lower)
        self.assertIn("audio", lower)

    def test_freeze_frame_notes_mention_lengthens_and_remeasure(self):
        effect = ef.freeze_frame(at=1.0, duration=0.5)
        lower = effect.notes.lower()
        self.assertIn("lengthen", lower)
        self.assertIn("re-measure", lower)


# ─── available / unavailable partition ───────────────────────

class TestAvailabilityPartition(unittest.TestCase):
    def test_available_and_unavailable_partition_effects_exactly(self):
        avail = set(ef.available())
        unavail = set(ef.unavailable())
        self.assertEqual(avail | unavail, set(ef.EFFECTS))
        self.assertTrue(avail.isdisjoint(unavail))

    def test_available_is_sorted(self):
        names = ef.available()
        self.assertEqual(names, sorted(names))

    def test_ffmpeg_absent_available_empty_unavailable_all(self):
        with mock.patch.object(ef, "probe_filters", return_value=frozenset()):
            self.assertEqual(ef.available(), [])
            unavail = ef.unavailable()
            self.assertEqual(set(unavail), set(ef.EFFECTS))
            for name, missing in unavail.items():
                self.assertTrue(missing, name)


class TestProbeFilters(unittest.TestCase):
    def test_returns_frozenset(self):
        result = ef.probe_filters()
        self.assertIsInstance(result, frozenset)

    def test_empty_when_ffmpeg_absent(self):
        # probe_filters is lru_cached; patch shutil.which AND clear the cache.
        ef.probe_filters.cache_clear()
        try:
            with mock.patch.object(ef.shutil, "which", return_value=None):
                self.assertEqual(ef.probe_filters(), frozenset())
        finally:
            ef.probe_filters.cache_clear()


# ─── sad path ────────────────────────────────────────────────

class TestSadPath(unittest.TestCase):
    def test_unknown_effect_name_contains_close_match(self):
        with self.assertRaises(ef.EffectError) as ctx:
            ef.build("flsh")
        msg = str(ctx.exception)
        self.assertIn("flash", msg)
        self.assertIn("flsh", msg)
        self.assertIsInstance(ctx.exception, ef.EffectError)
        self.assertNotIsInstance(ctx.exception, ValueError)

    def test_unknown_name_with_no_close_match_lists_known(self):
        with self.assertRaises(ef.EffectError) as ctx:
            ef.build("definitely-not-an-effect")
        self.assertIn("unknown effect", str(ctx.exception))

    def test_negative_at_names_the_param(self):
        with self.assertRaises(ef.EffectError) as ctx:
            ef.flash(at=-0.2, duration=0.12)
        msg = str(ctx.exception)
        self.assertIn("at=", msg)
        self.assertIn("-0.2", msg)

    def test_zero_duration_raises(self):
        with self.assertRaises(ef.EffectError) as ctx:
            ef.glitch(at=0.0, duration=0.0)
        self.assertIn("duration=", str(ctx.exception))

    def test_negative_duration_raises(self):
        with self.assertRaises(ef.EffectError) as ctx:
            ef.whip_blur(at=0.1, duration=-0.2)
        self.assertIn("duration=", str(ctx.exception))

    def test_speed_ramp_overlapping_segments_names_both(self):
        with self.assertRaises(ef.EffectError) as ctx:
            ef.speed_ramp(segments=[(0.0, 2.0, 2.0), (1.0, 3.0, 0.5)])
        msg = str(ctx.exception)
        self.assertIn("overlap", msg)
        self.assertIn("0", msg)
        self.assertIn("2", msg)
        self.assertIn("1", msg)
        self.assertIn("3", msg)

    def test_speed_ramp_unsorted_names_both(self):
        with self.assertRaises(ef.EffectError) as ctx:
            ef.speed_ramp(segments=[(2.0, 3.0, 1.0), (0.0, 1.0, 2.0)])
        msg = str(ctx.exception)
        self.assertIn("unsorted", msg)
        self.assertIn("2", msg)
        self.assertIn("0", msg)

    def test_speed_ramp_factor_zero(self):
        with self.assertRaises(ef.EffectError) as ctx:
            ef.speed_ramp(segments=[(0.0, 1.0, 0.0)])
        self.assertIn("factor=0", str(ctx.exception))

    def test_speed_ramp_factor_negative(self):
        with self.assertRaises(ef.EffectError) as ctx:
            ef.speed_ramp(segments=[(0.0, 1.0, -2.0)])
        self.assertIn("factor=-2", str(ctx.exception))

    def test_speed_ramp_empty_segments(self):
        with self.assertRaises(ef.EffectError) as ctx:
            ef.speed_ramp(segments=[])
        self.assertIn("empty", str(ctx.exception).lower())

    def test_bad_ken_burns_direction_lists_valid(self):
        with self.assertRaises(ef.EffectError) as ctx:
            ef.ken_burns(direction="diagonal", duration=2, width=64, height=64)
        msg = str(ctx.exception)
        self.assertIn("diagonal", msg)
        for valid in ef.KEN_BURNS_DIRECTIONS:
            self.assertIn(valid, msg)

    def test_bad_whip_blur_direction_lists_valid(self):
        with self.assertRaises(ef.EffectError) as ctx:
            ef.whip_blur(direction="spinwise")
        msg = str(ctx.exception)
        self.assertIn("spinwise", msg)
        self.assertIn("horizontal", msg)
        self.assertIn("vertical", msg)

    def test_bad_colour_string(self):
        with self.assertRaises(ef.EffectError) as ctx:
            ef.flash(color="darkslateblue6")
        msg = str(ctx.exception)
        self.assertIn("darkslateblue6", msg)
        self.assertIn("color=", msg)

    def test_unquoted_looking_hex_without_0x_rejected(self):
        with self.assertRaises(ef.EffectError):
            ef.flash(color="2a3d45")

    def test_hex_colour_accepted(self):
        effect = ef.flash(color="0x2a3d45")
        self.assertEqual(effect.name, "flash")
        effect2 = ef.flash(color="#FFFFFF")
        self.assertEqual(effect2.name, "flash")

    def test_shake_amplitude_zero_is_effecterror_not_noop(self):
        """amplitude=0 raises EffectError; we refuse a no-op crop rather than emit one."""
        with self.assertRaises(ef.EffectError) as ctx:
            ef.shake(width=384, height=384, amplitude=0)
        self.assertIn("amplitude=0", str(ctx.exception))

    def test_glitch_intensity_out_of_range(self):
        with self.assertRaises(ef.EffectError) as ctx:
            ef.glitch(intensity=1.4)
        self.assertIn("intensity=1.4", str(ctx.exception))
        self.assertIn("0..1", str(ctx.exception))

    def test_color_pop_half_window_raises(self):
        with self.assertRaises(ef.EffectError) as ctx:
            ef.color_pop(at=0.2, duration=None)
        self.assertIn("at=", str(ctx.exception))

    def test_zoom_punch_scale_one_raises(self):
        with self.assertRaises(ef.EffectError) as ctx:
            ef.zoom_punch(scale=1.0, width=64, height=64)
        self.assertIn("scale=1", str(ctx.exception))

    def test_missing_rgbashift_puts_glitch_in_unavailable_and_build_raises(self):
        present = frozenset({
            "eq", "scale", "zoompan", "format", "crop", "gblur",
            "vignette", "setpts", "loop", "noise",
        })
        with mock.patch.object(ef, "probe_filters", return_value=present):
            unavail = ef.unavailable()
            self.assertIn("glitch", unavail)
            self.assertIn("rgbashift", unavail["glitch"])
            self.assertNotIn("flash", unavail)
            with self.assertRaises(ef.EffectError) as ctx:
                ef.build("glitch")
            self.assertIn("rgbashift", str(ctx.exception))
            self.assertIn("glitch", str(ctx.exception))

    def test_build_still_works_when_ffmpeg_absent(self):
        with mock.patch.object(ef, "probe_filters", return_value=frozenset()):
            effect = ef.build("flash", at=0.0, duration=0.12)
            self.assertIsInstance(effect, ef.Effect)
            self.assertIn("ffmpeg absent", effect.notes.lower())
            self.assertTrue(effect.filters)

    def test_sad_paths_raise_effecterror_not_valueerror(self):
        cases = [
            lambda: ef.build("nope"),
            lambda: ef.flash(at=-1),
            lambda: ef.glitch(duration=0),
            lambda: ef.speed_ramp(segments=[(0, 1, 0)]),
            lambda: ef.ken_burns(direction="sideways", duration=1, width=8, height=8),
            lambda: ef.flash(color="not-a-colour"),
            lambda: ef.shake(width=8, height=8, amplitude=0),
        ]
        for fn in cases:
            with self.subTest(fn=fn):
                with self.assertRaises(ef.EffectError):
                    fn()


# ─── ken_burns directions ────────────────────────────────────

class TestKenBurnsDirections(unittest.TestCase):
    def test_every_valid_direction_builds(self):
        for direction in ef.KEN_BURNS_DIRECTIONS:
            with self.subTest(direction=direction):
                effect = ef.ken_burns(
                    direction=direction, duration=2, width=128, height=128,
                )
                self.assertIn("zoompan", effect.filters)
                self.assertIn("format=yuv420p", effect.filters)

    def test_out_starts_at_zoom_to(self):
        effect = ef.ken_burns(
            direction="out", zoom_from=1.0, zoom_to=1.2, duration=2, width=64, height=64,
        )
        # z = zoom_to + (zoom_from - zoom_to)*ease → starts at 1.2
        self.assertIn("1.2", effect.filters)


# ─── freeze / speed expressions ──────────────────────────────

class TestTimeEffects(unittest.TestCase):
    def test_freeze_loop_count_uses_round(self):
        effect = ef.freeze_frame(at=1.0, duration=0.5, fps=30)
        # 0.5*30 = 15
        self.assertIn("loop=loop=15", effect.filters)
        self.assertIn("start=30", effect.filters)

    def test_speed_ramp_whole_clip_factor_two_is_divide_pts(self):
        effect = ef.speed_ramp(segments=[(0.0, 3.0, 2.0)])
        self.assertIn("setpts=", effect.filters)
        self.assertIn("/TB", effect.filters)
        self.assertIn("/2", effect.filters)


# ─── apply_to / preview without ffmpeg ───────────────────────

class TestNoFfmpegRender(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_to_raises_install_message_when_ffmpeg_absent(self):
        clip = self.dir / "clip.mp4"
        clip.write_bytes(b"fake")
        with mock.patch.object(ef.shutil, "which", return_value=None):
            with self.assertRaises(ef.EffectError) as ctx:
                ef.apply_to(clip, [ef.color_pop()], self.dir / "out.mp4")
        msg = str(ctx.exception)
        self.assertIn("ffmpeg not found", msg)
        self.assertIn("brew install ffmpeg", msg)

    def test_preview_raises_install_message_when_ffmpeg_absent(self):
        with mock.patch.object(ef.shutil, "which", return_value=None):
            with self.assertRaises(ef.EffectError) as ctx:
                ef.preview(ef.color_pop(), self.dir / "out.mp4")
        self.assertIn("ffmpeg not found", str(ctx.exception))

    def test_apply_to_missing_clip_names_the_path(self):
        missing = self.dir / "nope.mp4"
        with self.assertRaises(ef.EffectError) as ctx:
            ef.apply_to(missing, [ef.color_pop()], self.dir / "out.mp4")
        self.assertIn("nope.mp4", str(ctx.exception))

    def test_duration_of_none_when_ffprobe_absent(self):
        ghost = self.dir / "ghost.mp4"
        ghost.write_bytes(b"not a video")
        with mock.patch.object(ef.shutil, "which", return_value=None):
            self.assertIsNone(ef.duration_of(ghost))


# ─── ffmpeg integration ──────────────────────────────────────

@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TestFfmpegRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        clips = make_clips(cls.dir / "fx", n=1, seconds=3, w=384, h=384, fps=30, audio=True)
        cls.clip = clips[0]
        cls.clip_dur = ef.duration_of(cls.clip)
        cls.clip_size = _probe_size(cls.clip)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_render_every_available_effect_exits_zero(self):
        names = ef.available()
        self.assertTrue(names, "available() was empty despite ffmpeg being present")
        rendered = 0
        for name in names:
            with self.subTest(name=name):
                effect = _build(name, w=384, h=384, dur=3.0)
                out = self.dir / f"sweep-{name}.mp4"
                result = ef.apply_to(self.clip, [effect], out)
                self.assertEqual(result, out)
                self.assertTrue(out.is_file(), f"{name} produced no file")
                self.assertGreater(out.stat().st_size, 1000)
                self.assertIsNotNone(ef.duration_of(out))
                rendered += 1
        # Keep a handle for the verdict: every available effect rendered.
        self.assertEqual(rendered, len(names))

    def test_zoom_punch_output_matches_input_dimensions(self):
        effect = ef.zoom_punch(width=384, height=384, at=0.2, duration=0.4)
        out = self.dir / "punch-size.mp4"
        ef.apply_to(self.clip, [effect], out)
        self.assertEqual(_probe_size(out), self.clip_size)
        self.assertEqual(_probe_size(out), (384, 384))

    def test_ken_burns_output_duration_matches_request(self):
        effect = ef.ken_burns(
            direction="in", duration=3.0, width=384, height=384, fps=30,
        )
        out = self.dir / "kb-dur.mp4"
        ef.apply_to(self.clip, [effect], out)
        dur = ef.duration_of(out)
        self.assertIsNotNone(dur)
        self.assertAlmostEqual(dur, 3.0, delta=0.1)

    def test_freeze_frame_output_is_longer_by_duration(self):
        hold = 0.5
        effect = ef.freeze_frame(at=1.0, duration=hold, fps=30)
        out = self.dir / "freeze.mp4"
        ef.apply_to(self.clip, [effect], out)
        dur = ef.duration_of(out)
        self.assertIsNotNone(dur)
        self.assertAlmostEqual(dur, self.clip_dur + hold, delta=0.15)

    def test_speed_ramp_factor_two_halves_duration(self):
        effect = ef.speed_ramp(segments=[(0.0, 3.0, 2.0)])
        out = self.dir / "speed2.mp4"
        ef.apply_to(self.clip, [effect], out)
        dur = ef.duration_of(out)
        self.assertIsNotNone(dur)
        self.assertAlmostEqual(dur, self.clip_dur / 2, delta=0.1)

    def test_two_effect_chain_renders(self):
        a = ef.color_pop(saturation=1.2, contrast=1.05)
        b = ef.flash(at=1.0, duration=0.12)
        out = self.dir / "chain-two.mp4"
        ef.apply_to(self.clip, [a, b], out)
        self.assertTrue(out.is_file())
        self.assertGreater(out.stat().st_size, 1000)
        self.assertIsNotNone(ef.duration_of(out))

    def test_preview_produces_playable_file_with_no_input_clip(self):
        out = self.dir / "preview.mp4"
        result = ef.preview(ef.flash(at=0.3, duration=0.12), out, seconds=1.0)
        self.assertEqual(result, out)
        self.assertTrue(out.is_file())
        dur = ef.duration_of(out)
        self.assertIsNotNone(dur)
        self.assertGreater(dur, 0.3)
        self.assertEqual(_probe_size(out), (384, 384))

    def test_empty_effects_stream_copies(self):
        out = self.dir / "copied.mp4"
        ef.apply_to(self.clip, [], out)
        self.assertTrue(out.is_file())
        self.assertAlmostEqual(ef.duration_of(out), self.clip_dur, delta=0.05)
        self.assertEqual(_probe_size(out), self.clip_size)

    def test_apply_to_window_past_clip_end_raises_naming_both_numbers(self):
        effect = ef.flash(at=10.0, duration=1.0)
        with self.assertRaises(ef.EffectError) as ctx:
            ef.apply_to(self.clip, [effect], self.dir / "past.mp4")
        msg = str(ctx.exception)
        self.assertIn("11", msg)          # at+duration
        self.assertIn(f"{self.clip_dur:g}", msg)  # clip length


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TestPixelAssertions(unittest.TestCase):
    """A chain that runs but does nothing visible is the most likely failure.

    Fixtures are solid-colour clips from make_fixtures.py so the measurements
    are about the effect, not about a test pattern's own edges.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        clips = make_clips(cls.dir / "pix", n=1, seconds=3, w=384, h=384, fps=30, audio=False)
        cls.clip = clips[0]
        cls.w, cls.h = 384, 384

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _frame(self, path: Path, t: float, label: str) -> bytes:
        return _grab_rgb(path, t, self.w, self.h, self.dir / f"{label}.raw")

    def test_flash_mean_brightness_higher_inside_window_than_outside(self):
        at, duration = 1.0, 0.12
        effect = ef.flash(color="white", at=at, duration=duration, peak=0.85)
        out = self.dir / "flash.mp4"
        ef.apply_to(self.clip, [effect], out)
        before = _luma_mean(self._frame(out, 0.5, "flash-before"))
        peak = _luma_mean(self._frame(out, at + duration / 2, "flash-peak"))
        after = _luma_mean(self._frame(out, 1.8, "flash-after"))
        self.assertGreater(
            peak, before + 40,
            f"flash peak luma {peak:.1f} is not much brighter than before {before:.1f}",
        )
        self.assertGreater(peak, after + 40)
        self.assertAlmostEqual(before, after, delta=8)

    def test_glitch_channel_divergence_inside_window(self):
        at, duration = 1.0, 0.25
        effect = ef.glitch(at=at, duration=duration, intensity=0.8)
        out = self.dir / "glitch.mp4"
        ef.apply_to(self.clip, [effect], out)
        # Solid colour + noise: variance is ~0 outside the window and jumps
        # inside it. rgbashift of a uniform field only shows at the edges;
        # noise is what makes channels diverge frame-wide.
        before = _luma_var(self._frame(out, 0.4, "glitch-before"))
        inside = _luma_var(self._frame(out, at + duration / 2, "glitch-in"))
        after = _luma_var(self._frame(out, 2.0, "glitch-after"))
        self.assertGreater(
            inside, 30,
            f"glitch variance inside window is {inside:.1f}, expected noise",
        )
        self.assertLess(before, 10)
        self.assertLess(after, 10)
        self.assertGreater(inside, before * 5 if before > 0 else 30)

    def test_vignette_pulse_corners_darker_than_centre(self):
        at, duration = 1.0, 0.6
        effect = ef.vignette_pulse(at=at, duration=duration, strength=0.4)
        out = self.dir / "vignette.mp4"
        ef.apply_to(self.clip, [effect], out)
        peak = self._frame(out, at + duration / 2, "vig-peak")
        quiet = self._frame(out, 0.3, "vig-quiet")
        centre_p = _region_luma(peak, self.w, self.h, 162, 162, 222, 222)
        corner_p = _region_luma(peak, self.w, self.h, 0, 0, 48, 48)
        centre_q = _region_luma(quiet, self.w, self.h, 162, 162, 222, 222)
        corner_q = _region_luma(quiet, self.w, self.h, 0, 0, 48, 48)
        self.assertLess(
            corner_p, centre_p * 0.4,
            f"vignette peak corner {corner_p:.1f} is not much darker than centre {centre_p:.1f}",
        )
        self.assertAlmostEqual(
            centre_q, corner_q, delta=8,
            msg="outside the window a solid clip should not be vignetted",
        )


# ─── whip_blur / flash extras ────────────────────────────────

class TestWhipAndFlash(unittest.TestCase):
    def test_whip_horizontal_uses_sigmav_zero(self):
        effect = ef.whip_blur(direction="horizontal", sigma=24)
        self.assertIn("sigmaV=0", effect.filters)
        self.assertIn("gblur=", effect.filters)
        self.assertGreaterEqual(effect.filters.count("gblur="), 3)

    def test_whip_vertical_uses_vertical_sigma(self):
        effect = ef.whip_blur(direction="vertical", sigma=18)
        self.assertIn("sigmaV=", effect.filters)
        self.assertIn("sigma=0.01", effect.filters)

    def test_flash_eval_frame(self):
        effect = ef.flash()
        self.assertIn("eval=frame", effect.filters)
        self.assertIn("brightness=", effect.filters)

    def test_glitch_requires_rgbashift_and_noise(self):
        effect = ef.glitch()
        self.assertEqual(effect.requires, ("rgbashift", "noise"))
        self.assertIn("rgbashift=", effect.filters)
        self.assertIn("noise=", effect.filters)
        self.assertNotIn("random(", effect.filters)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TestDurationIsPreserved(unittest.TestCase):
    """An effect that is not supposed to change the length must not change the length.

    zoompan's `fps` option RE-TIMES rather than resamples: hand it fps=30 for 24fps
    footage and it replays the same 158 frames at 30, so a 6.583s clip comes back
    5.267s — exactly frames/30. Nothing errors and the file is valid, so the loss only
    surfaces much later as a film whose video stream ends before its audio. It silently
    truncated the last two shots of a real ad before this check existed.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.clip = make_clips(self.dir, n=1, seconds=3, fps=24, audio=False)[0]

    def test_probe_fps_reads_the_real_rate(self):
        self.assertAlmostEqual(ef._probe_fps(self.clip), 24.0, places=3)

    def test_a_mismatched_fps_is_rejected_rather_than_silently_retiming(self):
        effect = ef.build("zoom_punch", width=384, height=384, at=0.5,
                          duration=0.5, fps=30)
        with self.assertRaises(ef.EffectError) as ctx:
            ef.apply_to(self.clip, [effect], self.dir / "out.mp4")
        message = str(ctx.exception)
        self.assertIn("duration", message)
        self.assertIn("fps", message)

    def test_the_clips_own_fps_preserves_duration(self):
        effect = ef.build("zoom_punch", width=384, height=384, at=0.5,
                          duration=0.5, fps=24)
        out = ef.apply_to(self.clip, [effect], self.dir / "ok.mp4")
        self.assertAlmostEqual(ef.duration_of(out), ef.duration_of(self.clip), delta=0.05)

    def test_ken_burns_at_the_clip_rate_preserves_duration(self):
        effect = ef.build("ken_burns", direction="in", zoom_from=1.0, zoom_to=1.2,
                          duration=3.0, width=384, height=384, fps=24)
        out = ef.apply_to(self.clip, [effect], self.dir / "kb.mp4")
        self.assertAlmostEqual(ef.duration_of(out), ef.duration_of(self.clip), delta=0.05)

    def test_effects_that_may_change_duration_are_still_allowed(self):
        effect = ef.build("freeze_frame", at=1.0, duration=0.5)
        out = ef.apply_to(self.clip, [effect], self.dir / "fz.mp4")
        self.assertGreater(ef.duration_of(out), ef.duration_of(self.clip))


if __name__ == "__main__":
    unittest.main()
