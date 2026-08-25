#!/usr/bin/env python3
"""Offline tests for the social-ad compiler."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import adspec
import storyboard


def spec_for(name: str) -> dict:
    fmt = adspec.AD_FORMATS[name]
    roles = fmt["roles"] or ("a",)
    beats = [{"id": role, "panel": f"panel {role}", **({"action": "a clear action"} if i < len(roles) - 1 else {})}
             for i, role in enumerate(roles)]
    spec = {"version": 1, "kind": "ad", "title": "Test", "format": name,
            "platform": "tiktok", "duration": max(20, len(beats) * 4), "beats": beats}
    if fmt["needs_product"]: spec["product"] = {"name": "Widget", "refs": ["widget.png"]}
    if fmt["needs_actor"]: spec["actor"] = {"name": "Maya", "ref": "maya.png"}
    return spec


class TestFormats(unittest.TestCase):
    def test_every_format_compiles_to_a_valid_story(self):
        for name in adspec.AD_FORMATS:
            with self.subTest(name=name):
                plan = adspec.compile_to_story(spec_for(name))
                storyboard.validate_spec(plan["story"])
                self.assertEqual(plan["story"]["aspect"], "9:16")
                self.assertFalse(plan["story"]["allow_text"])

    def test_weights_are_normalised(self):
        for name, fmt in adspec.AD_FORMATS.items():
            self.assertTrue(not fmt["weights"] or abs(sum(fmt["weights"]) - 1) < 1e-9, name)


class TestAllocation(unittest.TestCase):
    def test_largest_remainder_and_pins(self):
        equal = {"roles": ("a", "b", "c", "d"), "weights": (.25,) * 4}
        self.assertEqual(adspec.allocate_beats(equal, 30), [8, 8, 7, 7])
        fifth = {"roles": ("a", "b", "c", "d", "e"), "weights": (.2,) * 5}
        self.assertEqual(adspec.allocate_beats(fifth, 31), [7, 6, 6, 6, 6])
        self.assertEqual(adspec.allocate_beats(fifth, 30, overrides=[10, None, None, None, None]), [10, 5, 5, 5, 5])

    def test_bounds_and_impossible_totals(self):
        fifth = {"roles": ("a", "b", "c", "d", "e"), "weights": (.2,) * 5}
        with self.assertRaisesRegex(adspec.AdSpecError, "needs at least 20s"):
            adspec.allocate_beats(fifth, 12)
        with self.assertRaisesRegex(adspec.AdSpecError, "overrides\\[0\\]"):
            adspec.allocate_beats(fifth, 30, overrides=[3, None, None, None, None])
        self.assertEqual(adspec.allocate_beats(fifth, 100), [15] * 5)

    def test_totals_for_all_formats(self):
        for fmt in adspec.AD_FORMATS.values():
            if not fmt["roles"]:
                continue
            for total in (20, 25, 30, 45, 60):
                if total >= len(fmt["roles"]) * 4:
                    values = adspec.allocate_beats(fmt, total)
                    self.assertEqual(sum(values), min(total, len(fmt["roles"]) * 15))
                    self.assertTrue(all(4 <= value <= 15 for value in values))


class TestValidation(unittest.TestCase):
    def assert_bad(self, mutate, text):
        spec = spec_for("problem-solution"); mutate(spec)
        with self.assertRaisesRegex(adspec.AdSpecError, text): adspec.validate_ad_spec(spec)

    def test_sad_paths(self):
        cases = [
            (lambda s: s.update(version=2), "version"),
            (lambda s: s.update(extra=True), "unknown"),
            (lambda s: s["beats"][0].update(extra=True), "unknown"),
            (lambda s: s.update(format="nope"), "format"),
            (lambda s: s.update(platform="nope"), "platform"),
            (lambda s: s["beats"].__setitem__(0, {"id": "wrong", "panel": "x", "action": "x"}), "expects ids"),
            (lambda s: s["beats"].__setitem__(1, {**s["beats"][1], "id": "hook"}), "duplicated"),
            (lambda s: s["beats"][0].pop("panel"), "panel"),
            (lambda s: s["beats"][0].pop("action"), "action"),
            (lambda s: s.pop("product"), "product.refs"),
            (lambda s: s.pop("actor"), "actor.ref"),
            (lambda s: s.update(cta={}), "cta.text"),
        ]
        for mutate, text in cases:
            with self.subTest(text=text): self.assert_bad(mutate, text)

    def test_claims_and_disclosure_warning(self):
        spec = spec_for("problem-solution")
        spec["product"]["banned_claims"] = ["CURES"]
        spec["beats"][1]["say"] = "This cures everything"
        warnings = adspec.validate_ad_spec(spec)
        self.assertTrue(any("CURES" in warning for warning in warnings))
        self.assertTrue(any("disclosure" in warning for warning in warnings))
        spec["disclosure"] = True
        self.assertEqual(adspec.check_claims(spec), ["spec.beats[problem]: banned claim 'CURES' appears in copy"])
        spec["product"]["banned_claims"] = []
        self.assertEqual(adspec.check_claims(spec), [])


class TestCompilation(unittest.TestCase):
    def test_cues_and_stripped_fields(self):
        spec = spec_for("problem-solution")
        spec["beats"][0].update(caption="Look", lang="Arabic", say="Hello")
        spec["beats"][1]["caption"] = "Then this"
        spec["cta"] = {"text": "Buy now", "panel": "product beauty shot"}
        spec["disclosure"] = True
        plan = adspec.compile_to_story(spec)
        cues = plan["cues"]
        self.assertEqual([(cue["role"], cue["text"]) for cue in cues], [("hook", "Look"), ("caption", "Then this"), ("cta", "Buy now"), ("disclosure", adspec.DISCLOSURE_DEFAULT)])
        self.assertEqual(cues[2]["start"], plan["beats"][-1]["start"])
        self.assertEqual(cues[3]["end"], float(spec["duration"]))
        self.assertNotIn("caption", plan["story"]["shots"][0])
        self.assertNotIn("lang", plan["story"]["shots"][0])
        self.assertNotIn("say", plan["story"]["shots"][0])
        self.assertEqual(plan["story"]["shots"][0]["sound"]["dialogue"][0]["line"], "Hello")

    def test_custom_end_name_allow_text_and_prompt(self):
        spec = spec_for("custom")
        spec["beats"] = [{"id": "end", "panel": "still"}]
        spec["duration"] = 4; spec["allow_text"] = True; spec["disclosure"] = "Made with AI"
        plan = adspec.compile_to_story(spec)
        self.assertEqual(len(plan["story"]["shots"]), 2)
        self.assertEqual(plan["story"]["shots"][-1]["id"], "end_2")
        self.assertFalse(plan["story"]["allow_text"])
        self.assertTrue(any("allow_text" in warning for warning in plan["warnings"]))
        self.assertEqual(plan["cues"][-1]["text"], "Made with AI")
        product = spec_for("hero-product"); product["product"]["palette"] = "warm cream"
        self.assertIn("Widget", adspec.compile_board_prompt_extra(product))
        self.assertIn("warm cream", adspec.compile_board_prompt_extra(product))

    def test_estimate(self):
        plan = adspec.compile_to_story(spec_for("custom"))
        self.assertEqual(adspec.estimate(plan, .125, 1.2), {"clips": 1, "clip_seconds": 15, "clips_cost": 1.875, "board_cost": 1.2, "total": 3.075})


class TestSafeZones(unittest.TestCase):
    def test_platform_pixels(self):
        expected = {"tiktok": (192, 108, 384, 0), "reels": (192, 130, 384, 0), "shorts": (154, 130, 346, 0), "feed": (54, 54, 108, 54), "youtube": (54, 96, 108, 96)}
        for name, values in expected.items():
            with self.subTest(name=name):
                zone = adspec.safe_zone(name)
                self.assertEqual((zone["top"], zone["right"], zone["bottom"], zone["left"]), values)
        self.assertEqual(adspec.safe_zone("tiktok", 540, 960)["box"], (0, 96, 486, 768))
        with self.assertRaises(adspec.AdSpecError): adspec.safe_zone("tiktok", 0, 1)


try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


@unittest.skipUnless(HAVE_PIL, "Pillow not installed")
class TestFitAspect(unittest.TestCase):
    """Every panel must leave the slicer at one exact shape.

    Autotrim removes whatever border the model drew and it draws a different one per
    cell, so panels come out at slightly different aspects. That is not cosmetic: the
    video model takes its output geometry from the first frame, so panels at 0.556 and
    0.634 came back as 768x1376 and 768x1216 clips from a single 768P run — the ad's
    framing jumped between shots and concat had to re-encode instead of stream-copying.
    """

    SIZES = ((959, 1724), (963, 1518), (849, 1518), (1920, 1080), (1000, 1000),
             (2048, 2048), (101, 997))

    def test_always_a_crop_within_one_pixel_of_the_target(self):
        checked = 0
        for width, height in self.SIZES:
            for aspect in storyboard.SUPPORTED_ASPECTS:
                out = storyboard.fit_aspect(Image.new("RGB", (width, height)), aspect)
                tw, th = (float(n) for n in aspect.split(":"))
                target = tw / th
                ow, oh = out.size
                where = f"({width}x{height}) -> {aspect} gave {out.size}"
                # Never scales up: a panel is only ever trimmed.
                self.assertLessEqual(ow, width, where)
                self.assertLessEqual(oh, height, where)
                # Integer sizes cannot always hit the ratio exactly; one pixel is the bound.
                self.assertLessEqual(min(abs(oh - ow / target), abs(ow - oh * target)),
                                     1.0, where)
                checked += 1
        self.assertEqual(checked, len(self.SIZES) * len(storyboard.SUPPORTED_ASPECTS))

    def test_exact_input_is_returned_untouched(self):
        image = Image.new("RGB", (1080, 1920))
        self.assertIs(storyboard.fit_aspect(image, "9:16"), image)

    def test_too_wide_trims_the_sides_and_keeps_full_height(self):
        out = storyboard.fit_aspect(Image.new("RGB", (963, 1518)), "9:16")
        self.assertEqual(out.size[1], 1518)
        self.assertLess(out.size[0], 963)

    def test_too_tall_trims_top_and_bottom_and_keeps_full_width(self):
        out = storyboard.fit_aspect(Image.new("RGB", (959, 1724)), "9:16")
        self.assertEqual(out.size[0], 959)
        self.assertLess(out.size[1], 1724)

    def test_the_regression_pair_ends_up_the_same_shape(self):
        """0.556 and 0.634 were the two panels that produced mismatched clips."""
        a = storyboard.fit_aspect(Image.new("RGB", (959, 1724)), "9:16")
        b = storyboard.fit_aspect(Image.new("RGB", (963, 1518)), "9:16")
        self.assertAlmostEqual(a.size[0] / a.size[1], b.size[0] / b.size[1], places=3)


class TestRetimeCues(unittest.TestCase):
    """Cues are planned against requested durations; clips come back longer.

    H3 answers a 6s request with 6.583s, so a five-beat ad planned at 30s arrives at
    ~32.9s. Left alone, every cue after the first drifts against the footage it belongs
    to and the CTA caption lands on the previous shot.
    """

    BEATS = [
        {"id": "hook", "index": 0, "start": 0.0, "end": 6.0, "duration": 6},
        {"id": "problem", "index": 1, "start": 6.0, "end": 12.0, "duration": 6},
        {"id": "solution", "index": 2, "start": 12.0, "end": 19.0, "duration": 7},
        {"id": "proof", "index": 3, "start": 19.0, "end": 25.0, "duration": 6},
        {"id": "cta", "index": 4, "start": 25.0, "end": 30.0, "duration": 5},
    ]
    CUES = [
        {"start": 0.0, "end": 6.0, "role": "hook", "text": "a"},
        {"start": 6.0, "end": 12.0, "role": "caption", "text": "b"},
        {"start": 12.0, "end": 19.0, "role": "caption", "text": "c"},
        {"start": 19.0, "end": 25.0, "role": "caption", "text": "d"},
        {"start": 25.0, "end": 30.0, "role": "cta", "text": "e"},
        {"start": 0.0, "end": 30.0, "role": "disclosure", "text": "AI-generated"},
    ]
    ACTUAL = [6.583, 6.583, 7.583, 6.583, 5.583]

    def test_each_cue_lands_on_its_own_real_beat_window(self):
        out = adspec.retime_cues(self.CUES, self.BEATS, self.ACTUAL)
        edges, run = [0.0], 0.0
        for seconds in self.ACTUAL:
            run += seconds
            edges.append(round(run, 3))
        for index in range(5):
            self.assertAlmostEqual(out[index]["start"], edges[index], places=2)
            self.assertAlmostEqual(out[index]["end"], edges[index + 1], places=2)

    def test_disclosure_spans_the_whole_real_film(self):
        out = adspec.retime_cues(self.CUES, self.BEATS, self.ACTUAL)
        disclosure = next(c for c in out if c["role"] == "disclosure")
        self.assertEqual(disclosure["start"], 0.0)
        self.assertAlmostEqual(disclosure["end"], sum(self.ACTUAL), places=2)

    def test_cta_moves_later_not_earlier(self):
        """The regression: a CTA left at 25.0s captions the proof shot, not the CTA shot."""
        out = adspec.retime_cues(self.CUES, self.BEATS, self.ACTUAL)
        cta = next(c for c in out if c["role"] == "cta")
        self.assertGreater(cta["start"], 27.0)
        self.assertLess(abs(cta["end"] - sum(self.ACTUAL)), 0.01)

    def test_identity_when_clips_match_the_plan(self):
        out = adspec.retime_cues(self.CUES, self.BEATS, [6, 6, 7, 6, 5])
        for before, after in zip(self.CUES, out):
            self.assertAlmostEqual(before["start"], after["start"], places=6)
            self.assertAlmostEqual(before["end"], after["end"], places=6)

    def test_shorter_clips_pull_cues_earlier(self):
        out = adspec.retime_cues(self.CUES, self.BEATS, [5, 5, 6, 5, 4])
        self.assertAlmostEqual(out[-1]["end"], 25.0, places=2)

    def test_input_not_mutated(self):
        adspec.retime_cues(self.CUES, self.BEATS, self.ACTUAL)
        self.assertEqual(self.CUES[4]["start"], 25.0)

    def test_count_mismatch_raises(self):
        with self.assertRaises(adspec.AdSpecError) as ctx:
            adspec.retime_cues(self.CUES, self.BEATS, [6.0, 6.0])
        self.assertIn("2 clip duration", str(ctx.exception))

    def test_empty_inputs_pass_through(self):
        self.assertEqual(adspec.retime_cues(self.CUES, [], []), self.CUES)

    def test_never_produces_a_zero_length_cue(self):
        out = adspec.retime_cues(self.CUES, self.BEATS, self.ACTUAL)
        for cue in out:
            self.assertGreater(cue["end"], cue["start"])


class TestBoardAspect(unittest.TestCase):
    """The contact sheet is a GRID of clips, so it must be requested at the grid's
    aspect, not the clip's.

    Passing the clip aspect straight through is correct only when cols == rows, which is
    why a 2x2 sheet of 16:9 shots was right for months and the first 3x2 sheet of 9:16
    shots was not: it asked for a 9:16 sheet, so every cell was drawn at 0.37 against a
    0.56 target and the model composed for a sliver.
    """

    def _spec(self, aspect, cols, rows, shots=6):
        return {
            "version": 1, "aspect": aspect,
            "board": {"cols": cols, "rows": rows},
            "shots": [{"id": f"s{i}", "panel": "x", "action": "y"} for i in range(shots)],
        }

    def test_square_grid_preserves_the_cell_aspect_exactly(self):
        # This is the property worth knowing when authoring: a square grid is always
        # exact, whatever the clip ratio.
        for aspect, cols in (("16:9", 2), ("9:16", 2), ("9:16", 3), ("1:1", 3), ("21:9", 2)):
            chosen, error = storyboard.board_aspect(
                self._spec(aspect, cols, cols, shots=cols * cols))
            self.assertEqual(chosen, aspect, f"{cols}x{cols} of {aspect}")
            self.assertAlmostEqual(error, 0.0, places=9, msg=f"{cols}x{cols} of {aspect}")

    def test_non_square_grid_snaps_to_the_nearest_offered_ratio(self):
        chosen, error = storyboard.board_aspect(self._spec("9:16", 3, 2))
        self.assertEqual(chosen, "1:1")          # 27:32 = 0.844, nearest offered is 1:1
        self.assertGreater(error, 0.12)          # and the caller is expected to warn

    def test_the_regression_case_is_no_longer_silent(self):
        """3x2 of 9:16 used to be requested AS 9:16, drawing 0.37 cells."""
        chosen, _ = storyboard.board_aspect(self._spec("9:16", 3, 2))
        self.assertNotEqual(chosen, "9:16")

    def test_existing_two_by_two_sixteen_nine_is_unchanged(self):
        chosen, error = storyboard.board_aspect(self._spec("16:9", 2, 2, shots=4))
        self.assertEqual(chosen, "16:9")
        self.assertAlmostEqual(error, 0.0, places=9)

    def test_explicit_board_aspect_wins(self):
        spec = self._spec("9:16", 3, 2)
        spec["board"]["aspect"] = "2:3"
        chosen, _ = storyboard.board_aspect(spec)
        self.assertEqual(chosen, "2:3")

    def test_board_aspect_is_a_known_board_key(self):
        spec = self._spec("9:16", 3, 3, shots=9)
        spec["board"]["aspect"] = "1:1"
        storyboard.validate_spec(spec)  # must not raise on the unknown-key check

    def test_every_choice_comes_from_the_supported_menu(self):
        for aspect in storyboard.SUPPORTED_ASPECTS:
            for cols in range(1, 5):
                for rows in range(1, 5):
                    chosen, _ = storyboard.board_aspect(
                        self._spec(aspect, cols, rows, shots=cols * rows))
                    self.assertIn(chosen, storyboard.SUPPORTED_ASPECTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
