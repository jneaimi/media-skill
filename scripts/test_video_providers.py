#!/usr/bin/env python3
"""Tests for video_providers.py and storyboard.py.

Stdlib only and fully offline — urlopen is faked, so nothing here can submit a paid job.
Run with plain `python3 scripts/test_video_providers.py` (no uv, no deps). The Pillow-only
slicing tests skip themselves if Pillow isn't importable.
"""

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import storyboard as sb
import video_providers as vp


# ─── HELPERS ─────────────────────────────────────────────────

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def fake_json_response(payload):
    return FakeResponse(json.dumps(payload).encode())


def capture_request(payload_holder, response):
    """urlopen stand-in that records the outgoing Request and replays `response`."""
    def _urlopen(request, timeout=None):
        payload_holder.append(request)
        if callable(response):
            return response()
        return response
    return _urlopen


def a_request(**overrides):
    base = dict(
        model="hailuo-3", provider="openrouter", prompt="a lighthouse at dusk",
        duration=6, aspect_ratio="16:9", resolution="2K",
    )
    base.update(overrides)
    return vp.VideoRequest(**base)


OR_CAPS = vp.MODEL_REGISTRY["hailuo-3"].caps["openrouter"]
MM_CAPS = vp.MODEL_REGISTRY["hailuo-3"].caps["minimax"]


# ─── ROUTING ─────────────────────────────────────────────────

class TestResolve(unittest.TestCase):
    def test_auto_picks_first_provider(self):
        spec, provider = vp.resolve("hailuo-3", "auto")
        self.assertEqual(provider, "openrouter")
        self.assertEqual(spec.backend_ids["openrouter"], "minimax/hailuo-3")

    def test_explicit_provider(self):
        spec, provider = vp.resolve("hailuo-3", "minimax")
        self.assertEqual(provider, "minimax")
        self.assertEqual(spec.backend_ids["minimax"], "MiniMax-H3")

    def test_gemini_models_still_route_to_gemini(self):
        for model in ("fast", "standard"):
            _, provider = vp.resolve(model, "auto")
            self.assertEqual(provider, "gemini", f"{model} must stay on the Gemini path")

    def test_unknown_model(self):
        with self.assertRaises(vp.VideoProviderError) as ctx:
            vp.resolve("hailuo-9", "auto")
        self.assertIn("unknown model", str(ctx.exception))

    def test_model_not_served_by_provider(self):
        with self.assertRaises(vp.VideoProviderError) as ctx:
            vp.resolve("hailuo-2.3", "minimax")
        self.assertIn("not served by provider", str(ctx.exception))


# ─── VALIDATION (mostly sad path) ────────────────────────────

class TestValidate(unittest.TestCase):
    def test_valid_request_passes(self):
        vp.validate(a_request(), OR_CAPS)

    def test_frames_and_references_are_mutually_exclusive(self):
        request = a_request(first_frame="a.png", references=["ref.png"])
        with self.assertRaises(vp.VideoProviderError) as ctx:
            vp.validate(request, OR_CAPS)
        self.assertIn("cannot be combined", str(ctx.exception))

    def test_duration_outside_supported_set(self):
        # 4s is fine on MiniMax direct but below OpenRouter's floor.
        with self.assertRaises(vp.VideoProviderError) as ctx:
            vp.validate(a_request(duration=4), OR_CAPS)
        self.assertIn("--duration 4", str(ctx.exception))
        vp.validate(a_request(provider="minimax", duration=4, resolution="768P"), MM_CAPS)

    def test_resolution_outside_supported_set(self):
        with self.assertRaises(vp.VideoProviderError) as ctx:
            vp.validate(a_request(resolution="768P"), OR_CAPS)
        self.assertIn("--resolution 768P", str(ctx.exception))

    def test_aspect_outside_supported_set(self):
        with self.assertRaises(vp.VideoProviderError):
            vp.validate(a_request(aspect_ratio="5:4"), OR_CAPS)

    def test_last_frame_rejected_when_model_lacks_it(self):
        caps = vp.MODEL_REGISTRY["hailuo-2.3"].caps["openrouter"]
        request = a_request(model="hailuo-2.3", duration=6, resolution="1080p",
                            last_frame="b.png", generate_audio=False)
        with self.assertRaises(vp.VideoProviderError) as ctx:
            vp.validate(request, caps)
        self.assertIn("last_frame", str(ctx.exception))

    def test_audio_rejected_when_model_lacks_it(self):
        caps = vp.MODEL_REGISTRY["hailuo-2.3"].caps["openrouter"]
        request = a_request(model="hailuo-2.3", duration=6, resolution="1080p")
        with self.assertRaises(vp.VideoProviderError) as ctx:
            vp.validate(request, caps)
        self.assertIn("--no-audio", str(ctx.exception))

    def test_seed_rejected_when_unsupported(self):
        with self.assertRaises(vp.VideoProviderError) as ctx:
            vp.validate(a_request(seed=42), OR_CAPS)
        self.assertIn("seed", str(ctx.exception))

    def test_references_rejected_when_unsupported(self):
        caps = vp.MODEL_REGISTRY["hailuo-2.3"].caps["openrouter"]
        request = a_request(model="hailuo-2.3", duration=6, resolution="1080p",
                            references=["r.png"], generate_audio=False)
        with self.assertRaises(vp.VideoProviderError):
            vp.validate(request, caps)


