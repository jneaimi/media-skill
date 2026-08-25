#!/usr/bin/env python3
"""Tests for overlays.py.

Stdlib + Pillow, offline. Run with plain `python3 scripts/test_overlays.py`.
Pure tests (easing, sampling, positioning, validation, filter strings) need no ffmpeg;
preset render tests skip without Pillow; the burn tests skip without ffmpeg and build
their clips with make_fixtures.py — in Python, never shell (zsh word-splitting on an
unquoted hex colour silently produced 320x240 black clips in a previous run of this
repo).
"""

import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  # repo root, for make_fixtures

import captions as cap
import overlays as ov
import adspec
import make_fixtures

try:
    from PIL import Image, ImageChops, ImageDraw  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

try:
    import arabic_reshaper  # noqa: F401
    import bidi  # noqa: F401
    HAVE_RESHAPER = True
except ImportError:
    HAVE_RESHAPER = False

HAVE_FFMPEG = shutil.which("ffmpeg") is not None

ARABIC = "خصم ٥٠٪"  # "50% discount"

# TikTok 1080x1920: top 10%, right 10%, bottom 20% covered by platform UI.
SAFE_TIKTOK = {
    "width": 1080, "height": 1920,
    "top": 192, "right": 108, "bottom": 384, "left": 0,
    "box": (0, 192, 972, 1536),
}


def _tiny_png_bytes() -> bytes:
    """A valid 1x1 transparent RGBA PNG, built from stdlib only."""
    def chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
    return b"\x89PNG\r\n\x1a\n" + ihdr + idat + chunk(b"IEND", b"")


def _asset(dir_path: Path, name: str = "asset.png") -> Path:
    p = dir_path / name
    p.write_bytes(_tiny_png_bytes())
    return p


def _overlay(asset: Path, **kw) -> ov.Overlay:
    kw.setdefault("start", 0.0)
    kw.setdefault("end", 1.0)
    return ov.Overlay(asset, **kw)


class TmpDirMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)


# ─── EASING ──────────────────────────────────────────────────

class TestEasings(unittest.TestCase):
    def test_endpoints_exact_for_all_easings(self):
        for name in ov.EASINGS:
            self.assertEqual(ov.ease(name, 0.0), 0.0, name)
            self.assertEqual(ov.ease(name, 1.0), 1.0, name)

    def test_ten_easings_exist(self):
        self.assertEqual(set(ov.EASINGS), {
            "linear", "ease_in", "ease_out", "ease_in_out", "back_out", "back_in",
            "bounce_out", "elastic_out", "expo_out", "cubic_in_out",
        })

    def test_back_out_overshoots(self):
        values = [ov.ease("back_out", i / 50) for i in range(1, 50)]
        self.assertGreater(max(values), 1.0)

    def test_elastic_out_overshoots(self):
        values = [ov.ease("elastic_out", i / 50) for i in range(1, 50)]
        self.assertGreater(max(values), 1.0)

    def test_back_in_dips_below_zero(self):
        values = [ov.ease("back_in", i / 50) for i in range(1, 50)]
        self.assertLess(min(values), 0.0)

    def test_bounce_out_rebounds(self):
        values = [ov.ease("bounce_out", i / 100) for i in range(1, 101)]
        self.assertTrue(any(a > b for a, b in zip(values, values[1:])))

    def test_ease_clamps_p_above_one(self):
        self.assertEqual(ov.ease("linear", 1.5), 1.0)
        self.assertEqual(ov.ease("ease_out", 2.0), 1.0)

    def test_ease_clamps_p_below_zero(self):
        self.assertEqual(ov.ease("linear", -0.5), 0.0)
        self.assertEqual(ov.ease("ease_in", -1.0), 0.0)

    def test_linear_identity(self):
        self.assertAlmostEqual(ov.ease("linear", 0.37), 0.37)

    def test_ease_in_out_midpoint(self):
        self.assertAlmostEqual(ov.ease("ease_in_out", 0.5), 0.5)
        self.assertAlmostEqual(ov.ease("cubic_in_out", 0.5), 0.5)

    def test_unknown_easing_lists_valid(self):
        with self.assertRaises(ov.OverlayError) as ctx:
            ov.ease("snappy", 0.5)
        msg = str(ctx.exception)
        self.assertIn("snappy", msg)
        for name in ov.EASINGS:
            self.assertIn(name, msg)


# ─── TRACKS & SAMPLING ───────────────────────────────────────

class TestSample(unittest.TestCase):
    def test_before_first_key_holds_first_value(self):
        track = ov.Track("scale", [ov.Keyframe(1.0, 5.0), ov.Keyframe(2.0, 9.0)],
                         easing="linear")
        self.assertEqual(ov.sample(track, 0.25), 5.0)

    def test_after_last_key_holds_last_value(self):
        track = ov.Track("scale", [ov.Keyframe(1.0, 5.0), ov.Keyframe(2.0, 9.0)],
                         easing="linear")
        self.assertEqual(ov.sample(track, 99.0), 9.0)

    def test_between_keys_linear(self):
        track = ov.Track("scale", [ov.Keyframe(1.0, 10.0), ov.Keyframe(2.0, 20.0)],
                         easing="linear")
        self.assertAlmostEqual(ov.sample(track, 1.5), 15.0)

    def test_between_keys_eased_differs_from_linear(self):
        track = ov.Track("scale", [ov.Keyframe(0.0, 0.0), ov.Keyframe(1.0, 1.0)],
                         easing="ease_in")
        self.assertLess(ov.sample(track, 0.5), 0.5)

    def test_hits_keys_exactly(self):
        track = ov.Track("opacity", [ov.Keyframe(0.0, 0.2), ov.Keyframe(1.0, 0.8)],
                         easing="back_out")
        self.assertAlmostEqual(ov.sample(track, 0.0), 0.2)
        self.assertAlmostEqual(ov.sample(track, 1.0), 0.8)

    def test_single_key_is_constant(self):
        track = ov.Track("rotation", [ov.Keyframe(3.0, 45.0)])
        self.assertEqual(ov.sample(track, 0.0), 45.0)
        self.assertEqual(ov.sample(track, 3.0), 45.0)
        self.assertEqual(ov.sample(track, 9.0), 45.0)

    def test_keys_sorted_on_construction(self):
        track = ov.Track("x", [ov.Keyframe(2.0, 1.0), ov.Keyframe(0.0, 0.0)])
        self.assertEqual([k.t for k in track.keys], [0.0, 2.0])

    def test_duplicate_t_raises(self):
        with self.assertRaises(ov.OverlayError) as ctx:
            ov.Track("scale", [ov.Keyframe(1.0, 0.0), ov.Keyframe(1.0, 1.0)])
        self.assertIn("scale", str(ctx.exception))

    def test_zero_keys_raises(self):
        with self.assertRaises(ov.OverlayError):
            ov.Track("scale", [])

    def test_unknown_prop_lists_allowed(self):
        with self.assertRaises(ov.OverlayError) as ctx:
            ov.Track("zoom", [ov.Keyframe(0.0, 1.0)])
        msg = str(ctx.exception)
        self.assertIn("zoom", msg)
        for prop in ov.PROPS:
            self.assertIn(prop, msg)

    def test_unknown_easing_lists_valid(self):
        with self.assertRaises(ov.OverlayError) as ctx:
            ov.Track("scale", [ov.Keyframe(0.0, 1.0)], easing="snappy")
        for name in ov.EASINGS:
            self.assertIn(name, str(ctx.exception))


