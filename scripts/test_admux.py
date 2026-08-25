#!/usr/bin/env python3
"""Tests for admux.py.

Stdlib only and fully offline. ffmpeg-backed tests synthesise their own media and
skip when ffmpeg/ffprobe are absent. Run with plain `python3 scripts/test_admux.py`
(no uv, no deps, no network).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).parent))

import admux as am


HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _meta(path: Path) -> dict:
    dest = path.with_suffix(path.suffix + ".meta.json")
    return json.loads(dest.read_text())


# ─── variant_plan (the cost function — test hardest) ─────────

class TestVariantPlan(unittest.TestCase):
    def test_empty_clip_ids_raises_naming_the_list(self):
        with self.assertRaises(am.MuxError) as ctx:
            am.variant_plan([], 3)
        self.assertNotIsInstance(ctx.exception, ValueError)
        msg = str(ctx.exception)
        self.assertIn("clip_ids", msg)
        self.assertIn("empty", msg.lower())

    def test_variants_zero_raises_naming_the_value(self):
        with self.assertRaises(am.MuxError) as ctx:
            am.variant_plan(["a"], 0)
        self.assertIn("variants=0", str(ctx.exception))

    def test_variants_negative_raises_naming_the_value(self):
        with self.assertRaises(am.MuxError) as ctx:
            am.variant_plan(["a"], -2)
        self.assertIn("variants=-2", str(ctx.exception))
        self.assertIsInstance(ctx.exception, am.MuxError)

    def test_hook_index_out_of_range_names_index_and_valid_range(self):
        with self.assertRaises(am.MuxError) as ctx:
            am.variant_plan(["a", "b"], 2, hook_index=5)
        msg = str(ctx.exception)
        self.assertIn("hook_index=5", msg)
        self.assertIn("0..1", msg)

    def test_negative_hook_index_is_out_of_range(self):
        with self.assertRaises(am.MuxError) as ctx:
            am.variant_plan(["a", "b"], 2, hook_index=-1)
        self.assertIn("hook_index=-1", str(ctx.exception))
        self.assertIn("0..1", str(ctx.exception))

    def test_four_variants_of_three_clips_saves_six(self):
        plan = am.variant_plan(["hook", "body", "cta"], 4)
        self.assertEqual(plan["clips_to_render"], 6)
        self.assertEqual(plan["clips_if_naive"], 12)
        self.assertEqual(plan["clips_saved"], 6)
        self.assertEqual(plan["base_clips"], ["body", "cta"])
        self.assertEqual(plan["hook_id"], "hook")
        self.assertEqual(plan["hook_index"], 0)

    def test_one_variant_saves_nothing(self):
        plan = am.variant_plan(["a", "b"], 1)
        self.assertEqual(plan["clips_to_render"], 2)
        self.assertEqual(plan["clips_saved"], 0)
        self.assertEqual(plan["clips_if_naive"], 2)
        self.assertEqual(len(plan["variants"]), 1)

    def test_hook_index_last_on_three_clip_ad(self):
        plan = am.variant_plan(["a", "b", "c"], 2, hook_index=2)
        self.assertEqual(plan["base_clips"], ["a", "b"])
        self.assertEqual(plan["hook_id"], "c")
        self.assertEqual(plan["hook_index"], 2)
        self.assertEqual(plan["variants"][0]["id"], "c-v1")
        self.assertEqual(plan["variants"][0]["clips"], ["a", "b", "c-v1"])
        self.assertEqual(plan["variants"][1]["clips"], ["a", "b", "c-v2"])

    def test_single_clip_ad_three_variants(self):
        plan = am.variant_plan(["hook"], 3)
        self.assertEqual(plan["base_clips"], [])
        self.assertEqual(plan["clips_to_render"], 3)
        self.assertEqual(plan["clips_if_naive"], 3)
        self.assertEqual(plan["clips_saved"], 0)
        self.assertEqual(plan["variants"][0]["clips"], ["hook-v1"])
        self.assertEqual(plan["variants"][2]["id"], "hook-v3")

    def test_exact_dict_five_clips_six_variants(self):
        plan = am.variant_plan(["hook", "problem", "solution", "proof", "cta"], 6)
        expected_variants = [
            {
                "n": n,
                "id": f"hook-v{n}",
                "clips": [f"hook-v{n}", "problem", "solution", "proof", "cta"],
            }
            for n in range(1, 7)
        ]
        self.assertEqual(plan, {
            "base_clips": ["problem", "solution", "proof", "cta"],
            "hook_id": "hook",
            "hook_index": 0,
            "variants": expected_variants,
            "clips_to_render": 10,
            "clips_if_naive": 30,
            "clips_saved": 20,
        })

    def test_hook_index_zero_ids_and_clips(self):
        plan = am.variant_plan(["hook", "body", "cta"], 2, hook_index=0)
        self.assertEqual(plan["variants"][0]["id"], "hook-v1")
        self.assertEqual(plan["variants"][1]["id"], "hook-v2")
        self.assertEqual(plan["variants"][0]["n"], 1)
        self.assertEqual(plan["variants"][0]["clips"], ["hook-v1", "body", "cta"])
        self.assertEqual(plan["base_clips"], ["body", "cta"])

    def test_hook_index_middle_ids_and_clips(self):
        plan = am.variant_plan(["hook", "problem", "solution", "proof", "cta"], 2, hook_index=2)
        self.assertEqual(plan["hook_id"], "solution")
        self.assertEqual(plan["base_clips"], ["hook", "problem", "proof", "cta"])
        self.assertEqual(
            plan["variants"][0]["clips"],
            ["hook", "problem", "solution-v1", "proof", "cta"],
        )
        self.assertEqual(plan["variants"][1]["id"], "solution-v2")

    def test_hook_index_last_boundary_five_clips(self):
        ids = ["hook", "problem", "solution", "proof", "cta"]
        plan = am.variant_plan(ids, 3, hook_index=4)
        self.assertEqual(plan["hook_id"], "cta")
        self.assertEqual(plan["base_clips"], ["hook", "problem", "solution", "proof"])
        self.assertEqual(
            plan["variants"][2]["clips"],
            ["hook", "problem", "solution", "proof", "cta-v3"],
        )

    def test_sweep_clips_to_render_equals_base_plus_variants(self):
        for n_clips in range(2, 9):
            for n_var in range(1, 11):
                ids = [f"c{i}" for i in range(n_clips)]
                plan = am.variant_plan(ids, n_var)
                self.assertEqual(
                    plan["clips_to_render"],
                    len(plan["base_clips"]) + n_var,
                    f"{n_clips} clips x {n_var} variants",
                )
                self.assertGreaterEqual(plan["clips_saved"], 0)
                self.assertEqual(plan["clips_if_naive"], n_clips * n_var)
                self.assertEqual(len(plan["variants"]), n_var)
                self.assertEqual(len(plan["base_clips"]), n_clips - 1)

    def test_variant_ids_are_one_based(self):
        plan = am.variant_plan(["hook", "cta"], 3)
        self.assertEqual([v["n"] for v in plan["variants"]], [1, 2, 3])
        self.assertEqual([v["id"] for v in plan["variants"]], ["hook-v1", "hook-v2", "hook-v3"])

    def test_each_variant_clips_length_matches_source(self):
        ids = ["a", "b", "c", "d"]
        plan = am.variant_plan(ids, 5, hook_index=1)
        for row in plan["variants"]:
            self.assertEqual(len(row["clips"]), 4)
            self.assertEqual(row["clips"][0], "a")
            self.assertEqual(row["clips"][2], "c")
            self.assertEqual(row["clips"][3], "d")
            self.assertEqual(row["clips"][1], row["id"])

    def test_sad_paths_raise_muxerror_not_valueerror(self):
        cases = [
            ([], 1, {}),
            (["a"], 0, {}),
            (["a", "b"], 1, {"hook_index": 9}),
        ]
        for clip_ids, variants, kw in cases:
            with self.subTest(clip_ids=clip_ids, variants=variants, kw=kw):
                with self.assertRaises(am.MuxError):
                    am.variant_plan(clip_ids, variants, **kw)


class TestVariantFilename(unittest.TestCase):
    def test_ad_hook_v3(self):
        self.assertEqual(am.variant_filename("ad", "hook-v3"), "ad-hook-v3.mp4")

    def test_matches_plan_id(self):
        plan = am.variant_plan(["hook", "cta"], 2)
        self.assertEqual(am.variant_filename("ad", plan["variants"][0]["id"]), "ad-hook-v1.mp4")
        self.assertEqual(am.variant_filename("film", "before-v1"), "film-before-v1.mp4")


class TestLoudnormConstants(unittest.TestCase):
    def test_constants_reproduce_existing_voice_filter(self):
        built = (
            f"loudnorm=I={am.VOICE_TARGET_LUFS:g}"
            f":TP={am.VOICE_TRUE_PEAK:g}"
            f":LRA={am.VOICE_LRA:g}"
        )
        self.assertEqual(built, "loudnorm=I=-16:TP=-1.5:LRA=11")
        self.assertEqual(am.MUSIC_DUCK_DB, -12.0)
        self.assertEqual(am.MUSIC_BED_DB, -20.0)


# ─── sidecar ─────────────────────────────────────────────────

class TestSidecar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_valid_json_at_suffix_plus_meta(self):
        target = self.dir / "out.mp4"
        target.write_bytes(b"x")
        dest = am.sidecar(target, {"encode": "copy", "inputs": ["a"]})
        self.assertEqual(dest, self.dir / "out.mp4.meta.json")
        self.assertTrue(dest.is_file())
        data = json.loads(dest.read_text())
        self.assertEqual(data["tool"], "admux")
        self.assertEqual(data["output"], str(target))
        self.assertEqual(data["encode"], "copy")
        self.assertEqual(data["inputs"], ["a"])

    def test_preserves_caller_keys_and_is_json(self):
        target = self.dir / "clip.wav"
        dest = am.sidecar(target, {"filter_graph": "anull", "n": 3})
        data = json.loads(dest.read_text())
        self.assertEqual(data["filter_graph"], "anull")
        self.assertEqual(data["n"], 3)
        self.assertEqual(dest.name, "clip.wav.meta.json")


# ─── ffmpeg-absent behaviour (monkeypatched both ways) ───────

class TestNoFfmpeg(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_duration_of_none_and_has_audio_false_when_which_is_none(self):
        ghost = self.dir / "ghost.mp4"
        ghost.write_bytes(b"not a video")
        with mock.patch.object(am.shutil, "which", lambda _name: None):
            self.assertIsNone(am.duration_of(ghost))
            self.assertFalse(am.has_audio(ghost))
            self.assertIsNone(am.probe_size(ghost))

    def test_assemble_raises_install_message_when_which_is_none(self):
        clip = self.dir / "01-hook.mp4"
        clip.write_bytes(b"fake")
        with mock.patch.object(am.shutil, "which", lambda _name: None):
            with self.assertRaises(am.MuxError) as ctx:
                am.assemble_variant([clip], self.dir / "out.mp4")
        msg = str(ctx.exception)
        self.assertIn("brew install ffmpeg", msg)
        self.assertIn("ffmpeg not found", msg)

    def test_fit_audio_raises_install_message_when_which_is_none(self):
        audio = self.dir / "tone.m4a"
        audio.write_bytes(b"fake")
        with mock.patch.object(am.shutil, "which", lambda _name: None):
            with self.assertRaises(am.MuxError) as ctx:
                am.fit_audio(audio, 2.0, self.dir / "out.m4a")
        self.assertIn("brew install ffmpeg", str(ctx.exception))

    def test_assemble_does_not_claim_missing_when_which_returns_a_path(self):
        clip = self.dir / "01-hook.mp4"
        clip.write_bytes(b"fake")
        captured = {}

        def fake_run(argv, **_kw):
            captured["argv"] = argv
            class R:
                returncode = 0
                stderr = ""
                stdout = "320x568\n"
            return R()

        def fake_which(name):
            return f"/usr/bin/{name}"

        with mock.patch.object(am.shutil, "which", fake_which), \
             mock.patch.object(am.subprocess, "run", fake_run):
            out = am.assemble_variant([clip], self.dir / "out.mp4")
        self.assertEqual(out, self.dir / "out.mp4")
        self.assertTrue((self.dir / "out.mp4.meta.json").is_file())
        self.assertIn("ffmpeg", captured["argv"][0])
        self.assertNotIn("brew install ffmpeg", json.dumps(captured))


class TestAssembleMissing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_clip_list(self):
        with self.assertRaises(am.MuxError) as ctx:
            am.assemble_variant([], self.dir / "out.mp4")
        self.assertIn("no clips", str(ctx.exception).lower())
        self.assertIn("ad shots", str(ctx.exception))

    def test_lists_every_missing_path_not_just_the_first(self):
        existing = self.dir / "01-hook.mp4"
        existing.write_bytes(b"fake")
        missing_a = self.dir / "02-problem.mp4"
        missing_b = self.dir / "03-cta.mp4"
        with self.assertRaises(am.MuxError) as ctx:
            am.assemble_variant(
                [existing, missing_a, missing_b],
                self.dir / "out.mp4",
            )
        msg = str(ctx.exception)
        self.assertIn("02-problem.mp4", msg)
        self.assertIn("03-cta.mp4", msg)
        self.assertIn("ad shots", msg)
        self.assertIsInstance(ctx.exception, am.MuxError)

    def test_all_missing_still_lists_all(self):
        a = self.dir / "no-a.mp4"
        b = self.dir / "no-b.mp4"
        with self.assertRaises(am.MuxError) as ctx:
            am.assemble_variant([a, b], self.dir / "out.mp4")
        self.assertIn("no-a.mp4", str(ctx.exception))
        self.assertIn("no-b.mp4", str(ctx.exception))


class TestFitAudioSad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.audio = self.dir / "tone.m4a"
        self.audio.write_bytes(b"fake")

    def tearDown(self):
        self.tmp.cleanup()

    def test_target_zero(self):
        with self.assertRaises(am.MuxError) as ctx:
            am.fit_audio(self.audio, 0, self.dir / "out.m4a")
        self.assertIn("target_seconds=0", str(ctx.exception))

    def test_target_negative(self):
        with self.assertRaises(am.MuxError) as ctx:
            am.fit_audio(self.audio, -1.5, self.dir / "out.m4a")
        self.assertIn("target_seconds=-1.5", str(ctx.exception))

    def test_missing_audio_file(self):
        with self.assertRaises(am.MuxError) as ctx:
            am.fit_audio(self.dir / "nope.m4a", 2.0, self.dir / "out.m4a")
        self.assertIn("nope.m4a", str(ctx.exception))

    def test_invalid_mode(self):
        with self.assertRaises(am.MuxError) as ctx:
            am.fit_audio(self.audio, 2.0, self.dir / "out.m4a", mode="stretch")
        self.assertIn("stretch", str(ctx.exception))
        self.assertIsInstance(ctx.exception, am.MuxError)


class TestMixTracksSad(unittest.TestCase):
    def test_neither_voice_nor_music(self):
        with self.assertRaises(am.MuxError) as ctx:
            am.mix_tracks(Path("film.mp4"), Path("out.mp4"))
        msg = str(ctx.exception)
        self.assertIn("voice", msg)
        self.assertIn("music", msg)
        self.assertIsInstance(ctx.exception, am.MuxError)

    def test_missing_video_when_music_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            music = d / "bed.m4a"
            music.write_bytes(b"x")
            with self.assertRaises(am.MuxError) as ctx:
                am.mix_tracks(d / "missing.mp4", d / "out.mp4", music=music)
            self.assertIn("missing.mp4", str(ctx.exception))


class TestMuxVoiceSad(unittest.TestCase):
    def test_missing_video_and_voice_name_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            voice = d / "vo.m4a"
            voice.write_bytes(b"x")
            with self.assertRaises(am.MuxError) as ctx:
                am.mux_voice(d / "no-film.mp4", voice, d / "out.mp4")
            self.assertIn("no-film.mp4", str(ctx.exception))
            film = d / "film.mp4"
            film.write_bytes(b"x")
            with self.assertRaises(am.MuxError) as ctx:
                am.mux_voice(film, d / "no-vo.m4a", d / "out.mp4")
            self.assertIn("no-vo.m4a", str(ctx.exception))


# ─── ffmpeg-backed ───────────────────────────────────────────

def _run(args: list[str]) -> None:
    result = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"fixture ffmpeg failed: {result.stderr[-400:]}")


def make_silent(path: Path, size: str = "320x568", duration: float = 2, color: str = "black") -> Path:
    _run([
        "-f", "lavfi", "-i", f"color=c={color}:s={size}:d={duration}",
        "-pix_fmt", "yuv420p", str(path),
    ])
    return path


def make_with_audio(path: Path, size: str = "320x568", duration: float = 2, color: str = "red") -> Path:
    _run([
        "-f", "lavfi", "-i", f"color=c={color}:s={size}:d={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ])
    return path


def make_tone(path: Path, duration: float = 3, freq: int = 220) -> Path:
    _run([
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
        str(path),
    ])
    return path


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_size_clips_stream_copy_and_duration_sums(self):
        a = make_silent(self.dir / "a.mp4", duration=2)
        b = make_silent(self.dir / "b.mp4", duration=2)
        out = self.dir / "joined.mp4"
        am.assemble_variant([a, b], out)
        self.assertTrue(out.is_file())
        dur = am.duration_of(out)
        self.assertIsNotNone(dur)
        self.assertAlmostEqual(dur, 4.0, delta=0.15)
        meta = _meta(out)
        self.assertEqual(meta["encode"], "copy")
        self.assertEqual(meta["tool"], "admux")
        self.assertIsNone(meta["filter_graph"])

    def test_different_sizes_reencode_to_largest(self):
        small = make_silent(self.dir / "small.mp4", size="320x568", duration=1)
        large = make_silent(self.dir / "large.mp4", size="640x1136", duration=1)
        out = self.dir / "joined.mp4"
        am.assemble_variant([small, large], out)
        self.assertEqual(am.probe_size(out), (640, 1136))
        meta = _meta(out)
        self.assertEqual(meta["encode"], "reencode")
        self.assertIn("scale=640:1136", meta["filter_graph"])

    def test_mux_voice_onto_silent_video_adds_audio(self):
        film = make_silent(self.dir / "film.mp4", duration=2)
        voice = make_tone(self.dir / "vo.m4a", duration=2)
        self.assertFalse(am.has_audio(film))
        out = self.dir / "voiced.mp4"
        am.mux_voice(film, voice, out)
        self.assertTrue(am.has_audio(out))
        self.assertTrue(out.is_file())
        meta = _meta(out)
        self.assertIn("loudnorm=I=-16:TP=-1.5:LRA=11", meta["filter_graph"])
        self.assertFalse(meta["keep_original_fallback"])
        dur = am.duration_of(out)
        self.assertAlmostEqual(dur, 2.0, delta=0.15)

    def test_mux_voice_keep_original_on_silent_falls_back(self):
        film = make_silent(self.dir / "film.mp4", duration=2)
        voice = make_tone(self.dir / "vo.m4a", duration=2)
        out = self.dir / "voiced.mp4"
        am.mux_voice(film, voice, out, keep_original=True)
        self.assertTrue(am.has_audio(out))
        meta = _meta(out)
        self.assertTrue(meta["keep_original_fallback"])
        self.assertTrue(meta["keep_original"])
        self.assertNotIn("[0:a]", meta["filter_graph"])

    def test_mux_voice_keep_original_on_audio_film_mixes(self):
        film = make_with_audio(self.dir / "film.mp4", duration=2)
        voice = make_tone(self.dir / "vo.m4a", duration=2)
        self.assertTrue(am.has_audio(film))
        out = self.dir / "mixed.mp4"
        am.mux_voice(film, voice, out, keep_original=True)
        self.assertTrue(am.has_audio(out))
        meta = _meta(out)
        self.assertFalse(meta["keep_original_fallback"])
        self.assertIn("[0:a]", meta["filter_graph"])
        self.assertIn("amix", meta["filter_graph"])

    def test_fit_audio_pads_3s_to_5s(self):
        tone = make_tone(self.dir / "tone.m4a", duration=3)
        out = self.dir / "padded.m4a"
        am.fit_audio(tone, 5.0, out, mode="pad")
        dur = am.duration_of(out)
        self.assertIsNotNone(dur)
        self.assertAlmostEqual(dur, 5.0, delta=0.15)

    def test_fit_audio_trims_3s_to_2s(self):
        tone = make_tone(self.dir / "tone.m4a", duration=3)
        out = self.dir / "trimmed.m4a"
        am.fit_audio(tone, 2.0, out, mode="pad")
        dur = am.duration_of(out)
        self.assertIsNotNone(dur)
        self.assertAlmostEqual(dur, 2.0, delta=0.15)

    def test_fit_audio_trim_mode_on_long_input(self):
        tone = make_tone(self.dir / "tone.m4a", duration=3)
        out = self.dir / "trimmed.m4a"
        am.fit_audio(tone, 2.0, out, mode="trim")
        self.assertAlmostEqual(am.duration_of(out), 2.0, delta=0.15)

    def test_fit_audio_trim_mode_raises_on_short_input(self):
        tone = make_tone(self.dir / "tone.m4a", duration=3)
        with self.assertRaises(am.MuxError) as ctx:
            am.fit_audio(tone, 5.0, self.dir / "out.m4a", mode="trim")
        msg = str(ctx.exception)
        self.assertIn("shorter", msg.lower())
        self.assertIn("5", msg)
        self.assertIsInstance(ctx.exception, am.MuxError)

    def test_mix_tracks_voice_and_music_produces_audio(self):
        film = make_silent(self.dir / "film.mp4", duration=3)
        voice = make_tone(self.dir / "vo.m4a", duration=2, freq=440)
        music = make_tone(self.dir / "bed.m4a", duration=3, freq=220)
        out = self.dir / "stacked.mp4"
        am.mix_tracks(film, out, voice=voice, music=music, duck=True)
        self.assertTrue(am.has_audio(out))
        meta = _meta(out)
        self.assertIn("sidechaincompress", meta["filter_graph"])
        self.assertIn("asplit", meta["filter_graph"])
        self.assertTrue(meta["duck"])
        self.assertIn(";", meta["filter_graph"])

    def test_mix_tracks_duck_false_omits_sidechain(self):
        film = make_silent(self.dir / "film.mp4", duration=2)
        voice = make_tone(self.dir / "vo.m4a", duration=2, freq=440)
        music = make_tone(self.dir / "bed.m4a", duration=2, freq=220)
        out = self.dir / "flat.mp4"
        am.mix_tracks(film, out, voice=voice, music=music, duck=False)
        self.assertTrue(am.has_audio(out))
        meta = _meta(out)
        self.assertNotIn("sidechaincompress", meta["filter_graph"])
        self.assertIn("amix", meta["filter_graph"])
        self.assertFalse(meta["duck"])

    def test_mix_tracks_music_alone(self):
        film = make_silent(self.dir / "film.mp4", duration=2)
        music = make_tone(self.dir / "bed.m4a", duration=2)
        out = self.dir / "bedded.mp4"
        am.mix_tracks(film, out, music=music)
        self.assertTrue(am.has_audio(out))
        meta = _meta(out)
        self.assertIn(f"volume={am.MUSIC_BED_DB:g}dB", meta["filter_graph"])
        self.assertIsNone(meta["inputs"]["voice"])

    def test_mix_tracks_voice_alone_delegates_to_mux_voice(self):
        film = make_silent(self.dir / "film.mp4", duration=2)
        voice = make_tone(self.dir / "vo.m4a", duration=2)
        out = self.dir / "voiced.mp4"
        am.mix_tracks(film, out, voice=voice)
        self.assertTrue(am.has_audio(out))
        meta = _meta(out)
        self.assertIn("voice", meta["inputs"])
        self.assertNotIn("music", meta["inputs"])

    def test_two_variants_same_dir_unique_concat_lists(self):
        body = make_silent(self.dir / "body.mp4", duration=1, color="blue")
        hook1 = make_silent(self.dir / "hook-v1.mp4", duration=1, color="red")
        hook2 = make_silent(self.dir / "hook-v2.mp4", duration=1, color="green")
        out1 = self.dir / "ad-hook-v1.mp4"
        out2 = self.dir / "ad-hook-v2.mp4"
        am.assemble_variant([hook1, body], out1)
        am.assemble_variant([hook2, body], out2)
        self.assertTrue(out1.is_file())
        self.assertTrue(out2.is_file())
        list1 = self.dir / "concat-ad-hook-v1.txt"
        list2 = self.dir / "concat-ad-hook-v2.txt"
        self.assertTrue(list1.is_file(), "per-variant concat list must survive")
        self.assertTrue(list2.is_file(), "second variant must not clobber the first list")
        self.assertIn("hook-v1", list1.read_text())
        self.assertIn("hook-v2", list2.read_text())
        self.assertNotEqual(list1.read_text(), list2.read_text())
        meta1 = _meta(out1)
        meta2 = _meta(out2)
        self.assertNotEqual(meta1["concat_list"], meta2["concat_list"])
        self.assertAlmostEqual(am.duration_of(out1), 2.0, delta=0.15)
        self.assertAlmostEqual(am.duration_of(out2), 2.0, delta=0.15)

    def test_duration_of_and_has_audio_on_real_files(self):
        silent = make_silent(self.dir / "s.mp4", duration=2)
        voiced = make_with_audio(self.dir / "a.mp4", duration=2)
        self.assertIsNotNone(am.duration_of(silent))
        self.assertAlmostEqual(am.duration_of(silent), 2.0, delta=0.15)
        self.assertFalse(am.has_audio(silent))
        self.assertTrue(am.has_audio(voiced))
        self.assertEqual(am.probe_size(silent), (320, 568))

    def test_run_ffmpeg_non_zero_includes_what_and_stderr_tail(self):
        with self.assertRaises(am.MuxError) as ctx:
            am.run_ffmpeg(["-i", str(self.dir / "does-not-exist.mp4"), str(self.dir / "x.mp4")],
                          "unit-test")
        msg = str(ctx.exception)
        self.assertIn("unit-test", msg)
        self.assertIn("failed", msg.lower())


class TestRunFfmpegAbsent(unittest.TestCase):
    def test_run_ffmpeg_names_what_when_binary_missing(self):
        with mock.patch.object(am.shutil, "which", lambda _name: None):
            with self.assertRaises(am.MuxError) as ctx:
                am.run_ffmpeg(["-version"], "concatenate clips")
        msg = str(ctx.exception)
        self.assertIn("brew install ffmpeg", msg)
        self.assertIn("concatenate clips", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