class TestCost(unittest.TestCase):
    def test_openrouter_2k(self):
        self.assertAlmostEqual(vp.estimate_cost(a_request(duration=6), OR_CAPS), 0.78)

    def test_minimax_768p_is_cheaper(self):
        request = a_request(provider="minimax", duration=4, resolution="768P")
        self.assertAlmostEqual(vp.estimate_cost(request, MM_CAPS), 0.32)

    def test_first_five_reference_images_are_free(self):
        five = a_request(references=[f"{i}.png" for i in range(5)])
        seven = a_request(references=[f"{i}.png" for i in range(7)])
        self.assertAlmostEqual(vp.estimate_cost(five, OR_CAPS), 0.78)
        self.assertAlmostEqual(vp.estimate_cost(seven, OR_CAPS), 0.78 + 2 * 0.04)

    def test_unpriced_model_returns_zero(self):
        caps = vp.MODEL_REGISTRY["fast"].caps["gemini"]
        self.assertEqual(vp.estimate_cost(a_request(model="fast", duration=4), caps), 0.0)


# ─── OPENROUTER TRANSPORT ────────────────────────────────────

class TestOpenRouter(unittest.TestCase):
    def setUp(self):
        self.provider = vp.OpenRouterProvider("test-key")

    def _submit(self, request, response=None):
        sent = []
        response = response or fake_json_response({"id": "job-1", "status": "pending"})
        with mock.patch.object(vp.urllib.request, "urlopen",
                               capture_request(sent, response)):
            job_id = self.provider.submit(request, "minimax/hailuo-3", OR_CAPS)
        return job_id, json.loads(sent[0].data.decode()), sent[0]

    def test_submit_builds_frame_payload(self):
        request = a_request(first_frame="https://x/a.png", last_frame="https://x/b.png")
        job_id, payload, http = self._submit(request)

        self.assertEqual(job_id, "job-1")
        self.assertEqual(payload["model"], "minimax/hailuo-3")
        self.assertEqual(payload["duration"], 6)
        self.assertEqual(payload["resolution"], "2K")
        self.assertEqual(
            [f["frame_type"] for f in payload["frame_images"]],
            ["first_frame", "last_frame"],
        )
        self.assertEqual(payload["frame_images"][0]["image_url"]["url"],
                         "https://x/a.png")
        self.assertNotIn("input_references", payload)
        self.assertEqual(http.get_header("Authorization"), "Bearer test-key")

    def test_submit_builds_reference_payload(self):
        _, payload, _ = self._submit(a_request(references=["https://x/style.png"]))
        self.assertIn("input_references", payload)
        self.assertNotIn("frame_images", payload)
        self.assertNotIn("frame_type", payload["input_references"][0])

    def test_submit_without_job_id_raises(self):
        with self.assertRaises(vp.VideoProviderError) as ctx:
            self._submit(a_request(), fake_json_response({"status": "pending"}))
        self.assertIn("did not return a job id", str(ctx.exception))

    def test_http_error_surfaces_body(self):
        error = urllib.error.HTTPError(
            "u", 400, "Bad Request", {},
            io.BytesIO(b'{"error":"duration 3 not supported"}'),
        )
        def _raise(request, timeout=None):
            raise error
        with mock.patch.object(vp.urllib.request, "urlopen", _raise):
            with self.assertRaises(vp.VideoProviderError) as ctx:
                self.provider.submit(a_request(), "minimax/hailuo-3", OR_CAPS)
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertIn("duration 3 not supported", str(ctx.exception))

    def _poll(self, payload):
        with mock.patch.object(vp.urllib.request, "urlopen",
                               capture_request([], fake_json_response(payload))):
            return self.provider.poll("job-1")

    def test_poll_running(self):
        self.assertEqual(self._poll({"status": "in_progress"})["status"], vp.RUNNING)

    def test_poll_completed(self):
        state = self._poll({
            "status": "completed",
            "unsigned_urls": ["https://openrouter.ai/api/v1/videos/job-1/content?index=0"],
            "usage": {"cost": 0.78},
        })
        self.assertEqual(state["status"], vp.COMPLETED)
        self.assertEqual(state["cost"], 0.78)

    def test_poll_terminal_failures(self):
        for status in ("failed", "cancelled", "expired"):
            state = self._poll({"status": status, "error": "content policy"})
            self.assertEqual(state["status"], vp.FAILED, status)
            self.assertEqual(state["error"], "content policy")

    def test_completed_without_url_raises(self):
        with self.assertRaises(vp.VideoProviderError) as ctx:
            self._poll({"status": "completed", "unsigned_urls": []})
        self.assertIn("no video URL", str(ctx.exception))


