"""Async video-generation backends for generate_media.py — OpenRouter and MiniMax direct.

Stdlib-only (no google-genai / PIL / etc.) so this module — and its behavior — can be
imported and tested directly under plain `python3`, without the uv-managed deps that
generate_media.py needs. generate_media.py imports this lazily inside its command
functions; keep it that way.

Both HTTP backends have the same three-step shape: submit -> poll -> download. The
Gemini/Veo path is deliberately NOT here — it stays in generate_media.py because it needs
the google-genai SDK. What lives here is the registry that routes a `--model` to a
backend, the capability validation, and the two HTTP transports.

Capability data in MODEL_REGISTRY is a client-side mirror of what the providers publish.
It exists so an out-of-set value fails locally (free) instead of as a 400 (also free, but
slower and less legible). `refresh_openrouter_caps()` re-reads the live endpoint when you
want to check the mirror hasn't drifted.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
MINIMAX_BASE = "https://api.minimax.io/v2"

# Normalized job states. Provider-specific vocabularies are mapped onto these.
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"


class VideoProviderError(Exception):
    """Raised when a backend is missing credentials, rejects a request, or fails a job."""


# ─── CAPABILITIES ────────────────────────────────────────────

@dataclass(frozen=True)
class Caps:
    """What one provider will accept for one model. Values come from the provider's own
    capability endpoint / docs; see the module docstring on why they're mirrored here."""
    durations: tuple
    aspect_ratios: tuple
    frame_types: tuple = ()
    resolutions: tuple = ()
    default_resolution: str | None = None
    references: bool = False
    audio: bool = False
    seed: bool = False
    # resolution -> USD per second of output. Empty when the caller prices it (Gemini).
    price_per_second: dict = field(default_factory=dict)
    reference_image_price: float = 0.0
    free_reference_images: int = 0


@dataclass(frozen=True)
class ModelSpec:
    """A `--model` value: which backends can serve it, and what each calls it."""
    providers: tuple          # first entry is the default for --provider auto
    backend_ids: dict         # provider -> the model id that provider expects
    caps: dict                # provider -> Caps
    note: str = ""


_H3_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
_H3_PRICE = {"2K": 0.13, "768P": 0.08}

# 768P -> 2K re-render, MiniMax direct only. Cheaper than generating at 2K because it
# reuses the original result rather than starting over.
MINIMAX_REGEN_PRICE = 0.05
REGEN_WINDOW_DAYS = 7

MODEL_REGISTRY: dict[str, ModelSpec] = {
    # ── Gemini/Veo direct — the original path, unchanged behavior ──
    "fast": ModelSpec(
        providers=("gemini",),
        backend_ids={"gemini": "veo-3.1-lite-generate-preview"},
        caps={"gemini": Caps(
            durations=(4, 6, 8),
            aspect_ratios=("16:9", "9:16"),
            frame_types=("first_frame",),
            audio=True,
        )},
        note="Veo 3.1 Lite via the Gemini SDK",
    ),
    "standard": ModelSpec(
        providers=("gemini",),
        backend_ids={"gemini": "veo-3.1-generate-preview"},
        caps={"gemini": Caps(
            durations=(4, 6, 8),
            aspect_ratios=("16:9", "9:16"),
            frame_types=("first_frame",),
            audio=True,
        )},
        note="Veo 3.1 via the Gemini SDK",
    ),

    # ── MiniMax H3, reachable two ways ──
    "hailuo-3": ModelSpec(
        providers=("openrouter", "minimax"),
        backend_ids={"openrouter": "minimax/hailuo-3", "minimax": "MiniMax-H3"},
        caps={
            "openrouter": Caps(
                durations=tuple(range(5, 16)),
                aspect_ratios=_H3_RATIOS,
                frame_types=("first_frame", "last_frame"),
                resolutions=("2K",),
                default_resolution="2K",
                references=True,
                audio=True,
                price_per_second={"2K": 0.13},
                reference_image_price=0.04,
                free_reference_images=5,
            ),
            # Direct adds the 768P tier and a 4-second floor.
            "minimax": Caps(
                durations=tuple(range(4, 16)),
                aspect_ratios=_H3_RATIOS,
                frame_types=("first_frame", "last_frame"),
                resolutions=("768P", "2K"),
                default_resolution="768P",
                references=True,
                audio=True,
                price_per_second=_H3_PRICE,
                reference_image_price=0.04,
                free_reference_images=5,
            ),
        },
        note="2K + native audio; #1 on video editing. 768P draft tier is direct-only.",
    ),
    "hailuo-2.3": ModelSpec(
        providers=("openrouter",),
        backend_ids={"openrouter": "minimax/hailuo-2.3"},
        caps={"openrouter": Caps(
            durations=(6, 10),
            aspect_ratios=("16:9",),
            frame_types=("first_frame",),   # no last_frame — cannot run a frame chain
            resolutions=("1080p",),
            default_resolution="1080p",
            audio=False,
            price_per_second={"1080p": 0.0817},
        )},
        note="Cheaper, but first_frame only and no audio — the story chain needs last_frame.",
    ),
}


