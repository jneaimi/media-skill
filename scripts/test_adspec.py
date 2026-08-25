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


if __name__ == "__main__":
    unittest.main(verbosity=2)