# ─── MINIMAX TRANSPORT ───────────────────────────────────────

class TestMiniMax(unittest.TestCase):
    def setUp(self):
        self.provider = vp.MiniMaxProvider("test-key")

    def _submit(self, request, response=None):
        sent = []
        response = response or fake_json_response({"task_id": "task-9"})
        with mock.patch.object(vp.urllib.request, "urlopen",
                               capture_request(sent, response)):
            task_id = self.provider.submit(request, "MiniMax-H3", MM_CAPS)
        return task_id, json.loads(sent[0].data.decode())

    def test_submit_uses_content_roles(self):
        request = a_request(provider="minimax", duration=4, resolution="768P",
                            first_frame="https://x/a.png", last_frame="https://x/b.png")
        task_id, payload = self._submit(request)

        self.assertEqual(task_id, "task-9")
        self.assertEqual(payload["model"], "MiniMax-H3")
        self.assertEqual(payload["resolution"], "768P")
        self.assertEqual(payload["content"][0], {"type": "text", "text": request.prompt})
        self.assertEqual([c.get("role") for c in payload["content"][1:]],
                         ["first_frame", "last_frame"])
        # Image modes derive the ratio from the frames; sending one is rejected upstream.
        self.assertNotIn("ratio", payload)

    def test_text_to_video_sends_ratio(self):
        _, payload = self._submit(a_request(provider="minimax", duration=5,
                                            resolution="2K"))
        self.assertEqual(payload["ratio"], "16:9")

    def test_reference_mode_uses_reference_role(self):
        request = a_request(provider="minimax", duration=5, resolution="2K",
                            references=["https://x/sara.png"])
        _, payload = self._submit(request)
        self.assertEqual(payload["content"][1]["role"], "reference_image")

    def test_submit_without_task_id_surfaces_base_resp(self):
        response = fake_json_response(
            {"base_resp": {"status_code": 1008, "status_msg": "insufficient balance"}}
        )
        with self.assertRaises(vp.VideoProviderError) as ctx:
            self._submit(a_request(provider="minimax", duration=5), response)
        self.assertIn("insufficient balance", str(ctx.exception))

    def _poll(self, task):
        with mock.patch.object(vp.urllib.request, "urlopen",
                               capture_request([], fake_json_response({"task": task}))):
            return self.provider.poll("task-9")

    def test_poll_queued_and_running(self):
        for status in ("queued", "running"):
            self.assertEqual(self._poll({"status": status})["status"], vp.RUNNING)

    def test_poll_succeeded(self):
        state = self._poll({
            "status": "succeeded",
            "content": {"url": "https://cdn/out.mp4"},
            "resolution": "768P",
            "usage": {"total_seconds": 6},
        })
        self.assertEqual(state["status"], vp.COMPLETED)
        self.assertEqual(state["url"], "https://cdn/out.mp4")
        self.assertAlmostEqual(state["cost"], 0.48)

    def test_cost_is_none_when_usage_lags(self):
        # MiniMax fills usage a beat after the task flips to succeeded.
        state = self._poll({"status": "succeeded", "content": {"url": "https://cdn/o.mp4"},
                            "resolution": "768P", "usage": {}})
        self.assertIsNone(state["cost"])

    def test_poll_failed_carries_code(self):
        state = self._poll({
            "status": "failed",
            "error": {"code": "1026", "message": "video description contains sensitive content"},
        })
        self.assertEqual(state["status"], vp.FAILED)
        self.assertIn("1026", state["error"])

    def test_regenerate_payload(self):
        sent = []
        with mock.patch.object(vp.urllib.request, "urlopen",
                               capture_request(sent, fake_json_response({"task_id": "regen-1"}))):
            task_id = self.provider.regenerate("task-9")
        payload = json.loads(sent[0].data.decode())
        self.assertEqual(task_id, "regen-1")
        self.assertEqual(sent[0].full_url, f"{vp.MINIMAX_BASE}/video_regeneration")
        self.assertEqual(payload, {"model": "MiniMax-H3",
                                   "source_task_id": "task-9", "resolution": "2K"})

    def test_regenerate_failure_explains_the_7_day_window(self):
        response = fake_json_response(
            {"base_resp": {"status_code": 2013, "status_msg": "source task not found"}})
        with mock.patch.object(vp.urllib.request, "urlopen",
                               capture_request([], response)):
            with self.assertRaises(vp.VideoProviderError) as ctx:
                self.provider.regenerate("task-9")
        message = str(ctx.exception)
        self.assertIn("source task not found", message)
        self.assertIn("7 days", message)

    def test_regeneration_is_billed_at_its_own_rate(self):
        # Same 6 seconds: $0.05/s as a regeneration, not the 2K output rate of $0.13/s.
        state = self._poll({
            "status": "succeeded", "content": {"url": "https://cdn/o.mp4"},
            "resolution": "2K", "task_type": "regeneration",
            "usage": {"total_seconds": 6},
        })
        self.assertAlmostEqual(state["cost"], 0.30)

    def test_plain_generation_still_prices_by_resolution(self):
        state = self._poll({
            "status": "succeeded", "content": {"url": "https://cdn/o.mp4"},
            "resolution": "2K", "task_type": "generation",
            "usage": {"total_seconds": 6},
        })
        self.assertAlmostEqual(state["cost"], 0.78)

    def test_download_does_not_leak_the_key_to_the_cdn(self):
        sent = []
        with mock.patch.object(vp.urllib.request, "urlopen",
                               capture_request(sent, FakeResponse(b"mp4"))):
            self.provider.download("https://cdn.example/out.mp4")
        self.assertIsNone(sent[0].get_header("Authorization"))