def resolve(model: str, provider: str = "auto") -> tuple[ModelSpec, str]:
    """Map (--model, --provider) to a spec + concrete backend name. Raises on unknown or
    on a model/provider pair that doesn't exist."""
    spec = MODEL_REGISTRY.get(model)
    if spec is None:
        known = ", ".join(sorted(MODEL_REGISTRY))
        raise VideoProviderError(f"unknown model {model!r} — known models: {known}")

    if provider in (None, "", "auto"):
        return spec, spec.providers[0]

    if provider not in spec.providers:
        served = ", ".join(spec.providers)
        raise VideoProviderError(
            f"model {model!r} is not served by provider {provider!r} — available: {served}"
        )
    return spec, provider


# ─── REQUEST + VALIDATION ────────────────────────────────────

@dataclass
class VideoRequest:
    model: str
    provider: str
    prompt: str
    duration: int
    aspect_ratio: str | None = None
    resolution: str | None = None
    first_frame: str | None = None
    last_frame: str | None = None
    references: list = field(default_factory=list)
    generate_audio: bool = True
    seed: int | None = None

    @property
    def frames(self) -> list[tuple[str, str]]:
        out = []
        if self.first_frame:
            out.append(("first_frame", self.first_frame))
        if self.last_frame:
            out.append(("last_frame", self.last_frame))
        return out


def validate(req: VideoRequest, caps: Caps) -> None:
    """Fail locally on anything the backend would 400 on, plus the one rule that isn't a
    400 but a silent behavior change (frames vs references). Raises VideoProviderError."""

    # Frame roles and reference roles are different generation MODES, not additive inputs.
    # MiniMax enforces this at the model; OpenRouter silently drops input_references and
    # treats the call as image-to-video. Either way the user doesn't get what they asked
    # for, so refuse instead of guessing which one they meant.
    if req.frames and req.references:
        raise VideoProviderError(
            "--first-frame/--last-frame cannot be combined with --reference: they select "
            "different generation modes and the model accepts only one per request.\n"
            "  Bake identity into the frame images instead (generate them with `image "
            "--reference <cast refs>`), then pass those frames here."
        )

    if req.duration not in caps.durations:
        allowed = ", ".join(str(d) for d in caps.durations)
        raise VideoProviderError(
            f"--duration {req.duration} not supported by {req.model} on {req.provider} "
            f"(allowed: {allowed})"
        )

    if req.aspect_ratio and req.aspect_ratio not in caps.aspect_ratios:
        allowed = ", ".join(caps.aspect_ratios)
        raise VideoProviderError(
            f"--aspect {req.aspect_ratio} not supported by {req.model} on {req.provider} "
            f"(allowed: {allowed})"
        )

    if req.resolution and caps.resolutions and req.resolution not in caps.resolutions:
        allowed = ", ".join(caps.resolutions)
        raise VideoProviderError(
            f"--resolution {req.resolution} not supported by {req.model} on "
            f"{req.provider} (allowed: {allowed})"
        )

    for frame_type, _ in req.frames:
        if frame_type not in caps.frame_types:
            supported = ", ".join(caps.frame_types) or "none"
            raise VideoProviderError(
                f"{req.model} on {req.provider} does not accept {frame_type} "
                f"(supported: {supported})"
            )

    if req.references and not caps.references:
        raise VideoProviderError(
            f"{req.model} on {req.provider} does not accept reference images"
        )

    if req.generate_audio and not caps.audio:
        raise VideoProviderError(
            f"{req.model} on {req.provider} does not generate audio — pass --no-audio"
        )

    if req.seed is not None and not caps.seed:
        raise VideoProviderError(
            f"{req.model} on {req.provider} ignores --seed (no seed support); omit it "
            "rather than expecting reproducible output"
        )