# ─── OVERLAY VALIDATION (constructor) ────────────────────────

class TestOverlayValidation(TmpDirMixin, unittest.TestCase):
    def test_end_not_after_start_names_id_and_values(self):
        with self.assertRaises(ov.OverlayError) as ctx:
            _overlay(_asset(self.dir), id="price", start=5.0, end=2.0)
        msg = str(ctx.exception)
        self.assertIn("price", msg)
        self.assertIn("5.0", msg)
        self.assertIn("2.0", msg)

    def test_end_equal_start_raises(self):
        with self.assertRaises(ov.OverlayError):
            _overlay(_asset(self.dir), start=2.0, end=2.0)

    def test_negative_start_raises(self):
        with self.assertRaises(ov.OverlayError):
            _overlay(_asset(self.dir), start=-0.5, end=1.0)

    def test_non_numeric_start_raises(self):
        with self.assertRaises(ov.OverlayError):
            _overlay(_asset(self.dir), start=True, end=1.0)

    def test_missing_asset_names_path(self):
        missing = self.dir / "nope.png"
        with self.assertRaises(ov.OverlayError) as ctx:
            _overlay(missing)
        self.assertIn(str(missing), str(ctx.exception))

    def test_scale_zero_raises(self):
        with self.assertRaises(ov.OverlayError):
            _overlay(_asset(self.dir), scale=0.0)

    def test_scale_above_one_raises(self):
        with self.assertRaises(ov.OverlayError) as ctx:
            _overlay(_asset(self.dir), scale=1.5)
        self.assertIn("(0, 1]", str(ctx.exception))

    def test_scale_one_is_legal(self):
        _overlay(_asset(self.dir), scale=1.0)

    def test_opacity_out_of_range_raises(self):
        for bad in (-0.1, 1.01):
            with self.assertRaises(ov.OverlayError):
                _overlay(_asset(self.dir), opacity=bad)

    def test_opacity_bounds_are_legal(self):
        _overlay(_asset(self.dir), opacity=0.0)
        _overlay(_asset(self.dir), opacity=1.0)

    def test_unknown_anchor_lists_anchors(self):
        with self.assertRaises(ov.OverlayError) as ctx:
            _overlay(_asset(self.dir), anchor="middle")
        msg = str(ctx.exception)
        self.assertIn("middle", msg)
        for anchor in ov.ANCHORS:
            self.assertIn(anchor, msg)

    def test_fades_overlapping_window_raise(self):
        with self.assertRaises(ov.OverlayError) as ctx:
            _overlay(_asset(self.dir), id="cta", start=0.0, end=2.0,
                     fade_in=1.5, fade_out=1.0)
        self.assertIn("cta", str(ctx.exception))

    def test_fades_exactly_filling_window_are_legal(self):
        _overlay(_asset(self.dir), start=0.0, end=2.0, fade_in=1.0, fade_out=1.0)

    def test_negative_fade_raises(self):
        with self.assertRaises(ov.OverlayError):
            _overlay(_asset(self.dir), fade_in=-0.5)

    def test_id_defaults_to_asset_stem(self):
        self.assertEqual(_overlay(_asset(self.dir, "my-logo.png")).id, "my-logo")

    def test_explicit_id_kept(self):
        self.assertEqual(_overlay(_asset(self.dir), id="hero").id, "hero")


# ─── POSITIONING & SAFE ZONES ────────────────────────────────