class TestCredentials(unittest.TestCase):
    def test_missing_env_var_names_the_variable(self):
        with mock.patch.dict(vp.os.environ, {}, clear=True):
            with self.assertRaises(vp.VideoProviderError) as ctx:
                vp.get_provider("minimax")
        self.assertIn("MINIMAX_API_KEY", str(ctx.exception))

    def test_unknown_provider(self):
        with self.assertRaises(vp.VideoProviderError):
            vp.get_provider("runway")


class TestImageInputs(unittest.TestCase):
    def test_urls_pass_through(self):
        self.assertEqual(vp.to_image_url("https://x/a.png"), "https://x/a.png")

    def test_missing_local_file(self):
        with self.assertRaises(vp.VideoProviderError) as ctx:
            vp.to_image_url("/nope/missing.png")
        self.assertIn("image not found", str(ctx.exception))


# ─── STORY SPEC ──────────────────────────────────────────────

def a_spec(**overrides):
    spec = {
        "version": 1,
        "model": "hailuo-3",
        "aspect": "16:9",
        "duration": 6,
        "style": "field-note illustration",
        "shots": [
            {"id": "s1", "panel": "wide desk", "action": "she looks up",
             "camera": "slow push in"},
            {"id": "s2", "panel": "close on the screen", "action": "the alert lands",
             "camera": "locked static shot"},
        ],
    }
    spec.update(overrides)
    return spec