def estimate_cost(req: VideoRequest, caps: Caps) -> float:
    """USD estimate for one submit. Returns 0.0 when the caller prices the model itself."""
    if not caps.price_per_second:
        return 0.0
    resolution = req.resolution or caps.default_resolution
    per_second = caps.price_per_second.get(resolution)
    if per_second is None:
        per_second = max(caps.price_per_second.values())
    cost = per_second * req.duration
    billable_refs = max(0, len(req.references) - caps.free_reference_images)
    cost += billable_refs * caps.reference_image_price
    return cost


# ─── IMAGE INPUTS ────────────────────────────────────────────

def to_image_url(source: str) -> str:
    """Pass an http(s) URL through; base64 a local file into a data: URL.

    Both APIs accept data URLs for images. MiniMax caps the whole request body at 64 MB
    and recommends hosted URLs for large assets — storyboard panels are far under that,
    but a long reference list of full-res stills can add up.
    """
    if source.startswith(("http://", "https://", "data:")):
        return source

    path = Path(source).expanduser()
    if not path.is_file():
        raise VideoProviderError(f"image not found: {source}")

    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{encoded}"


# ─── HTTP ────────────────────────────────────────────────────

def _http_json(method: str, url: str, token: str | None, payload=None, timeout: int = 120) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace").strip()[:600]
        raise VideoProviderError(f"{method} {url} -> HTTP {e.code}: {detail or e.reason}")
    except urllib.error.URLError as e:
        raise VideoProviderError(f"{method} {url} failed: {e.reason}")
    except json.JSONDecodeError as e:
        raise VideoProviderError(f"{method} {url} returned non-JSON: {e}")


def _http_bytes(url: str, token: str | None, timeout: int = 300) -> bytes:
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace").strip()[:300]
        raise VideoProviderError(f"download failed: HTTP {e.code}: {detail or e.reason}")
    except urllib.error.URLError as e:
        raise VideoProviderError(f"download failed: {e.reason}")


# ─── PROVIDERS ───────────────────────────────────────────────

class Provider:
    """submit() -> job id; poll() -> normalized state; download() -> mp4 bytes."""

    name = ""
    env_var = ""
    signup = ""

    def __init__(self, token: str):
        self.token = token

    @classmethod
    def from_env(cls) -> "Provider":
        token = os.getenv(cls.env_var)
        if not token:
            raise VideoProviderError(
                f"{cls.env_var} is not set — needed for --provider {cls.name}. "
                f"Get a key at {cls.signup}"
            )
        return cls(token)

    def submit(self, req: VideoRequest, backend_id: str, caps: Caps) -> str:
        raise NotImplementedError

    def poll(self, job_id: str) -> dict:
        """-> {status, url, error, cost} with status in {RUNNING, COMPLETED, FAILED}."""
        raise NotImplementedError

    def download(self, url: str) -> bytes:
        raise NotImplementedError