class TestResolvePosition(TmpDirMixin, unittest.TestCase):
    W, H = 1080, 1920

    def test_nine_anchors_distinct(self):
        asset = _asset(self.dir)
        positions = set()
        for anchor in ov.ANCHORS:
            o = _overlay(asset, anchor=anchor)
            positions.add(ov.resolve_position(o, 0.5, frame_w=self.W, frame_h=self.H,
                                              asset_w=100, asset_h=50, safe=None))
        self.assertEqual(len(positions), 9)

    def test_anchor_values(self):
        asset = _asset(self.dir)
        o = _overlay(asset, anchor="bottom-right")
        self.assertEqual(
            ov.resolve_position(o, 0.5, frame_w=self.W, frame_h=self.H,
                                asset_w=100, asset_h=50, safe=None),
            (980, 1870),
        )

    def test_all_anchors_clamped_into_tiktok_box(self):
        asset = _asset(self.dir)
        l, t, r, b = SAFE_TIKTOK["box"]
        for anchor in ov.ANCHORS:
            o = _overlay(asset, anchor=anchor)
            x, y = ov.resolve_position(o, 0.5, frame_w=self.W, frame_h=self.H,
                                       asset_w=100, asset_h=50, safe=SAFE_TIKTOK)
            self.assertGreaterEqual(x, l, anchor)
            self.assertGreaterEqual(y, t, anchor)
            self.assertLessEqual(x + 100, r, anchor)
            self.assertLessEqual(y + 50, b, anchor)

    def test_offset_shifts_position(self):
        asset = _asset(self.dir)
        base = _overlay(asset, anchor="top-left")
        moved = _overlay(asset, anchor="top-left", offset=(0.1, 0.2))
        x0, y0 = ov.resolve_position(base, 0.5, frame_w=self.W, frame_h=self.H,
                                     asset_w=100, asset_h=50, safe=None)
        x1, y1 = ov.resolve_position(moved, 0.5, frame_w=self.W, frame_h=self.H,
                                     asset_w=100, asset_h=50, safe=None)
        self.assertEqual((x1 - x0, y1 - y0), (108, 384))

    def test_xy_track_replaces_offset(self):
        asset = _asset(self.dir)
        track = ov.Track("x", [ov.Keyframe(0.0, 0.0), ov.Keyframe(1.0, 0.5)],
                         easing="linear")
        o = _overlay(asset, anchor="top-left", tracks=[track])
        x0, _ = ov.resolve_position(o, 0.0, frame_w=self.W, frame_h=self.H,
                                    asset_w=100, asset_h=50, safe=None)
        x1, _ = ov.resolve_position(o, 1.0, frame_w=self.W, frame_h=self.H,
                                    asset_w=100, asset_h=50, safe=None)
        self.assertEqual(x1 - x0, 540)

    def test_oversized_asset_pinned_to_box_edge(self):
        asset = _asset(self.dir)
        o = _overlay(asset, anchor="center")
        x, y = ov.resolve_position(o, 0.5, frame_w=self.W, frame_h=self.H,
                                   asset_w=2000, asset_h=50, safe=SAFE_TIKTOK)
        self.assertEqual(x, SAFE_TIKTOK["box"][0])  # wider than the box: pin left

    def test_safe_none_clamps_to_frame(self):
        asset = _asset(self.dir)
        o = _overlay(asset, anchor="top-left", offset=(-0.5, -0.5))
        x, y = ov.resolve_position(o, 0.5, frame_w=self.W, frame_h=self.H,
                                   asset_w=100, asset_h=50, safe=None)
        self.assertEqual((x, y), (0, 0))


# ─── FILTER GENERATION ───────────────────────────────────────

