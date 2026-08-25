#!/usr/bin/env python3
"""Tests for transition timing, catalogues, failure diagnostics and real FFmpeg output.

The exhaustive expression smoke test is opt-in because it renders 106 files. Run it with
``TRANSITIONS_FULL_SWEEP=1 python3 -m unittest scripts.test_transitions.FullSweepTests``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import transitions as tr
from make_fixtures import make


HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _clips(*durations: float, transitions: list[tr.Transition] | None = None) -> list[tr.Clip]:
    transitions = transitions or [tr.resolve("fade") for _ in durations[1:]]
    return [tr.Clip(Path("clip0.mp4"), durations[0])] + [
        tr.Clip(Path(f"clip{index}.mp4"), duration, transitions[index - 1])
        for index, duration in enumerate(durations[1:], 1)
    ]


def _stream_duration(path: Path, selector: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", selector,
         "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip().splitlines()[0])


# ─── Catalogue and resolution ────────────────────────────────

class CatalogueTests(unittest.TestCase):
    def test_eased_count(self):
        self.assertEqual(len(tr.eased()), 106)

    def test_gl_count(self):
        self.assertEqual(sum(name.startswith("GL_") for name in tr.eased()), 50)

    def test_easing_count(self):
        self.assertEqual(len(tr.easings()), 43)

    def test_transition_expressions_are_nonempty_and_quote_safe(self):
        self.assertTrue(all(expr and "'" not in expr for expr in tr.eased().values()))

    def test_transition_expressions_read_eased_progress(self):
        self.assertTrue(all("ld(0)" in expr for expr in tr.eased().values()))

    def test_easings_assign_progress(self):
        self.assertTrue(all("st(0," in expr for expr in tr.easings().values()))

    def test_available_eased_is_sorted(self):
        self.assertEqual(tr.available("eased"), sorted(tr.available("eased")))

    def test_available_gl_is_subset(self):
        self.assertEqual(len(tr.available("gl")), 50)
        self.assertTrue(all(name.startswith("GL_") for name in tr.available("gl")))

    def test_available_native_has_fade(self):
        self.assertIn("fade", tr.available("native"))

    def test_available_rejects_bad_kind(self):
        with self.assertRaises(tr.TransitionError):
            tr.available("fast")

    def test_native_constant_includes_custom(self):
        self.assertIn("custom", tr.NATIVE)

    def test_resolve_native_fade(self):
        transition = tr.resolve("fade")
        self.assertEqual(transition.native, "fade")
        self.assertFalse(transition.needs_single_thread)

    def test_resolve_native_case_insensitive(self):
        self.assertEqual(tr.resolve("WiPeLeFt").native, "wipeleft")

    def test_resolve_eased_fade(self):
        transition = tr.resolve("fade", easing="cubic-in-out")
        self.assertIsNone(transition.native)
        self.assertTrue(transition.needs_single_thread)
        self.assertEqual(transition.easing, "CUBIC-IN-OUT")

    def test_easing_underscore_alias(self):
        self.assertEqual(
            tr.resolve("fade", easing="cubic_in_out").expr,
            tr.resolve("fade", easing="cubic-in-out").expr,
        )

    def test_gl_name_aliases(self):
        expressions = {tr.resolve(name).expr for name in ("GL_DOORWAY", "gl-doorway", "gl_doorway")}
        self.assertEqual(len(expressions), 1)

    def test_default_expression_easing_is_linear(self):
        self.assertEqual(tr.resolve("GL_SWIRL").easing, "LINEAR")

    def test_native_args(self):
        self.assertEqual(tr.resolve("fade").args(), "transition=fade")

    def test_expression_args_are_single_quoted(self):
        args = tr.resolve("GL_DOORWAY").args()
        self.assertTrue(args.startswith("transition=custom:expr='st(0,P);"))
        self.assertTrue(args.endswith("'"))

    def test_unknown_transition_has_close_match(self):
        with self.assertRaises(tr.TransitionError) as ctx:
            tr.resolve("fdae")
        self.assertIn("fade", str(ctx.exception).lower())

    def test_unknown_easing_has_close_match(self):
        with self.assertRaises(tr.TransitionError) as ctx:
            tr.resolve("fade", easing="cubik-in-out")
        self.assertIn("CUBIC-IN-OUT", str(ctx.exception))

    def test_hblur_cannot_be_eased(self):
        with self.assertRaises(tr.TransitionError) as ctx:
            tr.resolve("hblur", easing="bounce-out")
        self.assertIn("cannot be eased", str(ctx.exception))

    def test_distance_cannot_be_eased(self):
        with self.assertRaises(tr.TransitionError) as ctx:
            tr.resolve("distance", easing="linear")
        self.assertIn("transitions that can be eased", str(ctx.exception))

    def test_zero_duration_rejected(self):
        with self.assertRaises(tr.TransitionError):
            tr.resolve("fade", 0)

    def test_negative_duration_rejected(self):
        with self.assertRaises(tr.TransitionError):
            tr.resolve("fade", -0.1)

    def test_tiny_duration_clamped_with_warning(self):
        with self.assertWarns(RuntimeWarning) as caught:
            transition = tr.resolve("fade", 0.01)
        self.assertEqual(transition.duration, tr.MIN_DURATION)
        self.assertIn("clamped", str(caught.warning))

    def test_missing_vendor_names_path(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tr, "VENDOR", Path(tmp) / "gone"):
            tr.eased.cache_clear()
            try:
                with self.assertRaises(tr.TransitionError) as ctx:
                    tr.resolve("GL_DOORWAY")
                self.assertIn(str(Path(tmp) / "gone"), str(ctx.exception))
            finally:
                tr.eased.cache_clear()

    def test_parse_rejects_dangling_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.txt"
            path.write_text("NAME\n")
            with self.assertRaises(tr.TransitionError):
                tr._parse_expr_file(path)

    def test_probe_native_none_without_ffmpeg(self):
        with mock.patch.object(tr.shutil, "which", return_value=None):
            self.assertIsNone(tr.probe_native())

    def test_probe_native_parses_help(self):
        result = subprocess.CompletedProcess([], 0, "     fade            0  flags\n     newer_fx       58  flags\n", "")
        with mock.patch.object(tr.shutil, "which", return_value="/ffmpeg"), \
             mock.patch.object(tr.subprocess, "run", return_value=result):
            self.assertEqual(tr.probe_native(), ("fade", "newer_fx"))

    def test_probe_failure_falls_back(self):
        result = subprocess.CompletedProcess([], 1, "", "bad")
        with mock.patch.object(tr.shutil, "which", return_value="/ffmpeg"), \
             mock.patch.object(tr.subprocess, "run", return_value=result):
            self.assertEqual(tr.probe_native(), tr.NATIVE)


# ─── Chain planning ────────────────────────────────────

class ChainTests(unittest.TestCase):
    def test_three_clip_total_duration(self):
        clips = _clips(3, 3, 3, transitions=[tr.resolve("fade", .5), tr.resolve("wipeleft", .8)])
        self.assertAlmostEqual(tr.total_duration(clips), 7.7)

    def test_filter_has_two_xfades(self):
        graph = tr.chain_filter(_clips(3, 3, 3), audio=False)
        self.assertEqual(graph.count("xfade="), 2)

    def test_offsets_are_literal_expected_values(self):
        clips = _clips(3, 3, 3, transitions=[tr.resolve("fade", .5), tr.resolve("wipeleft", .8)])
        graph = tr.chain_filter(clips, audio=False)
        self.assertIn("offset=2.5", graph)
        self.assertIn("offset=4.7", graph)

    def test_audio_graph_has_two_acrossfades(self):
        with mock.patch.object(tr, "_has_audio", return_value=True):
            graph = tr.chain_filter(_clips(3, 3, 3), audio=True)
        self.assertEqual(graph.count("acrossfade="), 2)
        self.assertIn("[aout]", graph)

    def test_audio_false_never_probes_audio(self):
        with mock.patch.object(tr, "_has_audio") as probe:
            tr.chain_filter(_clips(2, 2), audio=False)
        probe.assert_not_called()

    def test_audio_degrades_with_warning(self):
        with mock.patch.object(tr, "_has_audio", side_effect=[True, False]):
            with self.assertWarnsRegex(RuntimeWarning, "audio omitted"):
                graph = tr.chain_filter(_clips(2, 2), audio=True)
        self.assertNotIn("acrossfade", graph)

    def test_single_clip_passthrough(self):
        graph = tr.chain_filter([tr.Clip(Path("one.mp4"), 3)], audio=False)
        self.assertEqual(graph, "[0:v]null[vout]")
        self.assertNotIn("xfade", graph)

    def test_single_clip_duration(self):
        self.assertEqual(tr.total_duration([tr.Clip(Path("one.mp4"), 2.25)]), 2.25)

    def test_custom_labels(self):
        graph = tr.chain_filter(_clips(2, 2), audio=False, video_label="film")
        self.assertTrue(graph.endswith("[film]"))

    def test_zero_clips_rejected(self):
        with self.assertRaisesRegex(tr.TransitionError, "no clips"):
            tr.chain_filter([])

    def test_missing_transition_rejected(self):
        with self.assertRaisesRegex(tr.TransitionError, "index 1"):
            tr.total_duration([tr.Clip(Path("a"), 2), tr.Clip(Path("b"), 2)])

    def test_transition_longer_than_next_clip(self):
        clips = _clips(2, .2, transitions=[tr.resolve("fade", .5)])
        with self.assertRaises(tr.TransitionError) as ctx:
            tr.chain_filter(clips, audio=False)
        self.assertIn("index 1", str(ctx.exception))
        self.assertIn("2", str(ctx.exception))
        self.assertIn("0.2", str(ctx.exception))

    def test_transition_longer_than_previous_clip(self):
        clips = _clips(.2, 2, transitions=[tr.resolve("fade", .5)])
        with self.assertRaisesRegex(tr.TransitionError, "index 1"):
            tr.total_duration(clips)

    def test_transition_longer_than_accumulated_head(self):
        clips = _clips(.1, .1, 1, transitions=[tr.resolve("fade", .1), tr.resolve("fade", .2)])
        with self.assertRaises(tr.TransitionError) as ctx:
            tr.total_duration(clips)
        self.assertIn("accumulated head", str(ctx.exception))

    def test_first_transition_warns_in_validate(self):
        clips = _clips(2, 2)
        clips[0].transition = tr.resolve("fade")
        self.assertTrue(any("index 0" in problem and "ignored" in problem for problem in tr.validate(clips)))

    def test_validate_reports_invalid_clip_duration(self):
        problems = tr.validate([tr.Clip(Path("bad"), 0)])
        self.assertTrue(any("index 0" in problem for problem in problems))

    def test_validate_clear_for_unprobeable_valid_chain(self):
        with mock.patch.object(tr, "_probe_video", return_value=None):
            self.assertEqual(tr.validate(_clips(2, 2)), [])

    def test_validate_reports_size_mismatch_and_fix(self):
        with mock.patch.object(tr, "_probe_video", side_effect=[((100, 100), 30), ((200, 100), 30)]):
            problems = tr.validate(_clips(2, 2))
        self.assertTrue(any("storyboard.fit_aspect" in problem for problem in problems))

    def test_validate_warns_mixed_frame_rates(self):
        with mock.patch.object(tr, "_probe_video", side_effect=[((100, 100), 24), ((100, 100), 30)]):
            problems = tr.validate(_clips(2, 2))
        self.assertTrue(any("frame rate" in problem for problem in problems))

    def test_native_chain_args_are_threaded_normally(self):
        with mock.patch.object(tr, "_has_audio", return_value=False), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            args = tr.chain_args(_clips(2, 2), Path("out.mp4"), audio=False)
        self.assertNotIn("-filter_complex_threads", args)

    def test_expression_chain_args_force_single_thread(self):
        clips = _clips(2, 2, transitions=[tr.resolve("GL_DOORWAY")])
        args = tr.chain_args(clips, Path("out.mp4"), audio=False)
        index = args.index("-filter_complex_threads")
        self.assertEqual(args[index + 1], "1")

    def test_chain_args_adds_each_input(self):
        args = tr.chain_args(_clips(2, 2, 2), Path("out.mp4"), audio=False)
        self.assertEqual(args.count("-i"), 3)

    def test_chain_args_extra_precedes_output(self):
        args = tr.chain_args(_clips(2, 2), Path("out.mp4"), audio=False, extra=["-movflags", "+faststart"])
        self.assertEqual(args[-3:], ["-movflags", "+faststart", "out.mp4"])

    def test_measure_none_without_ffprobe(self):
        with mock.patch.object(tr.shutil, "which", return_value=None):
            self.assertIsNone(tr.measure(Path("missing")))

    def test_render_missing_ffmpeg_has_install_fix(self):
        with mock.patch.object(tr.shutil, "which", return_value=None):
            with self.assertRaises(tr.TransitionError) as ctx:
                tr.render(_clips(2, 2), Path("out.mp4"), audio=False)
        self.assertIn("brew install ffmpeg", str(ctx.exception))

    def test_render_failure_tails_stderr(self):
        failed = subprocess.CompletedProcess([], 9, "", "one\ntwo")
        with mock.patch.object(tr.shutil, "which", return_value="/ffmpeg"), \
             mock.patch.object(tr, "chain_args", return_value=[]), \
             mock.patch.object(tr.subprocess, "run", return_value=failed):
            with self.assertRaises(tr.TransitionError) as ctx:
                tr.render(_clips(2, 2), Path("out.mp4"), audio=False)
        self.assertIn("exit 9", str(ctx.exception))
        self.assertIn("two", str(ctx.exception))


# ─── Real FFmpeg integration ────────────────────────────────────

@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _fixture_clips(self, audio: bool = True) -> list[tr.Clip]:
        paths = make(self.directory / ("audio" if audio else "silent"), audio=audio)
        return [
            tr.Clip(paths[0], 3),
            tr.Clip(paths[1], 3, tr.resolve("fade", .5)),
            tr.Clip(paths[2], 3, tr.resolve("wipeleft", .8)),
        ]

    def test_native_chain_duration_matches_prediction(self):
        clips = self._fixture_clips(audio=False)
        out = tr.render(clips, self.directory / "native.mp4", audio=False)
        self.assertAlmostEqual(tr.measure(out), tr.total_duration(clips), delta=tr.FRAME_TOLERANCE)

    def test_eased_gl_chain_renders_and_matches(self):
        clips = self._fixture_clips(audio=False)
        clips[1].transition = tr.resolve("GL_DOORWAY", .5, "CUBIC-IN-OUT")
        out = tr.render(clips, self.directory / "gl.mp4", audio=False)
        self.assertAlmostEqual(tr.measure(out), tr.total_duration(clips), delta=tr.FRAME_TOLERANCE)

    def test_audio_chain_has_audio_and_matching_duration(self):
        clips = self._fixture_clips(audio=True)
        out = tr.render(clips, self.directory / "audio.mp4", audio=True)
        self.assertTrue(tr._has_audio(out))
        self.assertAlmostEqual(_stream_duration(out, "a:0"), tr.total_duration(clips), delta=tr.FRAME_TOLERANCE)

    def test_audio_request_degrades_for_silent_clips(self):
        clips = self._fixture_clips(audio=False)
        with self.assertWarnsRegex(RuntimeWarning, "audio omitted"):
            out = tr.render(clips, self.directory / "degraded.mp4", audio=True)
        self.assertFalse(tr._has_audio(out))
        self.assertAlmostEqual(tr.measure(out), tr.total_duration(clips), delta=tr.FRAME_TOLERANCE)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
@unittest.skipUnless(os.environ.get("TRANSITIONS_FULL_SWEEP"), "set TRANSITIONS_FULL_SWEEP=1")
class FullSweepTests(unittest.TestCase):
    def test_every_vendored_expression_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            paths = make(directory / "clips", n=2, seconds=1, w=192, h=192, audio=False)
            failures: list[str] = []
            for name in tr.eased():
                clips = [tr.Clip(paths[0], 1), tr.Clip(paths[1], 1, tr.resolve(name, .2))]
                try:
                    tr.render(clips, directory / f"{name}.mp4", audio=False)
                except tr.TransitionError:
                    failures.append(name)
            self.assertEqual(failures, [])


class TestSizeNormalisation(unittest.TestCase):
    """xfade REFUSES mismatched sizes where concat merely re-encodes them.

    H3 returns four different heights across one five-shot ad, so a spec that assembled
    fine with hard cuts would break the moment a transition was added. The chain scales
    and centre-crops to the first clip's size instead — cropped, never padded, because
    padding puts black bars inside the frame and the transition animates them.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _clip(self, name, w, h, seconds=2):
        path = self.dir / name
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r=30",
                        "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        str(path)], check=True)
        return path

    def test_matching_sizes_emit_no_normalisation(self):
        clips = [tr.Clip(path=Path("a.mp4"), duration=2.0),
                 tr.Clip(path=Path("b.mp4"), duration=2.0,
                                  transition=tr.resolve("fade", 0.4))]
        chain = tr.chain_filter(clips, audio=False, normalise=False)
        self.assertNotIn("force_original_aspect_ratio", chain)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "ffmpeg not installed")
    def test_mismatched_sizes_render_instead_of_failing(self):
        a = self._clip("a.mp4", 384, 640)
        b = self._clip("b.mp4", 384, 560)
        c = self._clip("c.mp4", 384, 600)
        clips = [
            tr.Clip(path=a, duration=tr.measure(a)),
            tr.Clip(path=b, duration=tr.measure(b),
                             transition=tr.resolve("fade", 0.4)),
            tr.Clip(path=c, duration=tr.measure(c),
                             transition=tr.resolve("circleopen", 0.3)),
        ]
        self.assertTrue(any("differs" in p for p in tr.validate(clips)))
        out = self.dir / "out.mp4"
        tr.render(clips, out, audio=False)
        self.assertAlmostEqual(tr.measure(out),
                               tr.total_duration(clips), delta=0.1)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True).stdout.strip()
        self.assertEqual(probe.rstrip(","), "384,640")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "ffmpeg not installed")
    def test_normalise_false_still_fails_loudly(self):
        a = self._clip("a.mp4", 384, 640)
        b = self._clip("b.mp4", 384, 560)
        clips = [
            tr.Clip(path=a, duration=tr.measure(a)),
            tr.Clip(path=b, duration=tr.measure(b),
                             transition=tr.resolve("fade", 0.4)),
        ]
        chain = tr.chain_filter(clips, audio=False, normalise=False)
        self.assertNotIn("force_original_aspect_ratio", chain)


if __name__ == "__main__":
    unittest.main()