class TestSpecValidation(unittest.TestCase):
    def test_valid_spec(self):
        # The closing shot carries no action, so a clean spec warns about nothing.
        spec = a_spec()
        del spec["shots"][1]["action"]
        del spec["shots"][1]["camera"]
        self.assertEqual(sb.validate_spec(spec), [])

    def test_wrong_version(self):
        with self.assertRaises(sb.StorySpecError):
            sb.validate_spec(a_spec(version=2))

    def test_unknown_top_level_key(self):
        with self.assertRaises(sb.StorySpecError) as ctx:
            sb.validate_spec(a_spec(sytle="typo"))
        self.assertIn("sytle", str(ctx.exception))

    def test_unknown_shot_key(self):
        spec = a_spec()
        spec["shots"][0]["cammera"] = "push"
        with self.assertRaises(sb.StorySpecError) as ctx:
            sb.validate_spec(spec)
        self.assertIn("cammera", str(ctx.exception))

    def test_duplicate_shot_id(self):
        spec = a_spec()
        spec["shots"][1]["id"] = "s1"
        with self.assertRaises(sb.StorySpecError) as ctx:
            sb.validate_spec(spec)
        self.assertIn("duplicated", str(ctx.exception))

    def test_missing_panel(self):
        spec = a_spec()
        del spec["shots"][0]["panel"]
        with self.assertRaises(sb.StorySpecError):
            sb.validate_spec(spec)

    def test_missing_action_on_a_generating_shot(self):
        spec = a_spec()
        del spec["shots"][0]["action"]
        with self.assertRaises(sb.StorySpecError):
            sb.validate_spec(spec)

    def test_closing_shot_may_omit_action(self):
        spec = a_spec()
        del spec["shots"][1]["action"]
        self.assertEqual(sb.validate_spec(spec), [])

    def test_bridge_needs_two_shots(self):
        spec = a_spec(shots=[{"id": "s1", "panel": "only one", "action": "x"}])
        with self.assertRaises(sb.StorySpecError) as ctx:
            sb.validate_spec(spec)
        self.assertIn("at least 2 shots", str(ctx.exception))

    def test_anchor_accepts_one_shot(self):
        spec = a_spec(chain="anchor",
                      shots=[{"id": "s1", "panel": "only one", "action": "x"}])
        sb.validate_spec(spec)

    def test_bad_chain_mode(self):
        with self.assertRaises(sb.StorySpecError):
            sb.validate_spec(a_spec(chain="sliding"))

    def test_grid_too_small_is_an_error(self):
        with self.assertRaises(sb.StorySpecError) as ctx:
            sb.validate_spec(a_spec(board={"cols": 1, "rows": 1}))
        self.assertIn("holds 1 panels", str(ctx.exception))

    def test_grid_too_large_warns(self):
        warnings = sb.validate_spec(a_spec(board={"cols": 3, "rows": 3}))
        self.assertTrue(any("more cells than shots" in w for w in warnings))

    def test_unknown_camera_move_warns_but_passes(self):
        spec = a_spec()
        spec["shots"][0]["camera"] = "swooping vertigo dolly"
        warnings = sb.validate_spec(spec)
        self.assertTrue(any("outside the known vocabulary" in w for w in warnings))

    def test_closing_shot_with_action_warns(self):
        warnings = sb.validate_spec(a_spec())
        # s2 has an action and is the closing frame in bridge mode.
        self.assertTrue(any("closing frame" in w for w in warnings))

    def test_malformed_dialogue(self):
        spec = a_spec()
        spec["shots"][0]["sound"] = {"dialogue": [{"speaker": "Sara"}]}
        with self.assertRaises(sb.StorySpecError):
            sb.validate_spec(spec)