class OpenRouterProvider(Provider):
    name = "openrouter"
    env_var = "OPENROUTER_API_KEY"
    signup = "https://openrouter.ai/keys"

    # OpenRouter's terminal failure vocabulary is wider than "failed".
    _TERMINAL_FAILURES = {"failed", "cancelled", "expired"}

    def submit(self, req: VideoRequest, backend_id: str, caps: Caps) -> str:
        payload = {"model": backend_id, "prompt": req.prompt, "duration": req.duration}

        if req.aspect_ratio:
            payload["aspect_ratio"] = req.aspect_ratio
        resolution = req.resolution or caps.default_resolution
        if resolution:
            payload["resolution"] = resolution
        if caps.audio:
            payload["generate_audio"] = req.generate_audio
        if req.seed is not None:
            payload["seed"] = req.seed

        if req.frames:
            payload["frame_images"] = [
                {
                    "type": "image_url",
                    "image_url": {"url": to_image_url(source)},
                    "frame_type": frame_type,
                }
                for frame_type, source in req.frames
            ]
        elif req.references:
            payload["input_references"] = [
                {"type": "image_url", "image_url": {"url": to_image_url(source)}}
                for source in req.references
            ]

        response = _http_json("POST", f"{OPENROUTER_BASE}/videos", self.token, payload)
        job_id = response.get("id")
        if not job_id:
            raise VideoProviderError(f"OpenRouter did not return a job id: {response}")
        return job_id

    def poll(self, job_id: str) -> dict:
        data = _http_json("GET", f"{OPENROUTER_BASE}/videos/{job_id}", self.token)
        raw_status = (data.get("status") or "").lower()

        if raw_status == "completed":
            urls = data.get("unsigned_urls") or []
            if not urls:
                raise VideoProviderError(
                    f"job {job_id} completed but returned no video URL: {data}"
                )
            return {
                "status": COMPLETED,
                "url": urls[0],
                "error": None,
                "cost": (data.get("usage") or {}).get("cost"),
            }

        if raw_status in self._TERMINAL_FAILURES:
            return {
                "status": FAILED,
                "url": None,
                "error": data.get("error") or raw_status,
                "cost": (data.get("usage") or {}).get("cost"),
            }

        return {"status": RUNNING, "url": None, "error": None, "cost": None}

    def download(self, url: str) -> bytes:
        # OpenRouter's content endpoint requires the auth header.
        return _http_bytes(url, self.token)


class MiniMaxProvider(Provider):
    name = "minimax"
    env_var = "MINIMAX_API_KEY"
    signup = "https://platform.minimax.io (API Keys)"

    _TERMINAL_FAILURES = {"failed", "cancelled"}

    def submit(self, req: VideoRequest, backend_id: str, caps: Caps) -> str:
        # MiniMax takes one multimodal content[] array; the item's `role` is what selects
        # image-to-video / first-last-frame / reference mode.
        content = [{"type": "text", "text": req.prompt}]

        for frame_type, source in req.frames:
            content.append({
                "type": "image_url",
                "image_url": {"url": to_image_url(source)},
                "role": frame_type,
            })
        if not req.frames:
            for source in req.references:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": to_image_url(source)},
                    "role": "reference_image",
                })

        payload = {
            "model": backend_id,
            "content": content,
            "duration": req.duration,
            "resolution": req.resolution or caps.default_resolution,
        }
        # Text-to-video requires an explicit ratio; the image modes derive it from the
        # input image and reject one, so only send it when there are no image inputs.
        if req.aspect_ratio and not req.frames and not req.references:
            payload["ratio"] = req.aspect_ratio

        response = _http_json("POST", f"{MINIMAX_BASE}/video_generation", self.token, payload)
        task_id = response.get("task_id")
        if not task_id:
            base = response.get("base_resp") or {}
            detail = base.get("status_msg") or response
            raise VideoProviderError(f"MiniMax did not return a task_id: {detail}")
        return task_id

    def poll(self, job_id: str) -> dict:
        data = _http_json(
            "GET", f"{MINIMAX_BASE}/query/video_generation/{job_id}", self.token
        )
        task = data.get("task") or data
        raw_status = (task.get("status") or "").lower()

        if raw_status == "succeeded":
            url = (task.get("content") or {}).get("url")
            if not url:
                raise VideoProviderError(
                    f"task {job_id} succeeded but returned no video URL: {task}"
                )
            return {"status": COMPLETED, "url": url, "error": None,
                    "cost": _minimax_cost(task)}

        if raw_status in self._TERMINAL_FAILURES:
            error = task.get("error") or {}
            message = error.get("message") or raw_status
            code = error.get("code")
            return {
                "status": FAILED,
                "url": None,
                "error": f"[{code}] {message}" if code else message,
                "cost": None,
            }

        return {"status": RUNNING, "url": None, "error": None, "cost": None}

    def regenerate(self, source_task_id: str, resolution: str = "2K") -> str:
        """Re-render an existing 768P task at 2K, direct-only.

        This is NOT the same as re-submitting the prompt at a higher resolution. H3 has no
        seed, so a fresh submit returns a different take — different blocking, different
        performance. Regeneration feeds the original result plus its context back through
        the model, so what you approved at 768P is what you get at 2K. It is the only
        reason the cheap draft tier is useful rather than merely cheap.
        """
        payload = {
            "model": "MiniMax-H3",
            "source_task_id": source_task_id,
            "resolution": resolution,
        }
        response = _http_json(
            "POST", f"{MINIMAX_BASE}/video_regeneration", self.token, payload
        )
        task_id = response.get("task_id")
        if not task_id:
            base = response.get("base_resp") or {}
            detail = base.get("status_msg") or response
            raise VideoProviderError(
                f"MiniMax regeneration did not return a task_id: {detail}\n"
                "  The source task must be your own, 768P, in 'succeeded' state, and "
                "created within the last 7 days — regeneration cannot revive an old draft."
            )
        return task_id

    def download(self, url: str) -> bytes:
        # content.url is a plain CDN link — sending our API key to it would leak the
        # credential to a third-party host for no benefit.
        return _http_bytes(url, token=None)