@unittest.skipUnless(HAVE_PIL, "Pillow not installed")
class TestOverlayFilter(TmpDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.dir / "badge.png"
        Image.new("RGBA", (120, 60), (212, 113, 58, 255)).save(self.asset)

    def frag(self, o, **kw):
        kw.setdefault("frame_w", 1080)
        kw.setdefault("frame_h", 1920)
        kw.setdefault("safe", None)
        kw.setdefault("in_label", "0:v")
        kw.setdefault("out_label", "vout")
        return ov.overlay_filter(o, index=1, **kw)

    def test_static_enable_between_with_numbers(self):
        o = _overlay(self.asset, start=1.25, end=4.5)
        frag = self.frag(o)
        self.assertIn("enable='between(t,1.250,4.500)'", frag)

    def test_static_emits_no_ladder(self):
        o = _overlay(self.asset, start=0.0, end=2.0)
        frag = self.frag(o)
        self.assertNotIn("if(lt(t,", frag)
        self.assertNotIn("geq", frag)
        self.assertNotIn("eval=frame", frag)

    def test_static_labels_wired(self):
        o = _overlay(self.asset)
        frag = self.frag(o)
        self.assertIn("[1:v]", frag)
        self.assertIn("[ov1]", frag)
        self.assertIn("[0:v][ov1]overlay=", frag)
        self.assertTrue(frag.endswith("[vout]"))

    def test_static_scales_to_fraction_of_frame_width(self):
        o = _overlay(self.asset, scale=0.5)  # 540 wide, 270 tall (even)
        frag = self.frag(o)
        self.assertIn("scale=540:270", frag)

    def test_scale_capped_at_frame_width(self):
        o = _overlay(self.asset, scale=1.0)
        frag = self.frag(o)
        self.assertIn("scale=1080:540", frag)

    def test_animated_scale_emits_ladder(self):
        track = ov.Track("scale", [ov.Keyframe(0.0, 0.1), ov.Keyframe(0.5, 1.0)],
                         easing="back_out")
        o = _overlay(self.asset, start=0.0, end=0.5, tracks=[track])
        frag = self.frag(o)
        self.assertIn("eval=frame", frag)
        self.assertIn("if(lt(t,", frag)
        self.assertIn("x='if(lt(t,", frag)  # position ladder follows the size

    def test_fade_uses_geq_alpha_ladder_with_T(self):
        o = _overlay(self.asset, start=0.0, end=2.0, fade_in=0.5)
        frag = self.frag(o)
        self.assertIn("geq=", frag)
        self.assertIn("alpha(X,Y)", frag)
        self.assertIn("lt(T,", frag)
        self.assertNotIn("lt(t,0", frag.split("geq")[1].split("overlay")[0])

    def test_static_opacity_uses_constant_colorchannelmixer(self):
        o = _overlay(self.asset, opacity=0.5)
        frag = self.frag(o)
        self.assertIn("colorchannelmixer=aa=0.5000", frag)
        self.assertNotIn("geq", frag)

    def test_static_rotation_emits_rotate_with_canvas(self):
        o = _overlay(self.asset, rotation=45.0)
        frag = self.frag(o)
        self.assertIn("rotate=a='0.785398'", frag)
        self.assertIn("c=none", frag)

    def test_ladder_respects_max_rungs_and_notes_it(self):
        track = ov.Track("scale", [ov.Keyframe(0.0, 0.5), ov.Keyframe(10.0, 1.0)],
                         easing="elastic_out")
        o = _overlay(self.asset, start=0.0, end=10.0, tracks=[track])
        frag = self.frag(o)
        # w, h, x, y ladders — each capped at MAX_RUNGS rungs, plus the one leading
        # clamp that stops the first rung extrapolating backwards before the window.
        self.assertEqual(frag.count("if(lt(t,"), 4 * (ov.MAX_RUNGS + 1))
        self.assertTrue(any("MAX_RUNGS" in n for n in o.notes))

    def test_short_animation_stays_constant_rungs(self):
        track = ov.Track("scale", [ov.Keyframe(0.0, 0.5), ov.Keyframe(0.5, 1.0)])
        o = _overlay(self.asset, start=0.0, end=0.5, tracks=[track])
        frag = self.frag(o)
        # 0.5s at 30 Hz = 15 rungs per ladder, 4 ladders (w, h, x, y).
        self.assertEqual(frag.count("if(lt(t,"), 4 * 15)
        self.assertEqual(o.notes, [])

    def test_opacity_track_and_fades_compose(self):
        track = ov.Track("opacity", [ov.Keyframe(0.0, 1.0), ov.Keyframe(2.0, 0.5)],
                         easing="linear")
        o = _overlay(self.asset, start=0.0, end=2.0, tracks=[track], fade_out=0.5)
        frag = self.frag(o)
        self.assertIn("geq=", frag)


@unittest.skipUnless(HAVE_PIL, "Pillow not installed")
class TestCompose(TmpDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.assets = []
        for i in range(3):
            p = self.dir / f"a{i}.png"
            Image.new("RGBA", (40, 40), (255, 255, 255, 255)).save(p)
            self.assets.append(p)

    def test_empty_is_passthrough(self):
        fc, inputs = ov.compose([], frame_w=1080, frame_h=1920)
        self.assertEqual(fc, "[0:v]null[vout]")
        self.assertEqual(inputs, [])

    def test_three_overlays_three_inputs_in_order(self):
        overlays = [_overlay(a, start=i, end=i + 1) for i, a in enumerate(self.assets)]
        fc, inputs = ov.compose(overlays, frame_w=1080, frame_h=1920)
        self.assertEqual(fc.count("overlay="), 3)
        self.assertEqual(inputs, ["-i", str(self.assets[0]),
                                  "-i", str(self.assets[1]),
                                  "-i", str(self.assets[2])])

    def test_labels_chain(self):
        overlays = [_overlay(a, start=i, end=i + 1) for i, a in enumerate(self.assets)]
        fc, _ = ov.compose(overlays, frame_w=1080, frame_h=1920)
        self.assertIn("[0:v][ov1]overlay=", fc)
        self.assertIn("[v1][ov2]overlay=", fc)
        self.assertIn("[v2][ov3]overlay=", fc)
        self.assertTrue(fc.endswith("[vout]"))

    def test_animated_overlay_gets_looped_finite_input(self):
        track = ov.Track("scale", [ov.Keyframe(0.0, 0.5), ov.Keyframe(1.0, 1.0)])
        animated = _overlay(self.assets[0], start=0.0, end=1.0, tracks=[track])
        static = _overlay(self.assets[1], start=0.0, end=1.0)
        _, inputs = ov.compose([animated, static], frame_w=1080, frame_h=1920, fps=30)
        self.assertEqual(inputs, ["-loop", "1", "-framerate", "30", "-t", "1.000",
                                  "-i", str(self.assets[0]),
                                  "-i", str(self.assets[1])])

    def test_path_with_space_is_one_arg(self):
        spaced = self.dir / "my badge.png"
        spaced.write_bytes(self.assets[0].read_bytes())
        _, inputs = ov.compose([_overlay(spaced)], frame_w=1080, frame_h=1920)
        self.assertEqual(inputs, ["-i", str(spaced)])


# ─── VALIDATE ────────────────────────────────────────────────

@unittest.skipUnless(HAVE_PIL, "Pillow not installed")
class TestValidate(TmpDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.asset = self.dir / "badge.png"
        Image.new("RGBA", (100, 50), (212, 113, 58, 255)).save(self.asset)

    def test_clean_returns_empty(self):
        o = _overlay(self.asset, anchor="center", scale=0.2)
        self.assertEqual(ov.validate([o], frame_w=1080, frame_h=1920,
                                     safe=SAFE_TIKTOK, film_duration=10.0), [])

    def test_window_past_film_duration_warns_not_raises(self):
        o = _overlay(self.asset, start=8.0, end=12.0, scale=0.2)
        warnings = ov.validate([o], frame_w=1080, frame_h=1920, safe=SAFE_TIKTOK,
                               film_duration=10.0)
        self.assertEqual(len(warnings), 1)
        self.assertIn("12.000", warnings[0])

    def test_opaque_asset_warns(self):
        opaque = self.dir / "opaque.png"
        Image.new("RGB", (100, 50), (255, 0, 0)).save(opaque)
        o = _overlay(opaque, scale=0.2)
        warnings = ov.validate([o], frame_w=1080, frame_h=1920, safe=None)
        self.assertTrue(any("no alpha" in w for w in warnings))

    def test_clamped_position_named_with_both_positions(self):
        # bottom-right on TikTok insets requests a point under the platform UI.
        o = _overlay(self.asset, anchor="bottom-right", scale=0.2)
        warnings = ov.validate([o], frame_w=1080, frame_h=1920, safe=SAFE_TIKTOK)
        self.assertEqual(len(warnings), 1)
        self.assertIn(o.id, warnings[0])
        # displayed size is 216x108: requested bottom-right of the frame, clamped to
        # the box's bottom-right.
        self.assertIn("(864, 1812)", warnings[0])   # requested
        self.assertIn("(756, 1428)", warnings[0])   # clamped

    def test_unclamped_position_no_warning(self):
        o = _overlay(self.asset, anchor="center", scale=0.2)
        warnings = ov.validate([o], frame_w=1080, frame_h=1920, safe=SAFE_TIKTOK)
        self.assertEqual(warnings, [])

    def test_collision_same_region_same_time_warns(self):
        a = _overlay(self.asset, id="a", anchor="center", scale=0.4,
                     start=0.0, end=2.0)
        b = _overlay(self.asset, id="b", anchor="center", scale=0.4,
                     start=1.0, end=3.0)
        warnings = ov.validate([a, b], frame_w=1080, frame_h=1920, safe=SAFE_TIKTOK)
        self.assertTrue(any("overlap" in w and "'a'" in w and "'b'" in w
                            for w in warnings))

    def test_time_disjoint_no_collision(self):
        a = _overlay(self.asset, anchor="center", scale=0.4, start=0.0, end=1.0)
        b = _overlay(self.asset, anchor="center", scale=0.4, start=1.0, end=2.0)
        warnings = ov.validate([a, b], frame_w=1080, frame_h=1920, safe=SAFE_TIKTOK)
        self.assertEqual(warnings, [])

    def test_space_disjoint_no_collision(self):
        a = _overlay(self.asset, anchor="top-left", scale=0.2, start=0.0, end=2.0)
        b = _overlay(self.asset, anchor="bottom-right", scale=0.2, start=0.0, end=2.0)
        warnings = [w for w in ov.validate([a, b], frame_w=1080, frame_h=1920,
                                           safe=SAFE_TIKTOK)
                    if "overlap" in w]
        self.assertEqual(warnings, [])


# ─── PRESET RENDERING ────────────────────────────────────────

@unittest.skipUnless(HAVE_PIL, "Pillow not installed")
class TestPresets(TmpDirMixin, unittest.TestCase):
    def test_badge_writes_rgba_png_with_ink(self):
        out = ov.badge("50% OFF", self.dir / "b.png")
        with Image.open(out) as img:
            self.assertEqual(img.mode, "RGBA")
            self.assertIsNotNone(img.getchannel("A").getbbox())

    def test_badge_all_styles_render(self):
        for style in ("pill", "rect", "starburst", "circle"):
            out = ov.badge("SALE", self.dir / f"b-{style}.png", style=style)
            with Image.open(out) as img:
                self.assertEqual(img.mode, "RGBA")
                self.assertIsNotNone(img.getchannel("A").getbbox(), style)

    def test_badge_unknown_style_raises(self):
        with self.assertRaises(ov.OverlayError):
            ov.badge("SALE", self.dir / "b.png", style="neon")

    def test_badge_wider_with_more_text(self):
        short = ov.badge("50%", self.dir / "short.png")
        long = ov.badge("50% OFF", self.dir / "long.png")
        with Image.open(short) as a, Image.open(long) as b:
            self.assertGreater(b.size[0], a.size[0])

    def test_badge_plate_height_is_measured_box_plus_padding_exactly(self):
        # The rounded_rectangle off-by-one lives here: Pillow paints the bottom
        # coordinate inclusively, so an uncorrected plate is 1px taller than the
        # measured text box + padding.
        text, font_size, padding = "50% OFF", 96, 28
        font = ov._load_font(text, font_size)
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        tb = probe.textbbox((0, 0), cap.shape(text), font=font)
        expected_h = (tb[3] - tb[1]) + 2 * padding
        expected_w = (tb[2] - tb[0]) + 2 * padding
        out = ov.badge(text, self.dir / "exact.png",
                       font_size=font_size, padding=padding)
        with Image.open(out) as img:
            self.assertEqual(img.size, (expected_w, expected_h))

    def test_lower_third_height_is_measured_exactly(self):
        title, subtitle = "Jane Doe", "Saved four hours a week"
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        wrap_w = 900 - 2 * ov._LT_PADDING
        heights = []
        for text, size in ((title, ov._LT_TITLE_SIZE), (subtitle, ov._LT_SUB_SIZE)):
            font = ov._load_font(text, size)
            for line in ov._wrap_logical(text, font, wrap_w, probe):
                bb = probe.textbbox((0, 0), cap.shape(line), font=font)
                heights.append(bb[3] - bb[1])
        expected = (2 * ov._LT_PADDING + sum(heights)
                    + ov._LT_LINE_GAP * (len(heights) - 1) + ov._LT_PARA_GAP)
        out = ov.lower_third_plate(title, subtitle, self.dir / "lt.png", width=900)
        with Image.open(out) as img:
            self.assertEqual(img.size, (900, expected))

    def test_lower_third_plate_is_translucent(self):
        out = ov.lower_third_plate("A", "B", self.dir / "lt.png", opacity=0.72)
        with Image.open(out) as img:
            px = img.convert("RGBA")
            cx, cy = px.size[0] // 2, px.size[1] - 4  # plate, below the text
            alpha = px.getpixel((cx, cy))[3]
            self.assertGreater(alpha, 100)
            self.assertLess(alpha, 255)

    @unittest.skipUnless(HAVE_RESHAPER, "arabic-reshaper/python-bidi not installed")
    def test_badge_arabic_renders_nonblank_with_arabic_font(self):
        self.assertIn(cap.font_for(ARABIC), cap.ARABIC_FONT_PATHS)
        out = ov.badge(ARABIC, self.dir / "ar.png")
        with Image.open(out) as img:
            self.assertIsNotNone(img.getchannel("A").getbbox())
            # The shaped text is genuinely drawn, not tofu boxes: the badge is wider
            # than its padding, i.e. the text measured non-zero.
            self.assertGreater(img.size[0], 2 * 28 + 10)

    def test_mixed_string_picks_arabic_font(self):
        mixed = "SALE خصم"
        self.assertTrue(cap.has_arabic(mixed))
        found = cap.font_for(mixed)
        if found is not None:  # machine may lack fonts; content routing is the assertion
            self.assertIn(found, cap.ARABIC_FONT_PATHS)

    @unittest.skipUnless(HAVE_RESHAPER, "arabic-reshaper/python-bidi not installed")
    def test_lower_third_arabic_subtitle_wraps_before_reshaping(self):
        subtitle = ("هذا المنتج غيّر طريقة عملي تماماً أصبحت أنجز في ساعة واحدة "
                    "ما كان يستغرق يوماً كاملاً من العمل المتواصل")
        out = ov.lower_third_plate("سارة", subtitle, self.dir / "ar-lt.png",
                                   width=900)
        with Image.open(out) as img:
            px = img.convert("RGBA")
            w, h = px.size
            data = px.load()
            ink_rows = []
            for y in range(h):
                if any(data[x, y][3] > 200 and data[x, y][0] > 200
                       for x in range(0, w, 3)):
                    ink_rows.append(y)
            self.assertTrue(ink_rows, "lower third rendered blank")
            bands = 1 + sum(1 for a, b in zip(ink_rows, ink_rows[1:]) if b > a + 1)
            # title row + at least two wrapped subtitle rows
            self.assertGreaterEqual(bands, 3)

    def test_arrow_all_directions_render(self):
        for direction in ("down", "up", "left", "right"):
            out = ov.arrow(self.dir / f"arw-{direction}.png", direction=direction)
            with Image.open(out) as img:
                self.assertEqual(img.mode, "RGBA")
                self.assertIsNotNone(img.getchannel("A").getbbox(), direction)

    def test_arrow_horizontal_is_wide_not_tall(self):
        down = ov.arrow(self.dir / "d.png", direction="down", length=240)
        left = ov.arrow(self.dir / "l.png", direction="left", length=240)
        with Image.open(down) as a, Image.open(left) as b:
            self.assertGreater(a.size[1], a.size[0])
            self.assertGreater(b.size[0], b.size[1])

    def test_arrow_curved_renders(self):
        out = ov.arrow(self.dir / "curved.png", style="curved")
        with Image.open(out) as img:
            self.assertIsNotNone(img.getchannel("A").getbbox())

    def test_arrow_bad_direction_raises(self):
        with self.assertRaises(ov.OverlayError):
            ov.arrow(self.dir / "x.png", direction="sideways")

    def test_progress_bar_dims_and_two_colours(self):
        out = ov.progress_bar(self.dir / "pb.png", width=600, height=12,
                              color="#ffffff", track="#ffffff33")
        with Image.open(out) as img:
            self.assertEqual(img.size, (600, 12))
            self.assertEqual(img.mode, "RGBA")
            px = img.convert("RGBA")
            self.assertEqual(px.getpixel((300, 6))[:3], (255, 255, 255))
            self.assertEqual(px.getpixel((300, 6))[3], 255)


# ─── BURN (no ffmpeg needed) ─────────────────────────────────

class TestBurnNoFfmpeg(TmpDirMixin, unittest.TestCase):
    def test_empty_overlays_copies_without_ffmpeg(self):
        video = self.dir / "in.mp4"
        video.write_bytes(b"fake video bytes")
        out = ov.burn(video, [], self.dir / "out.mp4")
        self.assertEqual(out.read_bytes(), b"fake video bytes")

    def test_ffmpeg_absent_raises_with_brew_line(self):
        overlay = _overlay(_asset(self.dir))
        video = self.dir / "in.mp4"
        video.write_bytes(b"fake")
        with unittest.mock.patch.object(ov.shutil, "which", return_value=None):
            with self.assertRaises(ov.OverlayError) as ctx:
                ov.burn(video, [overlay], self.dir / "out.mp4")
        self.assertIn("brew install ffmpeg", str(ctx.exception))


# ─── BURN (ffmpeg-gated integration) ─────────────────────────

def _duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def _frame(video: Path, t: float, out: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", str(out)],
        capture_output=True, text=True, check=True,
    )
    return out


@unittest.skipUnless(HAVE_FFMPEG and HAVE_PIL, "needs ffmpeg and Pillow")
class TestBurnIntegration(TmpDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # Fixtures in Python, never shell — see module docstring.
        self.clip = make_fixtures.make(self.dir / "fx", n=1, seconds=3, w=384, h=384,
                                       fps=30, audio=False)[0]

    def test_static_badge_burns_and_duration_unchanged(self):
        badge = ov.badge("50% OFF", self.dir / "badge.png", font_size=64)
        o = ov.Overlay(badge, 0.5, 2.5, anchor="bottom-right", scale=0.4)
        out = ov.burn(self.clip, [o], self.dir / "static.mp4")
        self.assertTrue(out.is_file())
        self.assertGreater(out.stat().st_size, 0)
        self.assertAlmostEqual(_duration(out), _duration(self.clip), delta=0.1)

    def test_animated_badge_burns(self):
        badge = ov.badge("NEW", self.dir / "new.png", font_size=64)
        track = ov.Track("scale", [ov.Keyframe(0.5, 0.05), ov.Keyframe(1.2, 1.0)],
                         easing="back_out")
        o = ov.Overlay(badge, 0.5, 2.5, anchor="center", scale=0.4,
                       tracks=[track], fade_in=0.3, fade_out=0.3)
        out = ov.burn(self.clip, [o], self.dir / "anim.mp4")
        self.assertTrue(out.is_file())
        self.assertGreater(out.stat().st_size, 0)

    def test_three_overlays_burn_together(self):
        badge = ov.badge("50% OFF", self.dir / "b.png", font_size=64)
        spaced = self.dir / "my badge.png"  # a path with a space must survive
        spaced.write_bytes(badge.read_bytes())
        arw = ov.arrow(self.dir / "arw.png", direction="down", length=120)
        track = ov.Track("scale", [ov.Keyframe(0.0, 0.2), ov.Keyframe(1.0, 1.0)],
                         easing="back_out")
        overlays = [
            ov.Overlay(badge, 0.0, 3.0, anchor="top-left", scale=0.3),
            ov.Overlay(spaced, 0.5, 2.5, anchor="center", scale=0.3,
                       tracks=[track], fade_out=0.4),
            ov.Overlay(arw, 1.0, 2.0, anchor="bottom-center", scale=0.2),
        ]
        out = ov.burn(self.clip, overlays, self.dir / "three.mp4")
        self.assertTrue(out.is_file())
        self.assertGreater(out.stat().st_size, 0)

    def test_burn_failure_tails_stderr(self):
        badge = ov.badge("X", self.dir / "x.png", font_size=64)
        failed = subprocess.CompletedProcess([], 1, "", "line1\nline2\nboom tail")
        with unittest.mock.patch.object(ov, "probe_size", return_value=(384, 384)), \
             unittest.mock.patch.object(ov, "_probe_fps", return_value=30), \
             unittest.mock.patch.object(ov.subprocess, "run", return_value=failed):
            with self.assertRaises(ov.OverlayError) as ctx:
                ov.burn(self.clip, [ov.Overlay(badge, 0.0, 1.0)],
                        self.dir / "out.mp4")
        msg = str(ctx.exception)
        self.assertIn("overlay burn failed", msg)
        self.assertIn("boom tail", msg)

    def test_safe_zone_pixel_exact(self):
        """THE assertion that matters: a bottom-right badge on TikTok must land no
        non-background pixel outside safe['box'] — everything else proves the filter
        string was built; this proves the graphic is where the viewer can see it."""
        clip = make_fixtures.make(self.dir / "fx9x16", n=1, seconds=2, w=1080, h=1920,
                                  fps=30, audio=False)[0]
        safe = adspec.safe_zone("tiktok", 1080, 1920)
        badge = ov.badge("50% OFF", self.dir / "sz-badge.png", font_size=64)
        # anchor bottom-right with no offset requests the unsafe corner; the clamp
        # must move it inside the box.
        o = ov.Overlay(badge, 0.0, 2.0, anchor="bottom-right", scale=0.3)
        out = ov.burn(clip, [o], self.dir / "sz.mp4", safe=safe)
        frame = _frame(out, 1.0, self.dir / "sz.png")

        with Image.open(frame) as img:
            px = img.convert("RGB")
            bg = Image.new("RGB", px.size, (42, 61, 69))
            diff = ImageChops.difference(px, bg).convert("L").point(
                lambda v: 255 if v > 24 else 0)
            self.assertIsNotNone(diff.getbbox(), "badge did not render at all")

            l, t, r, b = safe["box"]
            w, h = px.size
            margin = 4  # yuv420p chroma bleed at the badge's edge
            mask = Image.new("L", px.size, 0)
            md = ImageDraw.Draw(mask)
            if t - margin > 0:
                md.rectangle([0, 0, w - 1, t - margin], fill=255)
            if b + margin < h:
                md.rectangle([0, b + margin, w - 1, h - 1], fill=255)
            if l - margin > 0:
                md.rectangle([0, 0, l - margin, h - 1], fill=255)
            if r + margin < w:
                md.rectangle([r + margin, 0, w - 1, h - 1], fill=255)
            outside = ImageChops.darker(diff, mask)
            self.assertIsNone(
                outside.getbbox(),
                "badge ink leaked outside the TikTok safe box — it would sit "
                "under the platform UI",
            )

    def test_fade_pixel_monotonic(self):
        """A fade expression that parses but does nothing exits 0; only the pixels
        tell the truth. Sample at start, start+fade_in/2 and start+fade_in."""
        badge = ov.badge("SALE", self.dir / "fade-badge.png", font_size=64)
        o = ov.Overlay(badge, 0.5, 3.0, anchor="center", scale=0.5, fade_in=1.0)
        out = ov.burn(self.clip, [o], self.dir / "fade.mp4")

        bg_rgb = (42, 61, 69)

        def contribution(t: float) -> float:
            frame = _frame(out, t, self.dir / f"f-{t}.png")
            with Image.open(frame) as img:
                px = img.convert("RGB")
                diff = ImageChops.difference(
                    px, Image.new("RGB", px.size, bg_rgb)).convert("L")
                hist = diff.histogram()
                total = px.size[0] * px.size[1]
                return sum(i * c for i, c in enumerate(hist)) / total

        c0 = contribution(0.5)               # fade just started: ~zero + codec noise
        c1 = contribution(0.5 + 0.5)         # halfway up
        c2 = contribution(0.5 + 1.0)         # fully on
        self.assertLess(c0, 2.0)
        self.assertGreater(c1, c0 + 3.0)
        self.assertGreater(c2, c1 + 3.0)


class TestAnimatedCombinationOrdering(TmpDirMixin, unittest.TestCase):
    """Animated scale AND animated opacity together — the combination, not either alone.

    geq addresses pixels by coordinate against the frame it is handed. Downstream of
    `scale=...:eval=frame` its input changes size every frame, so it reads a stale plane:
    observed on ffmpeg 8.1.1 as the correct badge PLUS a larger rectangle-clipped copy
    offset down-right. Scale-only and fade-only both rendered clean, which is why the
    per-property tests all passed and the ad was still visibly broken.
    """

    def _overlay(self, asset, **kw):
        return ov.Overlay(asset=asset, start=0.5, end=3.0, anchor="center",
                          scale=0.42, **kw)

    def test_alpha_is_applied_before_any_animated_scale(self):
        asset = self.dir / "badge.png"
        ov.badge("50% OFF", asset, style="starburst")
        o = self._overlay(asset, fade_in=0.25, fade_out=0.4,
                          tracks=[ov.Track("scale",
                                           [ov.Keyframe(0.5, 0.0), ov.Keyframe(1.0, 1.0)],
                                           easing="back_out")])
        chain = ov.overlay_filter(o, index=0, frame_w=768, frame_h=1376, safe=None,
                                  fps=30, in_label="0:v", out_label="v")
        self.assertIn("geq=", chain)
        self.assertIn("eval=frame", chain)
        self.assertLess(chain.index("geq="), chain.index("eval=frame"),
                        "geq must precede the per-frame scale or it samples a stale plane")

    def test_static_scale_still_emits_no_eval_frame(self):
        asset = self.dir / "badge.png"
        ov.badge("X", asset)
        o = self._overlay(asset, fade_in=0.25)
        chain = ov.overlay_filter(o, index=0, frame_w=768, frame_h=1376, safe=None,
                                  fps=30, in_label="0:v", out_label="v")
        self.assertNotIn("eval=frame", chain)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "ffmpeg not installed")
    def test_scale_and_fade_together_render_one_badge_not_two(self):
        asset = self.dir / "badge.png"
        ov.badge("50% OFF", asset, style="starburst", fill="#d4713a")
        bg = self.dir / "bg.mp4"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "color=c=black:s=768x1376:r=30", "-t", "4",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(bg)], check=True)
        out = self.dir / "out.mp4"
        o = self._overlay(asset, fade_in=0.25, fade_out=0.4,
                          tracks=[ov.Track("scale",
                                           [ov.Keyframe(0.5, 0.0), ov.Keyframe(1.0, 1.0)],
                                           easing="back_out")])
        ov.burn(bg, [o], out, safe=None)

        # Mid-animation is where the stale plane showed. The corrupt render painted a
        # SECOND copy offset down-right, so the discriminating question is not "how many
        # blobs" (they touch, and an ink-run count scores the corrupt frame identically)
        # but "is there ink outside where the badge can possibly be". Compute the badge's
        # own bounding box for this frame and assert nothing is drawn beyond it.
        t = 0.9
        aw, ah = ov._asset_size(asset)
        bw, bh = ov._scaled_size(o, t, 768, aw, ah)
        bx, by = ov.resolve_position(o, t, frame_w=768, frame_h=1376,
                                     asset_w=bw, asset_h=bh, safe=None)
        frame = self.dir / "f.png"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-ss", str(t), "-i", str(out), "-vframes", "1", str(frame)],
                       check=True)
        from PIL import Image
        im = Image.open(frame).convert("L")
        px = im.load()
        pad = 4  # scaler edge softness
        stray = [
            (x, y)
            for y in range(0, im.size[1], 3)
            for x in range(0, im.size[0], 3)
            if px[x, y] > 40
            and not (bx - pad <= x <= bx + bw + pad and by - pad <= y <= by + bh + pad)
        ]
        self.assertEqual(
            stray, [],
            f"{len(stray)} lit pixels outside the badge box "
            f"({bx},{by},{bx + bw},{by + bh}) — first at {stray[:3]}. "
            "The animated-scale/animated-alpha ordering has regressed: geq is sampling "
            "a stale plane and painting a second offset copy."
        )