class TestGridAndPlan(unittest.TestCase):
    def test_squarest_grid_by_default(self):
        self.assertEqual(sb.grid_for(a_spec()), (2, 1))
        nine = a_spec(shots=[{"id": f"s{i}", "panel": "p", "action": "a"}
                             for i in range(9)])
        self.assertEqual(sb.grid_for(nine), (3, 3))

    def test_explicit_grid_wins(self):
        self.assertEqual(sb.grid_for(a_spec(board={"cols": 2, "rows": 2})), (2, 2))

    def test_bridge_makes_n_minus_one_clips(self):
        spec = a_spec(shots=[{"id": f"s{i}", "panel": "p", "action": "a"}
                             for i in range(4)])
        plan = sb.clip_plan(spec)
        self.assertEqual(len(plan), 3)
        self.assertEqual(plan[0]["first_panel"], 1)
        self.assertEqual(plan[0]["last_panel"], 2)
        self.assertEqual(plan[-1]["last_panel"], 4)

    def test_anchor_makes_n_clips(self):
        spec = a_spec(chain="anchor",
                      shots=[{"id": f"s{i}", "panel": "p", "action": "a"}
                             for i in range(4)])
        plan = sb.clip_plan(spec)
        self.assertEqual(len(plan), 4)
        self.assertIsNone(plan[0]["last_panel"])

    def test_clip_filename_uses_the_full_plan_index(self):
        spec = a_spec(shots=[{"id": f"s{i}", "panel": "p", "action": "a"}
                             for i in range(4)])
        plan = sb.clip_plan(spec)
        self.assertEqual([sb.clip_filename(e) for e in plan],
                         ["01-s0.mp4", "02-s1.mp4", "03-s2.mp4"])
        # A filtered re-roll must keep each clip's original number, or assemble —
        # which builds its expected names from the whole plan — looks for the wrong file.
        only_third = [e for e in plan if e["id"] == "s2"]
        self.assertEqual(sb.clip_filename(only_third[0]), "03-s2.mp4")

    def test_per_shot_duration_overrides_spec_default(self):
        spec = a_spec()
        spec["shots"][0]["duration"] = 10
        self.assertEqual(sb.clip_plan(spec)[0]["duration"], 10)