def _minimax_cost(task: dict) -> float | None:
    """MiniMax reports billed seconds, not dollars, and fills `usage` in a beat AFTER the
    task flips to succeeded — so this is frequently None on the first successful poll.
    Callers should fall back to the local estimate rather than reporting $0.00."""
    usage = task.get("usage") or {}
    seconds = usage.get("total_seconds")
    if seconds is None:
        return None
    # Regeneration is billed at its own rate, not the output resolution's rate.
    if task.get("task_type") == "regeneration":
        return round(MINIMAX_REGEN_PRICE * seconds, 4)
    per_second = _H3_PRICE.get(task.get("resolution"))
    if per_second is None:
        return None
    return round(per_second * seconds, 4)


PROVIDERS = {p.name: p for p in (OpenRouterProvider, MiniMaxProvider)}


def get_provider(name: str) -> Provider:
    provider_cls = PROVIDERS.get(name)
    if provider_cls is None:
        known = ", ".join(sorted(PROVIDERS))
        raise VideoProviderError(f"no HTTP transport for provider {name!r} (have: {known})")
    return provider_cls.from_env()


# ─── POLLING LOOP ────────────────────────────────────────────

def wait_for(provider: Provider, job_id: str, timeout: int = 900, interval: int = 10,
             quiet: bool = False) -> dict:
    """Block until the job reaches a terminal state. Raises on failure or timeout."""
    started = time.time()
    while True:
        state = provider.poll(job_id)

        if state["status"] == COMPLETED:
            return state
        if state["status"] == FAILED:
            raise VideoProviderError(f"generation failed: {state['error']}")

        elapsed = int(time.time() - started)
        if elapsed > timeout:
            raise VideoProviderError(
                f"timed out after {timeout}s waiting for job {job_id} — the job may still "
                f"complete; check it with the provider before resubmitting and paying twice"
            )
        if not quiet:
            print(f"  Waiting... ({elapsed}s elapsed)", file=sys.stderr)
        time.sleep(interval)


# ─── LIVE CAPABILITY CHECK ───────────────────────────────────

def refresh_openrouter_caps(model_id: str) -> dict:
    """Read the live capability record for one OpenRouter model. Used by
    `video --check-caps` to verify MODEL_REGISTRY hasn't drifted. No auth required."""
    data = _http_json("GET", f"{OPENROUTER_BASE}/videos/models", token=None)
    for entry in data.get("data", data if isinstance(data, list) else []):
        if entry.get("id") == model_id:
            return entry
    raise VideoProviderError(f"{model_id} is not listed on {OPENROUTER_BASE}/videos/models")
