#!/usr/bin/env python3
"""Tests for captions.py.

Stdlib only and fully offline. Run with plain `python3 scripts/test_captions.py`
(no uv, no deps). Pillow-gated render tests skip without Pillow, reshaping tests skip
without arabic-reshaper/python-bidi, and the ffmpeg burn tests skip without ffmpeg —
they synthesise their own tiny clip with lavfi, no real video files involved.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import captions as cap

try:
    from PIL import Image, ImageDraw, ImageFont  # noqa: F401
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

ARABIC = "توقف عن التمرير"  # "stop scrolling"
STYLE_KEYS = {
    "font_size_frac", "color", "stroke_color", "stroke_width_frac", "bg_color",
    "bg_padding_frac", "line_spacing", "align", "max_width_frac", "uppercase",
}


# ─── PARSE ───────────────────────────────────────────────────

class TestParseCues(unittest.TestCase):
    def test_happy_defaults_per_role(self):
        cues = cap.parse_cues([
            {"start": 20, "end": 30, "text": "Buy now", "role": "cta"},
            {"start": 0, "end": 3, "text": "Stop", "role": "hook"},
            {"start": 3, "end": 20, "text": "It works"},
            {"start": 0, "end": 30, "text": "AI-generated", "role": "disclosure"},
            {"start": 5, "end": 8, "text": "Jane, CEO", "role": "lower_third"},
        ])
        by_role = {c["role"]: c for c in cues}
        self.assertEqual(by_role["hook"]["position"], "center")
        self.assertEqual(by_role["caption"]["position"], "bottom")
        self.assertEqual(by_role["cta"]["position"], "center")
        # Top, not bottom: an always-on disclosure anchored bottom draws through every
        # bottom-anchored caption.
        self.assertEqual(by_role["disclosure"]["position"], [0.5, 0.04])
        self.assertEqual(by_role["lower_third"]["position"], "bottom")
        self.assertTrue(all(c["style"] == "bold" for c in cues))
        self.assertTrue(all(isinstance(c["start"], float) for c in cues))
        self.assertTrue(all(isinstance(c["end"], float) for c in cues))

    def test_sorted_by_start(self):
        cues = cap.parse_cues([
            {"start": 10, "end": 12, "text": "b"},
            {"start": 0, "end": 2, "text": "a"},
            {"start": 5, "end": 6, "text": "c"},
        ])
        self.assertEqual([c["text"] for c in cues], ["a", "c", "b"])

    def test_same_start_sorted_by_role_order(self):
        cues = cap.parse_cues([
            {"start": 0, "end": 2, "text": "x", "role": "cta"},
            {"start": 0, "end": 2, "text": "y", "role": "hook"},
        ])
        self.assertEqual([c["role"] for c in cues], ["hook", "cta"])

    def test_input_not_mutated(self):
        original = {"start": 0, "end": 2, "text": "a"}
        cap.parse_cues([original])
        self.assertEqual(original, {"start": 0, "end": 2, "text": "a"})

    def test_explicit_position_kept(self):
        cues = cap.parse_cues([{"start": 0, "end": 1, "text": "a", "position": [0.25, 0.5]}])
        self.assertEqual(cues[0]["position"], [0.25, 0.5])

    def test_not_a_list(self):
        with self.assertRaises(cap.CaptionError):
            cap.parse_cues({"start": 0})

    def test_entry_not_dict(self):
        with self.assertRaises(cap.CaptionError) as ctx:
            cap.parse_cues([{"start": 0, "end": 1, "text": "ok"}, "nope"])
        self.assertIn("cues[1]", str(ctx.exception))

    def test_missing_required_keys(self):
        for missing in ("start", "end", "text"):
            cue = {"start": 0, "end": 1, "text": "a"}
            del cue[missing]
            with self.assertRaises(cap.CaptionError) as ctx:
                cap.parse_cues([cue])
            self.assertIn("cues[0]", str(ctx.exception))
            self.assertIn(missing, str(ctx.exception))

    def test_end_not_after_start(self):
        with self.assertRaises(cap.CaptionError) as ctx:
            cap.parse_cues([{"start": 0, "end": 1, "text": "ok"},
                            {"start": 5, "end": 2, "text": "bad"}])
        self.assertIn("cues[1]", str(ctx.exception))
        self.assertIn("greater than", str(ctx.exception))

    def test_end_equal_start(self):
        with self.assertRaises(cap.CaptionError):
            cap.parse_cues([{"start": 2, "end": 2, "text": "bad"}])

    def test_negative_start(self):
        with self.assertRaises(cap.CaptionError) as ctx:
            cap.parse_cues([{"start": -0.5, "end": 1, "text": "bad"}])
        self.assertIn("cues[0]", str(ctx.exception))

    def test_non_numeric_start(self):
        with self.assertRaises(cap.CaptionError):
            cap.parse_cues([{"start": "0", "end": 1, "text": "bad"}])

    def test_bool_start_rejected(self):
        with self.assertRaises(cap.CaptionError):
            cap.parse_cues([{"start": True, "end": 1, "text": "bad"}])

    def test_unknown_role_lists_valid(self):
        with self.assertRaises(cap.CaptionError) as ctx:
            cap.parse_cues([{"start": 0, "end": 1, "text": "a", "role": "splash"}])
        msg = str(ctx.exception)
        self.assertIn("cues[0]", msg)
        for role in cap.CUE_ROLES:
            self.assertIn(role, msg)

    def test_unknown_style_lists_valid(self):
        with self.assertRaises(cap.CaptionError) as ctx:
            cap.parse_cues([{"start": 0, "end": 1, "text": "a", "style": "neon"}])
        msg = str(ctx.exception)
        self.assertIn("cues[0]", msg)
        for style in cap.CAPTION_STYLES:
            self.assertIn(style, msg)

    def test_bad_position_string(self):
        with self.assertRaises(cap.CaptionError):
            cap.parse_cues([{"start": 0, "end": 1, "text": "a", "position": "middle"}])

    def test_position_out_of_range(self):
        with self.assertRaises(cap.CaptionError) as ctx:
            cap.parse_cues([{"start": 0, "end": 1, "text": "a", "position": [1.5, 0.5]}])
        self.assertIn("0..1", str(ctx.exception))

    def test_position_wrong_shape(self):
        with self.assertRaises(cap.CaptionError):
            cap.parse_cues([{"start": 0, "end": 1, "text": "a", "position": [0.5]}])

    def test_position_non_numeric(self):
        with self.assertRaises(cap.CaptionError):
            cap.parse_cues([{"start": 0, "end": 1, "text": "a", "position": ["x", 0.5]}])

    def test_boundary_first_and_last_validated(self):
        with self.assertRaises(cap.CaptionError) as ctx:
            cap.parse_cues([{"start": 0, "end": 0, "text": "first bad"},
                            {"start": 1, "end": 2, "text": "fine"}])
        self.assertIn("cues[0]", str(ctx.exception))
        with self.assertRaises(cap.CaptionError) as ctx:
            cap.parse_cues([{"start": 0, "end": 1, "text": "fine"},
                            {"start": 3, "end": 1, "text": "last bad"}])
        self.assertIn("cues[1]", str(ctx.exception))


# ─── OVERLAPS ────────────────────────────────────────────────

class TestOverlaps(unittest.TestCase):
    def test_same_role_overlap_warned(self):
        cues = cap.parse_cues([
            {"start": 0, "end": 5, "text": "a"},
            {"start": 4, "end": 9, "text": "b"},
        ])
        warns = cap.overlaps(cues)
        self.assertEqual(len(warns), 1)
        self.assertIn("caption", warns[0])

    def test_disclosure_does_not_collide_with_bottom_captions(self):
        """The defect this check exists for: a full-length disclosure over bottom
        captions rendered one string through the other, and a role-only overlap check
        called it clean."""
        cues = cap.parse_cues([
            {"start": 0, "end": 30, "text": "AI-generated", "role": "disclosure"},
            {"start": 6, "end": 12, "text": "It was the light."},
        ])
        self.assertEqual(cap.overlaps(cues), [])

    def test_same_band_different_role_warned(self):
        cues = cap.parse_cues([
            {"start": 0, "end": 30, "text": "AI-generated", "role": "disclosure",
             "position": "bottom"},
            {"start": 6, "end": 12, "text": "It was the light."},
        ])
        warns = cap.overlaps(cues)
        self.assertEqual(len(warns), 1)
        self.assertIn("bottom band", warns[0])

    def test_band_buckets(self):
        self.assertEqual(cap._band("bottom"), "bottom")
        self.assertEqual(cap._band([0.5, 0.04]), "top")
        self.assertEqual(cap._band([0.5, 0.5]), "center")
        self.assertEqual(cap._band([0.5, 0.95]), "bottom")

    def test_different_role_overlap_ignored(self):
        cues = cap.parse_cues([
            {"start": 0, "end": 30, "text": "AI", "role": "disclosure"},
            {"start": 0, "end": 5, "text": "a"},
        ])
        self.assertEqual(cap.overlaps(cues), [])

    def test_touching_is_not_overlap(self):
        cues = cap.parse_cues([
            {"start": 0, "end": 5, "text": "a"},
            {"start": 5, "end": 9, "text": "b"},
        ])
        self.assertEqual(cap.overlaps(cues), [])

    def test_empty(self):
        self.assertEqual(cap.overlaps([]), [])


# ─── ARABIC ──────────────────────────────────────────────────

class TestArabic(unittest.TestCase):
    def test_has_arabic_latin(self):
        self.assertFalse(cap.has_arabic("Stop scrolling."))

    def test_has_arabic_arabic(self):
        self.assertTrue(cap.has_arabic(ARABIC))

    def test_has_arabic_mixed(self):
        self.assertTrue(cap.has_arabic("Sale تخفيضات 50%"))

    def test_has_arabic_empty(self):
        self.assertFalse(cap.has_arabic(""))

    def test_has_arabic_presentation_forms(self):
        self.assertTrue(cap.has_arabic("ﺍﻟﻌﺮﺑﻴﺔ"))  # presentation forms only

    def test_has_arabic_supplement(self):
        self.assertTrue(cap.has_arabic("ݐ"))

    def test_shape_identity_on_latin(self):
        self.assertEqual(cap.shape("Hello world"), "Hello world")

    @unittest.skipUnless(HAVE_RESHAPER, "arabic-reshaper/python-bidi not installed")
    def test_shape_changes_arabic(self):
        shaped = cap.shape(ARABIC)
        self.assertNotEqual(shaped, ARABIC)

    def test_find_font_none_for_missing(self):
        self.assertIsNone(cap.find_font(["/no/such/font-a.ttf", "/no/such/font-b.ttf"]))

    def test_find_font_returns_first_existing(self):
        self.assertEqual(cap.find_font(["/no/such.ttf", __file__]), __file__)

    def test_font_for_arabic_uses_arabic_list(self):
        found = cap.font_for(ARABIC)
        if found is not None:  # machine may have no Arabic font; that's allowed
            self.assertIn(found, cap.ARABIC_FONT_PATHS)

    def test_font_for_latin_uses_latin_list(self):
        found = cap.font_for("Hello")
        if found is not None:
            self.assertIn(found, cap.LATIN_FONT_PATHS)

    def test_ibm_plex_listed_before_noto(self):
        self.assertLess(
            cap.ARABIC_FONT_PATHS.index(
                cap.os.path.expanduser("~/Library/Fonts/IBMPlexSansArabic-Bold.otf")),
            cap.ARABIC_FONT_PATHS.index(
                cap.os.path.expanduser("~/Library/Fonts/NotoSansArabic-Bold.ttf")),
        )

    def test_uppercase_applied_to_latin(self):
        style = dict(cap.CAPTION_STYLES["cta"])
        self.assertEqual(cap._maybe_upper("buy now", style), "BUY NOW")

    def test_uppercase_suppressed_for_arabic(self):
        style = dict(cap.CAPTION_STYLES["cta"])
        self.assertEqual(cap._maybe_upper(ARABIC, style), ARABIC)

    def test_uppercase_suppressed_for_mixed(self):
        style = dict(cap.CAPTION_STYLES["cta"])
        mixed = "Sale تخفيضات"
        self.assertEqual(cap._maybe_upper(mixed, style), mixed)


# ─── STYLES ──────────────────────────────────────────────────

class TestStyles(unittest.TestCase):
    def test_every_style_has_every_key(self):
        for name, style in cap.CAPTION_STYLES.items():
            self.assertEqual(set(style), STYLE_KEYS, f"style {name!r} keys")

    def test_required_styles_exist(self):
        for name in ("bold", "plate", "subtle", "cta"):
            self.assertIn(name, cap.CAPTION_STYLES)

    def test_cta_is_uppercase_and_plated(self):
        self.assertTrue(cap.CAPTION_STYLES["cta"]["uppercase"])
        self.assertIsNotNone(cap.CAPTION_STYLES["cta"]["bg_color"])

    def test_bold_is_default_and_unplated(self):
        self.assertFalse(cap.CAPTION_STYLES["bold"]["uppercase"])
        self.assertIsNone(cap.CAPTION_STYLES["bold"]["bg_color"])


# ─── SRT ─────────────────────────────────────────────────────

class TestSrt(unittest.TestCase):
    def test_exact_output_two_cues(self):
        cues = cap.parse_cues([
            {"start": 0, "end": 2.5, "text": "Hello"},
            {"start": 2.5, "end": 4, "text": "World\nAgain"},
        ])
        self.assertEqual(
            cap.srt_from_cues(cues),
            "1\n00:00:00,000 --> 00:00:02,500\nHello\n\n"
            "2\n00:00:02,500 --> 00:00:04,000\nWorld\nAgain\n\n",
        )

    def test_timestamp_formatting(self):
        cues = cap.parse_cues([{"start": 65.5, "end": 3661.25, "text": "x"}])
        self.assertIn("00:01:05,500 --> 01:01:01,250", cap.srt_from_cues(cues))

    def test_disclosure_skipped(self):
        cues = cap.parse_cues([
            {"start": 0, "end": 30, "text": "AI-generated", "role": "disclosure"},
            {"start": 1, "end": 2, "text": "real"},
        ])
        srt = cap.srt_from_cues(cues)
        self.assertNotIn("AI-generated", srt)
        self.assertIn("real", srt)
        self.assertTrue(srt.startswith("1\n"))

    def test_arabic_emitted_logical_not_reshaped(self):
        cues = cap.parse_cues([{"start": 0, "end": 2, "text": ARABIC}])
        srt = cap.srt_from_cues(cues)
        self.assertIn(ARABIC, srt)
        if HAVE_RESHAPER:
            self.assertNotIn(cap.reshape_arabic(ARABIC), srt)

    def test_empty(self):
        self.assertEqual(cap.srt_from_cues([]), "")


# ─── RENDER (Pillow-gated) ───────────────────────────────────

SAFE_9x16 = {
    "width": 1080, "height": 1920,
    "top": 192, "right": 108, "bottom": 384, "left": 0,
    "box": (0, 192, 972, 1536),
}


def alpha_bbox(path):
    with Image.open(path) as img:
        return img.convert("RGBA").getchannel("A").getbbox()


@unittest.skipUnless(HAVE_PIL, "Pillow not installed")
class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def render(self, cue, w=1080, h=1920, safe=SAFE_9x16, name="cue.png"):
        return cap.render_cue_png(cue, w, h, safe, self.dir / name)

    def test_rgba_png_right_size_1080p(self):
        out = self.render({"start": 0, "end": 1, "text": "Hello", "role": "caption",
                           "style": "bold", "position": "bottom"})
        with Image.open(out) as img:
            self.assertEqual(img.size, (1080, 1920))
            self.assertEqual(img.mode, "RGBA")

    def test_rgba_png_right_size_4k(self):
        out = self.render({"start": 0, "end": 1, "text": "Hello", "role": "caption",
                           "style": "bold", "position": "bottom"}, w=2160, h=3840)
        with Image.open(out) as img:
            self.assertEqual(img.size, (2160, 3840))

    def test_4k_font_doubles(self):
        cue = {"start": 0, "end": 1, "text": "Scale me", "role": "caption",
               "style": "bold", "position": "center"}
        hd = alpha_bbox(self.render(cue, name="hd.png"))
        uhd = alpha_bbox(self.render(cue, w=2160, h=3840, name="uhd.png"))
        ratio = (uhd[3] - uhd[1]) / (hd[3] - hd[1])
        self.assertGreater(ratio, 1.6)
        self.assertLess(ratio, 2.4)

    def test_text_inside_safe_box(self):
        out = self.render({"start": 0, "end": 1, "text": "Inside the box please",
                           "role": "caption", "style": "bold", "position": "bottom"})
        bbox = alpha_bbox(out)
        self.assertIsNotNone(bbox)
        tol = round(0.004 * 1920) + 6  # stroke spill below the nominal block bottom
        self.assertGreaterEqual(bbox[0], SAFE_9x16["box"][0])
        self.assertGreaterEqual(bbox[1], SAFE_9x16["box"][1])
        self.assertLessEqual(bbox[2], SAFE_9x16["box"][2] + tol)
        self.assertLessEqual(bbox[3], SAFE_9x16["box"][3] + tol)

    def test_wrap_long_string_multiple_lines(self):
        from PIL import Image, ImageDraw
        font = cap._load_font("Hello", 60)
        draw = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        lines = cap.wrap_text("one two three four five six seven eight nine ten",
                              font, 300, draw)
        self.assertGreater(len(lines), 1)

    def test_wrap_preserves_hard_breaks(self):
        from PIL import Image, ImageDraw
        font = cap._load_font("Hello", 60)
        draw = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        lines = cap.wrap_text("a\nb", font, 5000, draw)
        self.assertEqual(lines, ["a", "b"])

    def test_wrap_long_word_not_split(self):
        from PIL import Image, ImageDraw
        font = cap._load_font("Hello", 60)
        draw = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        lines = cap.wrap_text("supercalifragilisticexpialidocious ok", font, 100, draw)
        self.assertIn("supercalifragilisticexpialidocious", lines)
        self.assertTrue(all("supercali" not in l or l == "supercalifragilisticexpialidocious"
                            for l in lines))

    def test_point_position_clamped_into_box(self):
        out = self.render({"start": 0, "end": 1, "text": "AI-generated video",
                           "role": "disclosure", "style": "subtle",
                           "position": [0.5, 0.99]})
        bbox = alpha_bbox(out)
        self.assertIsNotNone(bbox)
        tol = round(0.002 * 1920) + 6
        self.assertLessEqual(bbox[3], SAFE_9x16["box"][3] + tol)
        self.assertGreaterEqual(bbox[0], SAFE_9x16["box"][0])
        self.assertLessEqual(bbox[2], SAFE_9x16["box"][2] + tol)

    def test_safe_none_uses_whole_frame(self):
        out = self.render({"start": 0, "end": 1, "text": "x", "role": "caption",
                           "style": "bold", "position": "bottom"}, safe=None)
        bbox = alpha_bbox(out)
        self.assertIsNotNone(bbox)
        self.assertGreater(bbox[3], 1536)  # bottom of frame, below the usual safe box

    def test_plate_drawn_for_plate_style(self):
        out = self.render({"start": 0, "end": 1, "text": "plated", "role": "caption",
                           "style": "plate", "position": "center"})
        with Image.open(out) as img:
            px = img.convert("RGBA")
            bbox = px.getchannel("A").getbbox()
            cx, cy = (bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2
            # just inside the plate's left edge, above/below the glyphs: plate alpha
            edge = px.getpixel((bbox[0] + 2, cy))
            self.assertGreater(edge[3], 100)

    def test_unknown_style_raises_at_render(self):
        with self.assertRaises(cap.CaptionError):
            self.render({"start": 0, "end": 1, "text": "x", "role": "caption",
                         "style": "neon", "position": "bottom"})

    def test_render_cues_names_and_entries(self):
        cues = cap.parse_cues([
            {"start": 0, "end": 1, "text": "a", "role": "hook"},
            {"start": 1, "end": 2, "text": "b"},
        ])
        rendered = cap.render_cues(cues, 320, 568, None, self.dir / "cues")
        self.assertEqual(len(rendered), 2)
        self.assertEqual(rendered[0]["png"].name, "cue-00-hook.png")
        self.assertEqual(rendered[1]["png"].name, "cue-01-caption.png")
        for entry in rendered:
            self.assertTrue(entry["png"].is_file())
            self.assertIs(entry["cue"], cues[entry["index"]])

    def test_no_font_file_still_renders(self):
        # No font anywhere on disk -> load_default fallback, no crash.
        self.assertIsNone(cap.find_font(["/nope.ttf"]))
        with unittest.mock.patch.object(cap, "find_font", return_value=None):
            out = self.render({"start": 0, "end": 1, "text": "fallback",
                               "role": "caption", "style": "bold", "position": "top"},
                              name="fallback.png")
        self.assertTrue(out.is_file())

    def test_cta_card_produces_file(self):
        out = cap.cta_card("Buy now", 1080, 1920, self.dir / "card.png",
                           safe=SAFE_9x16, sub="Link in bio")
        self.assertTrue(out.is_file())
        with Image.open(out) as img:
            self.assertEqual(img.size, (1080, 1920))

    def test_cta_card_without_sub(self):
        out = cap.cta_card("Buy now", 1080, 1920, self.dir / "card2.png")
        self.assertTrue(out.is_file())

    def test_safe_guide_produces_file(self):
        out = cap.safe_guide(1080, 1920, SAFE_9x16, self.dir / "guide.png")
        self.assertTrue(out.is_file())
        with Image.open(out) as img:
            self.assertEqual(img.size, (1080, 1920))
            px = img.convert("RGBA")
            self.assertGreater(px.getpixel((540, 50))[3], 0)      # top inset is red
            self.assertEqual(px.getpixel((540, 960))[3], 0)        # safe box is clear

    @unittest.skipUnless(HAVE_RESHAPER, "arabic-reshaper/python-bidi not installed")
    def test_arabic_cue_renders(self):
        out = self.render({"start": 0, "end": 1, "text": ARABIC, "role": "caption",
                           "style": "bold", "position": "bottom"}, name="ar.png")
        self.assertIsNotNone(alpha_bbox(out))


# ─── BURN (ffmpeg-gated) ─────────────────────────────────────

@unittest.skipUnless(HAVE_FFMPEG and HAVE_PIL, "needs ffmpeg and Pillow")
class BurnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.video = self.dir / "in.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x568:d=2",
             "-pix_fmt", "yuv420p", str(self.video)],
            capture_output=True, check=True,
        )

    def test_probe_size(self):
        self.assertEqual(cap.probe_size(self.video), (320, 568))

    def test_probe_size_missing_file(self):
        self.assertIsNone(cap.probe_size(self.dir / "nope.mp4"))

    def test_zero_cues_copies_without_reencode(self):
        out = self.dir / "copy.mp4"
        result = cap.burn(self.video, [], out)
        self.assertEqual(result, out)
        self.assertEqual(self.video.read_bytes(), out.read_bytes())

    def test_one_cue_burns_and_size_matches(self):
        cues = cap.parse_cues([{"start": 0, "end": 2, "text": "Hello", "role": "caption"}])
        out = cap.burn(self.video, cues, self.dir / "burned.mp4")
        self.assertTrue(out.is_file())
        self.assertGreater(out.stat().st_size, 0)
        self.assertEqual(cap.probe_size(out), (320, 568))

    def test_explicit_size_skips_probe(self):
        cues = cap.parse_cues([{"start": 0, "end": 1, "text": "Hi"}])
        out = cap.burn(self.video, cues, self.dir / "sized.mp4", width=320, height=568)
        self.assertEqual(cap.probe_size(out), (320, 568))

    def test_bogus_cue_raises(self):
        with self.assertRaises(cap.CaptionError):
            cap.burn(self.video, [{"start": 0, "end": 1, "text": "x", "style": "neon"}],
                     self.dir / "bad.mp4")

    def test_unprobeable_video_raises(self):
        bogus = self.dir / "bogus.mp4"
        bogus.write_bytes(b"not a video")
        cues = cap.parse_cues([{"start": 0, "end": 1, "text": "x"}])
        with self.assertRaises(cap.CaptionError):
            cap.burn(bogus, cues, self.dir / "out.mp4")


@unittest.skipUnless(HAVE_PIL, "Pillow not installed")
class SafeBoxSweep(unittest.TestCase):
    """Every platform x role x style x script must render inside the safe box.

    Two real defects were found by rendering rather than by reasoning, and both were
    invisible to a single happy-path check:

    * A single line of ARABIC anchored to the bottom overshot by 28px, because placement
      used the nominal `font_size * line_spacing` while ي/ن descenders drop past it. Two
      lines of the same string fitted, since the second line's spacing absorbed the
      descender — so the failing case was the one that looked safest.
    * Every `plate` style overshot by exactly 1px, because Pillow's rounded_rectangle
      paints its bottom coordinate inclusively.

    Text outside this box is covered by the platform's own UI, so an overflow is not a
    cosmetic issue: it is copy the viewer never sees.
    """

    PLATFORMS = ("tiktok", "reels", "shorts", "feed", "youtube")
    SAMPLES = (
        ("latin_one_line", "It was the light."),
        ("latin_two_line", "It was the light and nothing else at all in this whole room"),
        ("arabic_one_line", "ظننت أنني بحاجة إلى مزيد من القهوة"),
        ("arabic_two_line", "ظننت أنني بحاجة إلى مزيد من القهوة ولكن المشكلة كانت في الإضاءة"),
        ("mixed", "Halo — ظننت أنني بحاجة"),
    )
    COMBOS = (("hook", "bold"), ("caption", "bold"), ("caption", "plate"),
              ("cta", "cta"), ("disclosure", "subtle"))

    def _sweep(self, samples):
        import adspec

        checked = 0
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for platform in self.PLATFORMS:
                safe = adspec.safe_zone(platform)
                left, top, right, bottom = safe["box"]
                for role, style in self.COMBOS:
                    for name, text in samples:
                        cue = cap.parse_cues([{"start": 0, "end": 3, "text": text,
                                               "role": role, "style": style}])[0]
                        png = cap.render_cue_png(
                            cue, safe["width"], safe["height"], safe,
                            out / f"{platform}-{role}-{style}-{name}.png")
                        ink = alpha_bbox(png)
                        checked += 1
                        self.assertIsNotNone(ink, f"{platform}/{role}/{style}/{name} drew nothing")
                        self.assertGreaterEqual(ink[0], left, f"{platform}/{role}/{style}/{name} left")
                        self.assertGreaterEqual(ink[1], top, f"{platform}/{role}/{style}/{name} top")
                        self.assertLessEqual(ink[2], right, f"{platform}/{role}/{style}/{name} right")
                        self.assertLessEqual(ink[3], bottom, f"{platform}/{role}/{style}/{name} bottom")
        return checked

    def test_latin_combinations_stay_inside_the_safe_box(self):
        latin = [s for s in self.SAMPLES if not cap.has_arabic(s[1])]
        self.assertEqual(self._sweep(latin), 50)

    @unittest.skipUnless(HAVE_RESHAPER, "arabic-reshaper/python-bidi not installed")
    def test_arabic_combinations_stay_inside_the_safe_box(self):
        arabic = [s for s in self.SAMPLES if cap.has_arabic(s[1])]
        self.assertEqual(self._sweep(arabic), 75)


if __name__ == "__main__":
    unittest.main(verbosity=2)