class TestPromptCompilation(unittest.TestCase):
    def test_bridge_prompt_describes_the_transition(self):
        spec = a_spec()
        prompt = sb.compile_shot_prompt(sb.clip_plan(spec)[0], spec)
        self.assertIn("Picture 1 aligns with the opening frame", prompt)
        self.assertIn("Ending state: close on the screen.", prompt)
        self.assertIn("Camera: slow push in", prompt)
        self.assertIn("one continuous camera move", prompt)
        self.assertIn(sb.NO_TEXT_GUARD, prompt)

    def test_allow_text_drops_the_guard(self):
        spec = a_spec(allow_text=True)
        prompt = sb.compile_shot_prompt(sb.clip_plan(spec)[0], spec)
        self.assertNotIn(sb.NO_TEXT_GUARD, prompt)

    def test_board_carries_the_guard_by_default(self):
        self.assertIn(sb.NO_TEXT_GUARD, sb.compile_board_prompt(a_spec()))

    def test_allow_text_drops_the_guard_from_the_board_too(self):
        """The board is the ONLY call that draws the letters, so a film about text that
        still gags its board is forbidding the one image that has to render them."""
        prompt = sb.compile_board_prompt(a_spec(allow_text=True))
        self.assertNotIn(sb.NO_TEXT_GUARD, prompt)

    def test_one_shot_allowing_text_frees_the_whole_board(self):
        """allow_text is per-shot as well as per-spec, but the board is a single image for
        every cell — it cannot gag one cell and not another."""
        spec = a_spec()
        spec["shots"][1]["allow_text"] = True
        self.assertNotIn(sb.NO_TEXT_GUARD, sb.compile_board_prompt(spec))

    def test_board_always_bans_grid_artefacts(self):
        """Cell numbers and captions are stationery, not subject matter — allow_text must
        not readmit them."""
        for spec in (a_spec(), a_spec(allow_text=True)):
            self.assertIn("No cell numbers, no captions, no speech bubbles, no borders.",
                          sb.compile_board_prompt(spec))

    def test_dialogue_uses_the_tagged_syntax(self):
        spec = a_spec()
        spec["shots"][0]["sound"] = {
            "ambience": "server-room hum",
            "dialogue": [{"speaker": "Sara", "lang": "Arabic", "line": "شوف الشاشة",
                          "delivery": "quietly"}],
        }
        prompt = sb.compile_shot_prompt(sb.clip_plan(spec)[0], spec)
        self.assertIn("<d>[Arabic] شوف الشاشة</d>", prompt)
        self.assertIn("Soundscape: server-room hum.", prompt)
        self.assertIn("Non-diegetic music: N/A.", prompt)

    def test_anchor_uses_explicit_ending(self):
        spec = a_spec(chain="anchor")
        spec["shots"][0]["ending"] = "her hand rests on the mouse"
        prompt = sb.compile_shot_prompt(sb.clip_plan(spec)[0], spec)
        self.assertIn("Ending state: her hand rests on the mouse.", prompt)
        self.assertNotIn("Picture 1 aligns", prompt)

    def test_board_prompt_numbers_every_panel(self):
        prompt = sb.compile_board_prompt(a_spec())
        self.assertIn("Cell 1: wide desk", prompt)
        self.assertIn("Cell 2: close on the screen", prompt)
        self.assertIn("2x1 grid", prompt)
        self.assertIn(sb.NO_TEXT_GUARD, prompt)

    def test_board_prompt_accounts_for_filler_cells(self):
        prompt = sb.compile_board_prompt(a_spec(board={"cols": 2, "rows": 2}))
        self.assertIn("Remaining 2 cell(s)", prompt)


# ─── SLICING + MANIFEST + ASSEMBLY ───────────────────────────

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