class TestAnimatedScaleCanvas(TmpDirMixin, unittest.TestCase):
    """overlay negotiates its input link ONCE and scale=eval=frame never renegotiates it.

    A scale track opening at 0 hands overlay a 4x4 link, and every later frame composites
    at 4x4 — the graphic never appears at all. ffmpeg states it at verbose level:
        [Parsed_overlay_2] main w:768 h:1376 overlay w:4 h:4
    Early windows survived by luck, when a link reinit happened to land in time; the same
    badge at 25s on a 30s film drew nothing. So the varying scale rides inside a constant
    transparent canvas instead of resizing the frame under overlay's feet.
    """

    def _animated(self, asset):
        return ov.Overlay(asset=asset, start=25.0, end=30.0, anchor="center",
                          scale=0.40, fade_in=0.25, fade_out=0.4,
                          tracks=[ov.Track("scale", [ov.Keyframe(26.0, 0.0),
                                                     ov.Keyframe(26.45, 1.0)],
                                           "back_out")])

    def test_a_scale_track_pads_to_a_constant_canvas(self):
        asset = self.dir / "b.png"
        ov.badge("50% OFF", asset, style="starburst")
        chain = ov.overlay_filter(self._animated(asset), index=0, frame_w=768,
                                  frame_h=1376, safe=None, fps=24,
                                  in_label="0:v", out_label="v")
        self.assertIn("eval=frame", chain)
        self.assertIn("pad=", chain)
        self.assertLess(chain.index("scale=w="), chain.index("pad="),
                        "the pad must come AFTER the per-frame scale")

    def test_the_canvas_is_the_tracks_largest_size(self):
        asset = self.dir / "b.png"
        ov.badge("50% OFF", asset, style="starburst")
        o = self._animated(asset)
        aw, ah = ov._asset_size(asset)
        canvas = ov._canvas_size(o, o.start, 768, 1376, aw, ah)
        biggest = max(ov._scaled_size(o, t, 768, aw, ah)[0]
                      for t in ov._sample_times(o.start, o.end))
        self.assertGreaterEqual(canvas[0], biggest)

    def test_a_static_overlay_gets_no_pad(self):
        asset = self.dir / "b.png"
        ov.badge("X", asset)
        o = ov.Overlay(asset=asset, start=1.0, end=3.0, anchor="center", scale=0.4)
        chain = ov.overlay_filter(o, index=0, frame_w=768, frame_h=1376, safe=None,
                                  fps=24, in_label="0:v", out_label="v")
        self.assertNotIn("pad=", chain)

    def test_the_linear_ladder_clamps_below_its_window(self):
        """Without the leading clamp the first rung extrapolates backwards forever:
        a 5s window starting at 25s evaluates (0-25)/0.078 at t=0."""
        times = [25.0 + i * 0.078 for i in range(70)]
        values = [float(i) for i in range(70)]
        expr = ov._linear_ladder(times, values)
        self.assertTrue(expr.startswith("if(lt(t,25.000),0"),
                        f"ladder must clamp below its window, got {expr[:60]}")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "ffmpeg not installed")
    def test_a_late_window_actually_draws(self):
        asset = self.dir / "b.png"
        ov.badge("50% OFF", asset, style="starburst", fill="#d4713a")
        bg = self.dir / "bg.mp4"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "color=c=black:s=768x1376:r=24", "-t", "31",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(bg)], check=True)
        out = self.dir / "out.mp4"
        ov.burn(bg, [self._animated(asset)], out, safe=None)
        frame = self.dir / "f.png"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-ss", "27.5", "-i", str(out), "-vframes", "1", str(frame)],
                       check=True)
        from PIL import Image
        im = Image.open(frame).convert("L")
        px = im.load()
        lit = sum(1 for y in range(0, im.size[1], 4) for x in range(0, im.size[0], 4)
                  if px[x, y] > 40)
        self.assertGreater(lit, 500,
                           "a badge with a scale track at 25s on a 31s film drew nothing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