@unittest.skipUnless(HAVE_PIL, "Pillow not installed")
class TestSlicing(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.sheet = self.tmp / "board.png"
        Image.new("RGB", (900, 600), "white").save(self.sheet)

    def test_slices_every_cell(self):
        panels = sb.slice_contact_sheet(self.sheet, 3, 2, self.tmp / "panels",
                                        inset=0, autotrim=False)
        self.assertEqual(len(panels), 6)
        self.assertEqual([p.name for p in panels][:2], ["panel-01.png", "panel-02.png"])
        self.assertEqual(Image.open(panels[0]).size, (300, 300))

    def test_count_limits_output(self):
        panels = sb.slice_contact_sheet(self.sheet, 3, 2, self.tmp / "p2", count=4)
        self.assertEqual(len(panels), 4)

    def test_inset_trims_the_gutter(self):
        panels = sb.slice_contact_sheet(self.sheet, 3, 2, self.tmp / "p3",
                                        inset=0.1, autotrim=False)
        self.assertEqual(Image.open(panels[0]).size, (240, 240))

    def test_bad_inset(self):
        with self.assertRaises(sb.StorySpecError):
            sb.slice_contact_sheet(self.sheet, 3, 2, self.tmp / "p4", inset=0.9)


@unittest.skipUnless(HAVE_PIL, "Pillow not installed")
class TestAutotrim(unittest.TestCase):
    def _bordered(self, border_px, colour="black"):
        """Noisy 'artwork' inside a flat border — what a drawn panel frame looks like."""
        import random
        image = Image.new("RGB", (400, 400), colour)
        rng = random.Random(0)
        for x in range(border_px, 400 - border_px):
            for y in range(border_px, 400 - border_px):
                grey = rng.randrange(60, 200)
                image.putpixel((x, y), (grey, grey, grey))
        return image

    def test_removes_a_drawn_frame(self):
        trimmed = sb.autotrim_borders(self._bordered(20))
        self.assertEqual(trimmed.size, (360, 360))

    def test_removes_a_pale_margin_too(self):
        trimmed = sb.autotrim_borders(self._bordered(15, colour="white"))
        self.assertEqual(trimmed.size, (370, 370))

    def test_stops_at_the_cap_on_a_flat_image(self):
        # A genuinely flat panel must not be eaten away — the cap holds it at 12%/edge.
        flat = Image.new("RGB", (400, 400), "white")
        self.assertEqual(sb.autotrim_borders(flat).size, (304, 304))

    def test_untouched_when_there_is_no_border(self):
        self.assertEqual(sb.autotrim_borders(self._bordered(0)).size, (400, 400))


class TestManifest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def test_merge_preserves_sibling_entries(self):
        sb.merge_manifest(self.tmp, {"clips": {"s1": {"file": "a.mp4"}}})
        sb.merge_manifest(self.tmp, {"clips": {"s2": {"file": "b.mp4"}}})
        manifest = json.loads((self.tmp / "manifest.json").read_text())
        self.assertEqual(sorted(manifest["clips"]), ["s1", "s2"])

    def test_merge_survives_a_corrupt_manifest(self):
        (self.tmp / "manifest.json").write_text("{not json")
        manifest = sb.merge_manifest(self.tmp, {"clips": {"s1": {}}})
        self.assertIn("s1", manifest["clips"])


class TestAssemble(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def test_no_clips(self):
        with self.assertRaises(sb.StorySpecError) as ctx:
            sb.assemble([], self.tmp / "out.mp4")
        self.assertIn("no clips to assemble", str(ctx.exception))

    def test_missing_clip_named_in_the_error(self):
        with self.assertRaises(sb.StorySpecError) as ctx:
            sb.assemble([self.tmp / "01-s1.mp4"], self.tmp / "out.mp4")
        self.assertIn("01-s1.mp4", str(ctx.exception))

    def _fake_clips(self, n=2):
        out = []
        for i in range(n):
            c = self.tmp / f"0{i+1}-s{i}.mp4"
            c.write_bytes(b"fake")
            out.append(c)
        return out

    def _run_assemble(self, sizes):
        """Assemble with probe_size stubbed; returns the ffmpeg argv actually used."""
        clips = self._fake_clips(len(sizes))
        captured = {}
        def fake_run(argv, **kw):
            captured["argv"] = argv
            class R: returncode = 0; stderr = ""
            return R()
        with mock.patch.object(sb, "probe_size", side_effect=lambda c: sizes[clips.index(c)]), \
             mock.patch.object(sb.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             mock.patch.object(sb.subprocess, "run", fake_run):
            sb.assemble(clips, self.tmp / "out.mp4")
        return captured["argv"]

    def test_uniform_sizes_stream_copy(self):
        argv = self._run_assemble([(2592, 1440), (2592, 1440)])
        self.assertIn("copy", argv)
        self.assertNotIn("libx264", argv)

    def test_mismatched_sizes_force_a_reencode_to_the_largest(self):
        # H3 really does this: regenerating a batch returned 2592x1440 and 2560x1440.
        # -c copy would write one size into the header and let a segment disagree.
        argv = self._run_assemble([(2592, 1440), (2560, 1440)])
        self.assertIn("libx264", argv)
        self.assertNotIn("copy", argv)
        self.assertTrue(any("scale=2592:1440" in a for a in argv if isinstance(a, str)))

    def test_unprobeable_clips_fall_back_to_stream_copy(self):
        argv = self._run_assemble([None, None])
        self.assertIn("copy", argv)

    def test_missing_ffmpeg_is_actionable(self):
        clip = self.tmp / "01-s1.mp4"
        clip.write_bytes(b"fake")
        with mock.patch.object(sb.shutil, "which", return_value=None):
            with self.assertRaises(sb.StorySpecError) as ctx:
                sb.assemble([clip], self.tmp / "out.mp4")
        self.assertIn("brew install ffmpeg", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
