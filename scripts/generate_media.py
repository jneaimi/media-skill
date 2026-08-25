# /// script
# requires-python = ">=3.11"
# dependencies = ["google-genai", "elevenlabs", "python-dotenv", "pillow", "arabic-reshaper", "python-bidi"]
# ///
"""Unified media generation: images (Gemini), video clips (Veo), voiceovers (ElevenLabs), text overlays."""

import argparse
import base64
import io
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Load .env from home dir as fallback (env vars from ~/.zshrc take precedence)
_home_env = os.path.expanduser("~/.env")
if os.path.exists(_home_env):
    load_dotenv(_home_env, override=False)

# Default output — can be overridden with --output
OUTPUT_DIR = Path.home() / "generated_media"

# Cost tracking
_api_calls = 0
_estimated_cost = 0.0

_COST_MAP = {
    "image_flash": 0.04,
    "image_pro": 0.12,
    "video_standard": 0.40,  # per second
    "video_fast": 0.15,      # per second
    "voice_v3": 0.00030,     # per character
    "voice_flash": 0.00015,  # per character
}

# Model mappings
IMAGE_MODELS = {
    "flash": "gemini-3.1-flash-image",  # Nano Banana 2 (current stable; was gemini-2.5-flash-image)
    "pro": "gemini-3-pro-image",        # Nano Banana Pro (stable; the -preview id is retiring)
}

VIDEO_MODELS = {
    "standard": "veo-3.1-generate-preview",
    "fast": "veo-3.1-lite-generate-preview",  # "fast" variant was retired; "lite" is the current low-cost tier
}

# Models served over HTTP by video_providers.py rather than the Gemini SDK. The registry
# there is the source of truth for routing and capabilities; this list only exists so
# argparse can offer the choices without importing a module that may not be needed.
HTTP_VIDEO_MODELS = ["hailuo-3", "hailuo-2.3"]
ALL_VIDEO_MODELS = list(VIDEO_MODELS) + HTTP_VIDEO_MODELS
VIDEO_PROVIDERS = ["auto", "gemini", "openrouter", "minimax"]

VOICE_MODELS = {
    "v3": "eleven_v3",
    "flash": "eleven_flash_v2_5",
}

VALID_ASPECT_RATIOS = ["1:1", "2:3", "3:2", "16:9", "9:16", "21:9"]
VALID_IMAGE_SIZES = ["1K", "2K", "4K"]
VALID_VIDEO_DURATIONS = [4, 6, 8]

# Voice presets — stock ElevenLabs voices by default. Point any of these at your own
# cloned voice by exporting the matching env var; no voice ID is hardcoded to a person.
VOICE_PRESETS = {
    "my-voice": os.getenv("ELEVENLABS_VOICE_MY_VOICE", "JBFqnCBsd6RMkjVDRZzb"),             # George — override with your own clone
    "arabic-male": os.getenv("ELEVENLABS_VOICE_ARABIC_MALE", "nPczCjzI2devNBz1zQrb"),       # Brian
    "arabic-female": os.getenv("ELEVENLABS_VOICE_ARABIC_FEMALE", "21m00Tcm4TlvDq8ikWAM"),   # Rachel
    "english-male": os.getenv("ELEVENLABS_VOICE_ENGLISH_MALE", "nPczCjzI2devNBz1zQrb"),     # Brian
    "english-female": os.getenv("ELEVENLABS_VOICE_ENGLISH_FEMALE", "21m00Tcm4TlvDq8ikWAM"), # Rachel
}


def _track_cost(cost_key: str, multiplier: float = 1.0):
    global _api_calls, _estimated_cost
    _api_calls += 1
    _estimated_cost += _COST_MAP.get(cost_key, 0) * multiplier


def _track_cost_usd(amount: float):
    """Book a call whose price came from the provider rather than _COST_MAP."""
    global _api_calls, _estimated_cost
    _api_calls += 1
    _estimated_cost += amount


def _confirm_spend(amount: float, what: str, assume_yes: bool) -> None:
    """Gate paid generation behind an explicit yes. Video is 10-50x the cost of an image,
    so a mistyped duration or a fat shot list should not silently drain a balance."""
    if assume_yes:
        return
    print(f"\nAbout to spend ~${amount:.2f} on {what}.", file=sys.stderr)
    if not sys.stdin.isatty():
        print(
            "Error: refusing to spend without confirmation in a non-interactive session — "
            "pass --yes if this is what you want.",
            file=sys.stderr,
        )
        sys.exit(2)
    answer = input("Proceed? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Aborted — nothing was submitted.", file=sys.stderr)
        sys.exit(1)


def _print_cost():
    print(f"[Media: {_api_calls} calls, ~${_estimated_cost:.2f} estimated]", file=sys.stderr)


def _save_metadata(filepath: Path, metadata: dict):
    meta_path = filepath.with_suffix(".meta.json")
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)
    print(f"  Metadata: {meta_path}", file=sys.stderr)


def _save_file(data: bytes, output_dir: Path, prefix: str, ext: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{prefix}_{timestamp}{ext}"
    filepath = output_dir / filename

    counter = 1
    while filepath.exists():
        filename = f"{prefix}_{timestamp}_{counter}{ext}"
        filepath = output_dir / filename
        counter += 1

    with open(filepath, "wb") as f:
        f.write(data)
    return filepath


def _get_google_client():
    from google import genai
    # Prefer GEMINI_API_KEY — many users keep GOOGLE_API_KEY scoped to non-Gemini
    # services (Maps, Geocoding, Gmail, etc.) where the key has Gemini blocked.
    # Falling back to GOOGLE_API_KEY only when GEMINI_API_KEY is unset matches
    # the way most dev setups split the two.
    gemini_key = os.getenv("GEMINI_API_KEY")
    api_key = gemini_key or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY or GOOGLE_API_KEY not set. Add to ~/.zshrc or ~/.env", file=sys.stderr)
        sys.exit(1)
    # The google-genai SDK auto-detects GOOGLE_API_KEY from the environment and
    # uses it even when api_key= is passed explicitly. When GOOGLE_API_KEY is
    # scoped to non-Gemini GCP services this silently breaks image/video
    # generation (empty response → `resp.parts is None`). When we've chosen the
    # Gemini key, hide GOOGLE_API_KEY from the SDK so our explicit choice wins.
    # Equivalent to running with `env -u GOOGLE_API_KEY`; process-local only.
    if gemini_key and os.environ.get("GOOGLE_API_KEY"):
        os.environ.pop("GOOGLE_API_KEY", None)
    return genai.Client(api_key=api_key)


def _extract_image_bytes(part) -> bytes | None:
    """Extract image bytes from a Gemini response part."""
    # Try raw data first
    try:
        if hasattr(part.inline_data, "data") and part.inline_data.data:
            raw = part.inline_data.data
            if isinstance(raw, bytes):
                return raw
            if isinstance(raw, str):
                return base64.b64decode(raw)
    except Exception:
        pass

    # Try as_image()
    try:
        from PIL import Image
        pil_img = part.as_image()
        buf = io.BytesIO()
        pil_img.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        pass

    return None


def extract_alpha_two_pass(img_on_white: bytes, img_on_black: bytes) -> bytes:
    """Extract alpha channel using difference matting technique."""
    from PIL import Image

    white_img = Image.open(io.BytesIO(img_on_white)).convert("RGBA")
    black_img = Image.open(io.BytesIO(img_on_black)).convert("RGBA")

    if white_img.size != black_img.size:
        raise ValueError("Dimension mismatch between white and black background images")

    width, height = white_img.size
    white_px = white_img.load()
    black_px = black_img.load()
    output_img = Image.new("RGBA", (width, height))
    out_px = output_img.load()
    bg_dist = math.sqrt(3 * 255 * 255)

    for y in range(height):
        for x in range(width):
            rW, gW, bW, _ = white_px[x, y]
            rB, gB, bB, _ = black_px[x, y]
            pixel_dist = math.sqrt((rW - rB) ** 2 + (gW - gB) ** 2 + (bW - bB) ** 2)
            alpha = max(0, min(1, 1 - pixel_dist / bg_dist))

            if alpha > 0.01:
                rOut = min(255, int(rB / alpha))
                gOut = min(255, int(gB / alpha))
                bOut = min(255, int(bB / alpha))
            else:
                rOut, gOut, bOut = 0, 0, 0
            out_px[x, y] = (rOut, gOut, bOut, int(alpha * 255))

    buf = io.BytesIO()
    output_img.save(buf, format="PNG")
    return buf.getvalue()


# ─── IMAGE SUBCOMMAND ───────────────────────────────────────

def cmd_image(args):
    from google.genai import types

    if args.vectorize:
        sys.path.insert(0, str(Path(__file__).parent))
        from vectorize import _vectorize_file

    client = _get_google_client()
    model_name = IMAGE_MODELS[args.model]
    cost_key = f"image_{args.model}"

    if args.size == "4K" and args.model != "pro":
        print("Warning: 4K requires pro model. Switching to pro...", file=sys.stderr)
        args.model = "pro"
        model_name = IMAGE_MODELS["pro"]
        cost_key = "image_pro"

    output_dir = Path(args.output) if args.output else OUTPUT_DIR

    # image_size is only honoured by the pro image model; flash always renders 1K.
    def _image_config():
        kwargs = {"aspect_ratio": args.aspect}
        if args.model == "pro":
            kwargs["image_size"] = args.size
        return types.ImageConfig(**kwargs)

    results = []

    for i in range(args.count):
        if args.count > 1:
            print(f"Generating variation {i + 1}/{args.count}...", file=sys.stderr)

        # Build content
        if args.reference:
            from PIL import Image
            content_parts = []
            for img_path in args.reference[:14]:
                try:
                    img = Image.open(img_path)
                    content_parts.append(img)
                    print(f"  Loaded reference: {img_path}", file=sys.stderr)
                except Exception as e:
                    print(f"  Failed to load {img_path}: {e}", file=sys.stderr)
            content_parts.append(args.prompt)
            contents = content_parts
        else:
            contents = args.prompt

        # Handle transparent generation
        if args.transparent:
            # Pass 1: white background
            print("Step 1/3: Generating on white background...", file=sys.stderr)
            white_prompt = f"{args.prompt}, on a pure solid white #FFFFFF background"
            config = types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=_image_config(),
            )
            resp = client.models.generate_content(model=model_name, contents=white_prompt, config=config)
            _track_cost(cost_key)

            white_bytes = None
            for part in resp.parts:
                if part.inline_data is not None:
                    white_bytes = _extract_image_bytes(part)
                    if white_bytes:
                        break
            if not white_bytes:
                print("Error: Failed to generate white background image", file=sys.stderr)
                sys.exit(1)

            # Pass 2: edit to black background
            print("Step 2/3: Editing to black background...", file=sys.stderr)
            from PIL import Image
            pil_white = Image.open(io.BytesIO(white_bytes))
            edit_config = types.GenerateContentConfig(response_modalities=["IMAGE"])
            edit_resp = client.models.generate_content(
                model=model_name,
                contents=[pil_white, "Change the white background to a solid pure black #000000. Keep everything else exactly unchanged."],
                config=edit_config,
            )
            _track_cost(cost_key)

            black_bytes = None
            for part in edit_resp.parts:
                if part.inline_data is not None:
                    black_bytes = _extract_image_bytes(part)
                    if black_bytes:
                        break
            if not black_bytes:
                print("Error: Failed to generate black background image", file=sys.stderr)
                sys.exit(1)

            # Pass 3: extract alpha
            print("Step 3/3: Extracting alpha channel...", file=sys.stderr)
            image_data = extract_alpha_two_pass(white_bytes, black_bytes)

        else:
            # Standard generation
            response_modalities = ["IMAGE"]
            config = types.GenerateContentConfig(
                response_modalities=response_modalities,
                image_config=_image_config(),
            )

            resp = client.models.generate_content(model=model_name, contents=contents, config=config)
            _track_cost(cost_key)

            image_data = None
            for part in resp.parts:
                if part.inline_data is not None:
                    image_data = _extract_image_bytes(part)
                    if image_data:
                        break

            if not image_data:
                print("Error: No image generated", file=sys.stderr)
                continue

        # Save
        prefix = args.prefix
        if args.transparent:
            prefix = f"{prefix}_transparent"
        if args.count > 1:
            prefix = f"{prefix}_v{i + 1}"

        filepath = _save_file(image_data, output_dir, prefix, ".png")
        print(f"  Saved: {filepath}", file=sys.stderr)

        metadata = {
            "type": "image",
            "prompt": args.prompt,
            "model": model_name,
            "aspect_ratio": args.aspect,
            "size": args.size,
            "transparent": args.transparent,
            "reference_images": args.reference or [],
            "timestamp": datetime.now().isoformat(),
        }
        _save_metadata(filepath, metadata)
        results.append({"file": str(filepath), "metadata": metadata})

    if args.vectorize:
        for r in results:
            svg_path = _vectorize_file(Path(r["file"]))
            if svg_path is not None:
                r["svg"] = str(svg_path)

    _print_cost()
    json.dump({"generated": results}, sys.stdout, indent=2)
    print()


# ─── TEXT OVERLAY ────────────────────────────────────────────

# Font discovery and Arabic shaping now live in captions.py, which is the module that
# does the hard version of this job (timed text, safe zones, per-line reshaping). Two
# copies of a font search path is one copy too many — when a face is added for the video
# captions, the still overlays must pick it up in the same change.
sys.path.insert(0, str(Path(__file__).parent))
from captions import (  # noqa: E402
    ARABIC_FONT_PATHS as _ARABIC_FONT_PATHS,
    LATIN_FONT_PATHS as _LATIN_FONT_PATHS,
    find_font as _find_font,
    has_arabic as _has_arabic,
    reshape_arabic as _reshape_arabic,
)


def overlay_text_on_image(
    image_path: str,
    texts: list[dict],
    output_path: str | None = None,
) -> Path:
    """
    Overlay text on an image with proper Arabic support.

    Each text dict:
      {
        "text": "string",
        "position": "top" | "center" | "bottom" | (x, y),
        "color": "#FFFFFF",
        "size": 60,
        "stroke_color": "#000000",
        "stroke_width": 3,
        "bg_color": null,
        "bg_padding": 20,
      }
    """
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(image_path).convert("RGBA")
    draw = ImageDraw.Draw(img)
    img_w, img_h = img.size

    for t in texts:
        text = t["text"]
        color = t.get("color", "#FFFFFF")
        font_size = t.get("size", 60)
        stroke_color = t.get("stroke_color")
        stroke_width = t.get("stroke_width", 0)
        bg_color = t.get("bg_color")
        bg_padding = t.get("bg_padding", 20)
        position = t.get("position", "center")

        is_arabic = _has_arabic(text)

        # Find and load font
        if is_arabic:
            font_path = _find_font(_ARABIC_FONT_PATHS)
        else:
            font_path = _find_font(_LATIN_FONT_PATHS)

        if font_path:
            try:
                font = ImageFont.truetype(font_path, font_size)
            except Exception:
                font = ImageFont.load_default(font_size)
        else:
            font = ImageFont.load_default(font_size)

        # Reshape Arabic for correct rendering
        if is_arabic:
            display_text = _reshape_arabic(text)
        else:
            display_text = text

        # Calculate text size
        bbox = draw.textbbox((0, 0), display_text, font=font, stroke_width=stroke_width)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        # Calculate position
        if isinstance(position, (list, tuple)):
            x, y = position
        elif position == "top":
            x = (img_w - text_w) // 2
            y = int(img_h * 0.08)
        elif position == "bottom":
            x = (img_w - text_w) // 2
            y = int(img_h * 0.85) - text_h
        else:  # center
            x = (img_w - text_w) // 2
            y = (img_h - text_h) // 2

        # Draw background box
        if bg_color:
            box = [
                x - bg_padding,
                y - bg_padding,
                x + text_w + bg_padding,
                y + text_h + bg_padding,
            ]
            draw.rectangle(box, fill=bg_color)

        # Draw text with optional stroke
        draw.text(
            (x, y),
            display_text,
            font=font,
            fill=color,
            stroke_width=stroke_width if stroke_color else 0,
            stroke_fill=stroke_color,
        )

    # Save
    out = Path(output_path) if output_path else Path(image_path).with_stem(
        Path(image_path).stem + "_text"
    )
    img.save(str(out), "PNG")
    return out


def cmd_overlay(args):
    """Add text overlay to an existing image."""
    texts = []
    for i, text in enumerate(args.text):
        positions = args.position or []
        pos = positions[i] if i < len(positions) else "center"
        # Parse position: "top", "center", "bottom", or "x,y"
        if "," in pos and pos.replace(",", "").replace("-", "").isdigit():
            pos = tuple(int(v) for v in pos.split(","))

        t = {
            "text": text,
            "position": pos,
            "color": args.color,
            "size": args.size,
            "stroke_color": args.stroke_color,
            "stroke_width": args.stroke_width,
        }
        if args.bg_color:
            t["bg_color"] = args.bg_color
            t["bg_padding"] = args.bg_padding
        texts.append(t)

    output_dir = Path(args.output) if args.output else OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    out_path = output_dir / f"{args.prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    result = overlay_text_on_image(args.image, texts, str(out_path))
    print(f"  Saved: {result}", file=sys.stderr)

    metadata = {
        "type": "overlay",
        "source_image": args.image,
        "texts": [t["text"] for t in texts],
        "timestamp": datetime.now().isoformat(),
    }
    _save_metadata(result, metadata)

    json.dump({"generated": [{"file": str(result), "metadata": metadata}]}, sys.stdout, indent=2)
    print()


# ─── VIDEO SUBCOMMAND ───────────────────────────────────────

def cmd_video(args):
    """Route --model/--provider to a backend. `fast`/`standard` keep the original Gemini
    path; everything else goes over HTTP via video_providers."""
    sys.path.insert(0, str(Path(__file__).parent))
    from video_providers import VideoProviderError, resolve

    try:
        _, provider = resolve(args.model, args.provider)
    except VideoProviderError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    if provider == "gemini":
        _video_via_gemini(args)
    else:
        _video_via_http(args, provider)


def _video_via_gemini(args):
    from google.genai import types

    if args.reference or args.resolution:
        print(
            "Error: --reference/--resolution are not supported on the Gemini backend; "
            f"pick an HTTP-backed model ({', '.join(HTTP_VIDEO_MODELS)}) for those.",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.duration not in VALID_VIDEO_DURATIONS:
        allowed = ", ".join(str(d) for d in VALID_VIDEO_DURATIONS)
        print(f"Error: --duration {args.duration} not supported by {args.model} "
              f"(allowed: {allowed})", file=sys.stderr)
        sys.exit(2)

    if args.aspect not in ("16:9", "9:16"):
        print(f"Error: --aspect {args.aspect} not supported by {args.model} "
              f"(allowed: 16:9, 9:16)", file=sys.stderr)
        sys.exit(2)

    client = _get_google_client()
    model_name = VIDEO_MODELS[args.model]
    output_dir = Path(args.output) if args.output else OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    # Build request. duration_seconds must be sent explicitly — without it Veo returns its
    # own default length while the cost line below still bills for --duration.
    generate_config = {
        "aspect_ratio": args.aspect,
        "number_of_videos": 1,
        "duration_seconds": args.duration,
    }
    if not args.audio:
        generate_config["generate_audio"] = False

    # Image-to-video. --first-frame is the explicit spelling; --from-image is the original
    # flag and stays an alias for it.
    from PIL import Image

    image_ref = None
    first_frame = args.first_frame or args.from_image
    if first_frame:
        image_ref = Image.open(first_frame)
        print(f"Using first frame: {first_frame}", file=sys.stderr)

    if args.last_frame:
        if not first_frame:
            print("Error: --last-frame needs --first-frame (or --from-image) as well",
                  file=sys.stderr)
            sys.exit(2)
        generate_config["last_frame"] = Image.open(args.last_frame)
        print(f"Using last frame: {args.last_frame}", file=sys.stderr)

    print(f"Generating video with {model_name} ({args.duration}s)...", file=sys.stderr)

    try:
        if image_ref:
            operation = client.models.generate_videos(
                model=model_name,
                prompt=args.prompt,
                image=image_ref,
                config=types.GenerateVideosConfig(**generate_config),
            )
        else:
            operation = client.models.generate_videos(
                model=model_name,
                prompt=args.prompt,
                config=types.GenerateVideosConfig(**generate_config),
            )
    except Exception as e:
        print(f"Error starting video generation: {e}", file=sys.stderr)
        sys.exit(1)

    _track_cost(f"video_{args.model}", multiplier=args.duration)

    # Poll until done (5 min timeout)
    timeout = 300
    start = time.time()
    while not operation.done:
        if time.time() - start > timeout:
            print("Error: Video generation timed out after 5 minutes", file=sys.stderr)
            sys.exit(1)
        elapsed = int(time.time() - start)
        print(f"  Waiting... ({elapsed}s elapsed)", file=sys.stderr)
        time.sleep(10)
        operation = client.operations.get(operation)

    # Download
    results = []
    for idx, video in enumerate(operation.result.generated_videos):
        prefix = args.prefix
        if len(operation.result.generated_videos) > 1:
            prefix = f"{prefix}_{idx + 1}"

        filepath = _save_file(b"", output_dir, prefix, ".mp4")
        # Download the video file
        client.files.download(file=video.video)
        video.video.save(str(filepath))
        print(f"  Saved: {filepath}", file=sys.stderr)

        metadata = {
            "type": "video",
            "prompt": args.prompt,
            "model": model_name,
            "provider": "gemini",
            "duration": args.duration,
            "aspect_ratio": args.aspect,
            "generate_audio": args.audio,
            "first_frame": first_frame,
            "last_frame": args.last_frame,
            "timestamp": datetime.now().isoformat(),
        }
        _save_metadata(filepath, metadata)
        results.append({"file": str(filepath), "metadata": metadata})

    _print_cost()
    json.dump({"generated": results}, sys.stdout, indent=2)
    print()


def _video_via_http(args, provider_name: str):
    """OpenRouter / MiniMax: validate locally, gate the spend, submit, poll, download."""
    sys.path.insert(0, str(Path(__file__).parent))
    import video_providers as vp

    output_dir = Path(args.output) if args.output else OUTPUT_DIR

    try:
        spec, provider_name = vp.resolve(args.model, provider_name)
        caps = spec.caps[provider_name]
        request = vp.VideoRequest(
            model=args.model,
            provider=provider_name,
            prompt=args.prompt,
            duration=args.duration,
            aspect_ratio=args.aspect,
            resolution=args.resolution or caps.default_resolution,
            first_frame=args.first_frame or args.from_image,
            last_frame=args.last_frame,
            references=args.reference or [],
            generate_audio=args.audio,
        )
        vp.validate(request, caps)
        estimate = vp.estimate_cost(request, caps)
    except vp.VideoProviderError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    label = (
        f"{args.model} via {provider_name} — {request.duration}s at "
        f"{request.resolution or 'default'}"
    )
    _confirm_spend(estimate, label, args.yes)

    try:
        provider = vp.get_provider(provider_name)
        print(f"Submitting to {provider_name} ({label})...", file=sys.stderr)
        job_id = provider.submit(request, spec.backend_ids[provider_name], caps)
        print(f"  Job: {job_id}", file=sys.stderr)

        state = vp.wait_for(provider, job_id, timeout=args.timeout)
        video_bytes = provider.download(state["url"])
    except vp.VideoProviderError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    filepath = _save_file(video_bytes, output_dir, args.prefix, ".mp4")
    print(f"  Saved: {filepath}", file=sys.stderr)

    # Providers report the real charge late (MiniMax fills `usage` a beat after the task
    # flips to succeeded), so keep the local estimate when it isn't there yet.
    billed = state.get("cost")
    _track_cost_usd(billed if billed is not None else estimate)

    metadata = {
        "type": "video",
        "prompt": args.prompt,
        "model": args.model,
        "backend_model": spec.backend_ids[provider_name],
        "provider": provider_name,
        "job_id": job_id,
        "duration": request.duration,
        "resolution": request.resolution,
        "aspect_ratio": request.aspect_ratio,
        "generate_audio": request.generate_audio,
        "first_frame": request.first_frame,
        "last_frame": request.last_frame,
        "references": request.references,
        "estimated_cost": round(estimate, 4),
        "billed_cost": billed,
        "timestamp": datetime.now().isoformat(),
    }
    _save_metadata(filepath, metadata)

    _print_cost()
    json.dump({"generated": [{"file": str(filepath), "metadata": metadata}]},
              sys.stdout, indent=2)
    print()


def cmd_caps(args):
    """Print the live capability record for a model — checks MODEL_REGISTRY for drift."""
    sys.path.insert(0, str(Path(__file__).parent))
    import video_providers as vp

    try:
        spec, provider_name = vp.resolve(args.model, args.provider)
    except vp.VideoProviderError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    local = spec.caps[provider_name]
    payload = {
        "model": args.model,
        "provider": provider_name,
        "backend_id": spec.backend_ids[provider_name],
        "local": {
            "durations": list(local.durations),
            "resolutions": list(local.resolutions),
            "aspect_ratios": list(local.aspect_ratios),
            "frame_types": list(local.frame_types),
            "audio": local.audio,
            "seed": local.seed,
            "price_per_second": local.price_per_second,
        },
    }

    if provider_name == "openrouter":
        try:
            payload["live"] = vp.refresh_openrouter_caps(spec.backend_ids[provider_name])
        except vp.VideoProviderError as e:
            payload["live_error"] = str(e)
    else:
        payload["live"] = None
        payload["note"] = (
            "MiniMax publishes no capability endpoint; the local record mirrors "
            "platform.minimax.io/docs/guides/video-generation"
        )

    json.dump(payload, sys.stdout, indent=2)
    print()


# ─── VOICE SUBCOMMAND ───────────────────────────────────────

def cmd_voice(args):
    from elevenlabs import ElevenLabs

    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        print("Error: ELEVENLABS_API_KEY not set. Add to ~/.zshrc or ~/.env", file=sys.stderr)
        sys.exit(1)

    client = ElevenLabs(api_key=api_key)
    model_id = VOICE_MODELS[args.model]
    output_dir = Path(args.output) if args.output else OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    # Get text
    if args.file:
        with open(args.file) as f:
            text = f.read().strip()
    elif args.text:
        text = args.text
    else:
        print("Error: Provide --text or --file", file=sys.stderr)
        sys.exit(1)

    # Resolve voice ID
    voice_id = VOICE_PRESETS.get(args.voice, args.voice)  # preset name or raw ID

    print(f"Generating voiceover with {model_id}...", file=sys.stderr)
    print(f"  Voice: {args.voice} ({voice_id})", file=sys.stderr)
    print(f"  Text length: {len(text)} chars", file=sys.stderr)

    try:
        audio_generator = client.text_to_speech.convert(
            text=text,
            voice_id=voice_id,
            model_id=model_id,
            output_format="mp3_44100_128",
        )
        # Collect audio bytes from generator
        audio_data = b"".join(audio_generator)
    except Exception as e:
        print(f"Error generating voice: {e}", file=sys.stderr)
        sys.exit(1)

    _track_cost(f"voice_{args.model}", multiplier=len(text))

    filepath = _save_file(audio_data, output_dir, args.prefix, ".mp3")
    print(f"  Saved: {filepath}", file=sys.stderr)

    metadata = {
        "type": "voice",
        "text": text[:200] + ("..." if len(text) > 200 else ""),
        "text_length": len(text),
        "voice": args.voice,
        "voice_id": voice_id,
        "model": model_id,
        "timestamp": datetime.now().isoformat(),
    }
    _save_metadata(filepath, metadata)

    _print_cost()
    json.dump({"generated": [{"file": str(filepath), "metadata": metadata}]}, sys.stdout, indent=2)
    print()


# ─── STORY SUBCOMMAND ───────────────────────────────────────

def _gemini_image(prompt: str, model_key: str, aspect: str, size: str,
                  references: list) -> bytes:
    """One image, returned as bytes. A focused twin of cmd_image's generation core —
    kept separate so the story pipeline can't regress the image command."""
    from google.genai import types

    if size == "4K" and model_key != "pro":
        print("Warning: 4K requires the pro model. Switching to pro...", file=sys.stderr)
        model_key = "pro"

    client = _get_google_client()
    model_name = IMAGE_MODELS[model_key]

    config_kwargs = {"aspect_ratio": aspect}
    if model_key == "pro":
        config_kwargs["image_size"] = size

    contents = prompt
    if references:
        from PIL import Image
        parts = []
        for reference in references[:14]:
            try:
                parts.append(Image.open(reference))
                print(f"  Loaded reference: {reference}", file=sys.stderr)
            except Exception as e:
                print(f"  Failed to load {reference}: {e}", file=sys.stderr)
        parts.append(prompt)
        contents = parts

    response = client.models.generate_content(
        model=model_name,
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(**config_kwargs),
        ),
    )
    _track_cost(f"image_{model_key}")

    for part in response.parts:
        if part.inline_data is not None:
            data = _extract_image_bytes(part)
            if data:
                return data
    raise RuntimeError("Gemini returned no image for the board prompt")


def _load_story(args):
    """Load + validate a spec, echo warnings, and resolve the working directory."""
    sys.path.insert(0, str(Path(__file__).parent))
    import storyboard as sb

    spec_path = Path(args.spec).expanduser().resolve()
    if not spec_path.is_file():
        print(f"Error: spec not found: {spec_path}", file=sys.stderr)
        sys.exit(2)

    try:
        spec = sb.load_spec(spec_path)
        warnings = sb.validate_spec(spec)
    except sb.StorySpecError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)

    workdir = (
        Path(args.workdir).expanduser().resolve() if args.workdir
        else spec_path.parent / f"{spec_path.stem}-story"
    )
    return sb, spec, spec_path, workdir


def _story_video_target(spec, args):
    """(spec_obj, provider) for the story's model, honouring CLI overrides."""
    sys.path.insert(0, str(Path(__file__).parent))
    import video_providers as vp

    model = getattr(args, "model", None) or spec.get("model", "hailuo-3")
    provider = getattr(args, "provider", None) or spec.get("provider", "auto")
    try:
        model_spec, resolved = vp.resolve(model, provider)
    except vp.VideoProviderError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    if resolved == "gemini":
        print(
            f"Error: story runs on the HTTP backends only; {model!r} resolves to the "
            f"Gemini SDK path. Use one of: {', '.join(HTTP_VIDEO_MODELS)}.",
            file=sys.stderr,
        )
        sys.exit(2)

    return model_spec, resolved, model


def _story_clip_requests(sb, spec, args, workdir):
    """Build one VideoRequest per clip, with panel paths attached. Validates every clip
    before any of them are submitted."""
    import video_providers as vp

    model_spec, provider, model = _story_video_target(spec, args)
    caps = model_spec.caps[provider]
    panels_dir = workdir / "panels"

    plan = sb.clip_plan(spec)
    if args.only:
        wanted = set(args.only)
        plan = [entry for entry in plan if entry["id"] in wanted]
        missing = wanted - {entry["id"] for entry in plan}
        if missing:
            print(
                f"Error: --only names shot(s) with no clip: {', '.join(sorted(missing))}",
                file=sys.stderr,
            )
            sys.exit(2)

    built = []
    for entry in plan:
        first = panels_dir / f"panel-{entry['first_panel']:02d}.png"
        last = (
            panels_dir / f"panel-{entry['last_panel']:02d}.png"
            if entry["last_panel"] else None
        )
        request = vp.VideoRequest(
            model=model,
            provider=provider,
            prompt=sb.compile_shot_prompt(entry, spec),
            duration=entry["duration"],
            aspect_ratio=spec.get("aspect"),
            # CLI override first: the whole point of the 768P tier is drafting a spec
            # written for 2K without maintaining a second copy of it.
            resolution=(getattr(args, "resolution", None) or spec.get("resolution")
                        or caps.default_resolution),
            first_frame=str(first),
            last_frame=str(last) if last else None,
            generate_audio=caps.audio,
        )
        try:
            vp.validate(request, caps)
        except vp.VideoProviderError as e:
            print(f"Error: shot {entry['id']}: {e}", file=sys.stderr)
            sys.exit(2)

        built.append({
            "entry": entry,
            "request": request,
            "first": first,
            "last": last,
            "cost": vp.estimate_cost(request, caps),
        })

    return built, model_spec, provider, caps


def cmd_story_plan(args):
    """Dry run: every compiled prompt and the full cost, without spending anything."""
    sb, spec, spec_path, workdir = _load_story(args)
    cols, rows = sb.grid_for(spec)
    clips, _, provider, _ = _story_clip_requests(sb, spec, args, workdir)

    board_prompt = sb.compile_board_prompt(spec)
    board_model = (spec.get("board") or {}).get("image_model", "pro")
    board_cost = _COST_MAP.get(f"image_{board_model}", 0.0)

    print(f"\n=== BOARD ({cols}x{rows}, {board_model}) ===\n", file=sys.stderr)
    print(board_prompt, file=sys.stderr)

    for clip in clips:
        entry = clip["entry"]
        frames = f"panel-{entry['first_panel']:02d}"
        if entry["last_panel"]:
            frames += f" -> panel-{entry['last_panel']:02d}"
        print(f"\n=== CLIP {entry['id']} ({frames}, {entry['duration']}s, "
              f"~${clip['cost']:.2f}) ===\n", file=sys.stderr)
        print(clip["request"].prompt, file=sys.stderr)

    clips_cost = sum(clip["cost"] for clip in clips)
    print(
        f"\nTotal: 1 board (~${board_cost:.2f}) + {len(clips)} clips "
        f"(~${clips_cost:.2f}) = ~${board_cost + clips_cost:.2f} via {provider}\n",
        file=sys.stderr,
    )

    json.dump({
        "spec": str(spec_path),
        "workdir": str(workdir),
        "provider": provider,
        "grid": [cols, rows],
        "board_prompt": board_prompt,
        "clips": [
            {
                "id": clip["entry"]["id"],
                "first_panel": clip["entry"]["first_panel"],
                "last_panel": clip["entry"]["last_panel"],
                "duration": clip["entry"]["duration"],
                "estimated_cost": round(clip["cost"], 4),
                "prompt": clip["request"].prompt,
            }
            for clip in clips
        ],
        "estimated_cost": round(board_cost + clips_cost, 4),
    }, sys.stdout, indent=2)
    print()


def _suggest_grid(sb, spec) -> str:
    """The grid whose cells come closest to the clip's real shape.

    Searches cols and rows independently rather than deriving one from the other,
    because the best answer is often SQUARE — a square grid preserves the cell aspect
    exactly, whatever the clip ratio — and a square grid usually has spare cells. Spare
    cells cost nothing extra: the sheet is one image call either way, and a discarded
    cell is far cheaper than every panel composed for the wrong shape.
    """
    count = len(spec["shots"])
    limit = max(count + 2, round(count * 1.7))
    best, score = None, None
    for cols in range(1, count + 1):
        for rows in range(1, count + 1):
            cells = cols * rows
            if cells < count or cells > limit:
                continue
            _, error = sb.board_aspect({**spec, "board": {"cols": cols, "rows": rows}})
            # Prefer the truer cell; break ties toward fewer wasted cells.
            key = (round(error, 3), cells)
            if score is None or key < score:
                best, score = f"{cols}x{rows}", key
    return f"{best} ({score[0] * 100:.0f}% off)" if best else "a different shot count"


def cmd_story_board(args):
    """Phase 1: one image call -> a contact sheet -> sliced panels."""
    sb, spec, spec_path, workdir = _load_story(args)
    cols, rows = sb.grid_for(spec)
    board = spec.get("board") or {}
    board_model = board.get("image_model", "pro")
    board_size = board.get("size", "4K")
    inset = float(board.get("inset", 0.03))

    # Cast references travel with the spec, so resolve them relative to it.
    references = [
        str((spec_path.parent / path).resolve() if not Path(path).is_absolute() else path)
        for path in (spec.get("cast") or {}).values()
    ]

    # The sheet is a GRID of clips, so it is requested at the grid's aspect, not the
    # clip's. They coincide only when cols == rows.
    sheet_aspect, cell_error = sb.board_aspect(spec)
    if cell_error > 0.12:
        print(
            f"Warning: a {cols}x{rows} grid of {spec.get('aspect', '16:9')} cells wants a "
            f"sheet the backend does not offer; nearest is {sheet_aspect}, which draws "
            f"each cell {cell_error * 100:.0f}% off its final shape. Panels will be "
            f"letterboxed or cropped by the video model.\n"
            f"  Pick a grid whose aspect is closer — for {len(spec['shots'])} cells at "
            f"{spec.get('aspect', '16:9')}, try "
            f"{_suggest_grid(sb, spec)} — or set board.aspect explicitly.",
            file=sys.stderr,
        )

    prompt = sb.compile_board_prompt(spec)
    estimate = _COST_MAP.get(f"image_{board_model}", 0.0)
    _confirm_spend(estimate, f"1 storyboard sheet ({cols}x{rows}, {board_model})", args.yes)

    print(f"Generating {cols}x{rows} contact sheet at {sheet_aspect}...", file=sys.stderr)
    try:
        sheet_bytes = _gemini_image(
            prompt, board_model, sheet_aspect, board_size, references
        )
    except Exception as e:
        print(f"Error generating the board: {e}", file=sys.stderr)
        sys.exit(1)

    workdir.mkdir(parents=True, exist_ok=True)
    sheet_path = workdir / "board.png"
    sheet_path.write_bytes(sheet_bytes)
    print(f"  Saved: {sheet_path}", file=sys.stderr)

    try:
        panels = sb.slice_contact_sheet(
            sheet_path, cols, rows, workdir / "panels",
            inset=inset, count=len(spec["shots"]),
            autotrim=board.get("autotrim", True),
        )
    except sb.StorySpecError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    for panel in panels:
        print(f"  Panel: {panel}", file=sys.stderr)

    sb.merge_manifest(workdir, {
        "spec": str(spec_path),
        "spec_sha256": sb.sha256_of(spec_path),
        "board": {
            "prompt": prompt,
            "model": IMAGE_MODELS[board_model if board_size != "4K" else "pro"],
            "grid": [cols, rows],
            "inset": inset,
            "sheet": str(sheet_path),
            "sheet_sha256": sb.sha256_of(sheet_path),
            "panels": {p.name: sb.sha256_of(p) for p in panels},
            "timestamp": datetime.now().isoformat(),
        },
    })

    _print_cost()
    json.dump({
        "board": str(sheet_path),
        "panels": [str(p) for p in panels],
        "workdir": str(workdir),
    }, sys.stdout, indent=2)
    print()


def cmd_story_shots(args):
    """Phase 2: chain the panels through the video model, one clip per pair."""
    import video_providers as vp

    sb, spec, spec_path, workdir = _load_story(args)
    clips, model_spec, provider_name, _ = _story_clip_requests(sb, spec, args, workdir)

    missing = [str(c["first"]) for c in clips if not c["first"].is_file()]
    missing += [str(c["last"]) for c in clips if c["last"] and not c["last"].is_file()]
    if missing:
        print(
            "Error: missing panel(s): " + ", ".join(sorted(set(missing)))
            + "\n  Run `story board` first (or re-run it if you changed the shot list).",
            file=sys.stderr,
        )
        sys.exit(2)

    total = sum(clip["cost"] for clip in clips)
    _confirm_spend(
        total,
        f"{len(clips)} clip(s) on {spec.get('model', 'hailuo-3')} via {provider_name}",
        args.yes,
    )

    clips_dir = workdir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    try:
        provider = vp.get_provider(provider_name)
    except vp.VideoProviderError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    generated, failures = [], []
    for position, clip in enumerate(clips, 1):
        entry = clip["entry"]
        print(f"\n[{position}/{len(clips)}] {entry['id']} "
              f"({entry['duration']}s, ~${clip['cost']:.2f})", file=sys.stderr)

        try:
            job_id = provider.submit(
                clip["request"], model_spec.backend_ids[provider_name],
                model_spec.caps[provider_name],
            )
            print(f"  Job: {job_id}", file=sys.stderr)
            state = vp.wait_for(provider, job_id, timeout=args.timeout)
            video_bytes = provider.download(state["url"])
        except vp.VideoProviderError as e:
            # Keep going: one refused shot shouldn't discard the clips already paid for.
            print(f"  Failed: {e}", file=sys.stderr)
            failures.append({"id": entry["id"], "error": str(e)})
            continue

        clip_path = clips_dir / sb.clip_filename(entry)
        clip_path.write_bytes(video_bytes)
        print(f"  Saved: {clip_path}", file=sys.stderr)

        billed = state.get("cost")
        _track_cost_usd(billed if billed is not None else clip["cost"])

        record = {
            "id": entry["id"],
            "file": str(clip_path),
            "sha256": sb.sha256_of(clip_path),
            "job_id": job_id,
            "provider": provider_name,
            "model": model_spec.backend_ids[provider_name],
            "duration": entry["duration"],
            "resolution": clip["request"].resolution,
            "first_frame": clip["first"].name,
            "last_frame": clip["last"].name if clip["last"] else None,
            "prompt": clip["request"].prompt,
            "estimated_cost": round(clip["cost"], 4),
            "billed_cost": billed,
            "timestamp": datetime.now().isoformat(),
        }
        generated.append(record)
        # Written per clip, not at the end — a crash on clip 6 must not lose clips 1-5.
        sb.merge_manifest(workdir, {"clips": {entry["id"]: record}})

    _print_cost()
    if failures:
        print(f"\n{len(failures)} shot(s) failed; re-run just those with "
              f"--only {' '.join(f['id'] for f in failures)}", file=sys.stderr)

    json.dump({"clips": generated, "failed": failures}, sys.stdout, indent=2)
    print()
    if failures and not generated:
        sys.exit(1)


def cmd_story_regenerate(args):
    """Promote approved 768P drafts to 2K, reusing each draft rather than re-rolling it."""
    import video_providers as vp

    sb, spec, spec_path, workdir = _load_story(args)
    manifest_path = workdir / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: no manifest at {manifest_path} — run `story shots` first",
              file=sys.stderr)
        sys.exit(2)

    recorded = (json.loads(manifest_path.read_text()).get("clips") or {})
    wanted = set(args.only) if args.only else None

    eligible, skipped = [], []
    for entry in sb.clip_plan(spec):
        clip_id = entry["id"]
        if wanted and clip_id not in wanted:
            continue
        record = recorded.get(clip_id)
        if not record:
            skipped.append((clip_id, "not generated yet"))
        elif record.get("provider") != "minimax":
            skipped.append((clip_id, f"came from {record.get('provider')}; regeneration "
                                     "is MiniMax-direct only"))
        elif record.get("resolution") == "2K":
            skipped.append((clip_id, "already 2K"))
        elif not record.get("job_id"):
            skipped.append((clip_id, "no source task id recorded"))
        else:
            eligible.append((entry, record))

    for clip_id, why in skipped:
        print(f"Skipping {clip_id}: {why}", file=sys.stderr)
    if not eligible:
        print("Error: nothing eligible to regenerate.", file=sys.stderr)
        sys.exit(1)

    total = sum(vp.MINIMAX_REGEN_PRICE * e["duration"] for e, _ in eligible)
    _confirm_spend(total, f"{len(eligible)} clip(s) re-rendered 768P → 2K", args.yes)

    try:
        provider = vp.get_provider("minimax")
    except vp.VideoProviderError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    clips_dir = workdir / "clips"
    drafts_dir = clips_dir / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)

    promoted, failures = [], []
    for position, (entry, record) in enumerate(eligible, 1):
        estimate = vp.MINIMAX_REGEN_PRICE * entry["duration"]
        print(f"\n[{position}/{len(eligible)}] {entry['id']} "
              f"({entry['duration']}s, ~${estimate:.2f})", file=sys.stderr)

        try:
            task_id = provider.regenerate(record["job_id"], resolution="2K")
            print(f"  Task: {task_id}", file=sys.stderr)
            state = vp.wait_for(provider, task_id, timeout=args.timeout)
            video_bytes = provider.download(state["url"])
        except vp.VideoProviderError as e:
            print(f"  Failed: {e}", file=sys.stderr)
            failures.append({"id": entry["id"], "error": str(e)})
            continue

        clip_path = clips_dir / sb.clip_filename(entry)
        # Keep the draft: it is the thing that was approved, and the 2K render is only
        # trustworthy insofar as it matches it.
        if clip_path.exists():
            clip_path.replace(drafts_dir / clip_path.name)
        clip_path.write_bytes(video_bytes)
        print(f"  Saved: {clip_path}  (draft kept in {drafts_dir.name}/)", file=sys.stderr)

        billed = state.get("cost")
        _track_cost_usd(billed if billed is not None else estimate)

        updated = dict(record)
        updated.update({
            "resolution": "2K",
            "sha256": sb.sha256_of(clip_path),
            "regenerated_from": record["job_id"],
            "job_id": task_id,
            "draft_file": str(drafts_dir / clip_path.name),
            "draft_cost": record.get("billed_cost"),
            "estimated_cost": round(estimate, 4),
            "billed_cost": billed,
            "timestamp": datetime.now().isoformat(),
        })
        sb.merge_manifest(workdir, {"clips": {entry["id"]: updated}})
        promoted.append(updated)

    _print_cost()
    if failures:
        print(f"\n{len(failures)} failed; retry with "
              f"--only {' '.join(f['id'] for f in failures)}", file=sys.stderr)
    json.dump({"regenerated": promoted, "failed": failures}, sys.stdout, indent=2)
    print()
    if failures and not promoted:
        sys.exit(1)


def cmd_story_assemble(args):
    """Phase 3: concatenate the clips in shot order."""
    sb, spec, spec_path, workdir = _load_story(args)
    plan = sb.clip_plan(spec)

    clips_dir = workdir / "clips"
    ordered = [clips_dir / sb.clip_filename(entry) for entry in plan]

    output = (
        Path(args.output).expanduser().resolve() if args.output
        else workdir / f"{spec.get('title') or spec_path.stem}.mp4"
    )

    try:
        sb.assemble(ordered, output)
    except sb.StorySpecError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  Saved: {output}", file=sys.stderr)
    sb.merge_manifest(workdir, {
        "assembled": {
            "file": str(output),
            "sha256": sb.sha256_of(output),
            "clips": [c.name for c in ordered],
            "timestamp": datetime.now().isoformat(),
        }
    })

    json.dump({"video": str(output), "clips": [str(c) for c in ordered]},
              sys.stdout, indent=2)
    print()


# ─── CLI ─────────────────────────────────────────────────────

# ─── AD MODE ──────────────────────────────────────────────────
#
# An ad spec compiles DOWN into an ordinary story spec, which is then run through the
# story machinery unchanged. That is the whole design: `ad` adds a format vocabulary, a
# platform safe zone, a product that must stay on-model and burned-in text, and adds
# nothing at all to the board/shots/assemble path that was already proven.

def _load_ad(args):
    """Load + compile an ad spec. Returns (adspec, spec, plan, spec_path, workdir)."""
    sys.path.insert(0, str(Path(__file__).parent))
    import adspec as ad

    spec_path = Path(args.spec).expanduser().resolve()
    if not spec_path.is_file():
        print(f"Error: spec not found: {spec_path}", file=sys.stderr)
        sys.exit(2)

    try:
        spec = ad.load_ad_spec(spec_path)
        plan = ad.compile_to_story(spec)
    except ad.AdSpecError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    for warning in plan["warnings"]:
        print(f"Warning: {warning}", file=sys.stderr)

    workdir = (
        Path(args.workdir).expanduser().resolve() if args.workdir
        else spec_path.parent / f"{spec_path.stem}-ad"
    )

    # The product instruction goes into the story's STYLE, not just the board prompt.
    # storyboard threads style through both compile_board_prompt and compile_shot_prompt,
    # and the label has to survive the video pass too — a panel that renders the packaging
    # correctly is worth nothing if the clip animating it invents new lettering.
    extra = ad.compile_board_prompt_extra(spec)
    if extra:
        base = (plan["story"].get("style") or "").rstrip()
        if base and not base.endswith((".", "!", "?")):
            base += "."
        plan["story"]["style"] = " ".join(part for part in (base, extra) if part)

    # Paths inside an ad spec are relative to the spec, but the compiled story lands in
    # the workdir. Re-anchor them or the board call looks for cast refs that aren't there.
    for name, ref in list((plan["story"].get("cast") or {}).items()):
        resolved = (spec_path.parent / ref).resolve()
        plan["story"]["cast"][name] = str(resolved)

    return ad, spec, plan, spec_path, workdir


def _write_compiled_story(plan, workdir) -> Path:
    """Persist the compiled story so every story phase reads exactly one artifact — and
    so the user can look at what their ad spec actually became."""
    workdir.mkdir(parents=True, exist_ok=True)
    story_path = workdir / "story.json"
    story_path.write_text(json.dumps(plan["story"], indent=2, ensure_ascii=False) + "\n")
    return story_path


def _ad_story_args(args, story_path, workdir, **extra):
    """A Namespace the existing cmd_story_* functions accept unchanged."""
    import argparse as _argparse
    shim = _argparse.Namespace(
        spec=str(story_path), workdir=str(workdir),
        model=getattr(args, "model", None), provider=getattr(args, "provider", None),
        resolution=getattr(args, "resolution", None), only=getattr(args, "only", None),
        yes=getattr(args, "yes", False), timeout=getattr(args, "timeout", 900),
        output=getattr(args, "output", None),
    )
    for key, value in extra.items():
        setattr(shim, key, value)
    return shim


def _ad_platform_size(plan):
    """(width, height) the captions should be laid out for."""
    safe = plan["safe"]
    return safe["width"], safe["height"]


def cmd_ad_plan(args):
    """Dry run: the beat sheet, the compiled prompts, the cues and the total cost."""
    ad, spec, plan, spec_path, workdir = _load_ad(args)
    story_path = _write_compiled_story(plan, workdir)

    print(f"\n=== {spec.get('title', spec_path.stem)} — {plan['format']} on "
          f"{plan['platform']} ({plan['story']['aspect']}, "
          f"{plan['total_duration']}s) ===\n", file=sys.stderr)
    for beat in plan["beats"]:
        print(f"  {beat['index'] + 1}. {beat['id']:<12} {beat['duration']:>2}s  "
              f"{beat['start']:>5.1f} → {beat['end']:<5.1f}", file=sys.stderr)

    safe = plan["safe"]
    print(f"\n  safe box: {safe['box']} of {safe['width']}x{safe['height']} "
          f"(top {safe['top']}px, right {safe['right']}px, bottom {safe['bottom']}px)",
          file=sys.stderr)

    if plan["cues"]:
        print("\n=== BURNED TEXT ===", file=sys.stderr)
        for cue in plan["cues"]:
            print(f"  {cue['start']:>5.1f}-{cue['end']:<5.1f} {cue['role']:<11} "
                  f"{cue['text']}", file=sys.stderr)

    for claim in ad.check_claims(spec):
        print(f"Warning: {claim}", file=sys.stderr)

    story_args = _ad_story_args(args, story_path, workdir)
    cmd_story_plan(story_args)


def cmd_ad_preview(args):
    """Render the safe-zone guide and every caption still — spends nothing.

    This is the phase that pays for itself: text placed under the platform's UI is copy
    nobody reads, and finding that out after paying for five clips is the expensive way.
    """
    sys.path.insert(0, str(Path(__file__).parent))
    import captions as cap

    ad, spec, plan, spec_path, workdir = _load_ad(args)
    _write_compiled_story(plan, workdir)
    width, height = _ad_platform_size(plan)

    out_dir = workdir / "preview"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        cues = cap.parse_cues(plan["cues"])
    except cap.CaptionError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    for warning in cap.overlaps(cues):
        print(f"Warning: {warning}", file=sys.stderr)

    guide = cap.safe_guide(width, height, plan["safe"], out_dir / "safe-zone.png")
    rendered = cap.render_cues(cues, width, height, plan["safe"], out_dir)

    srt_path = out_dir / "captions.srt"
    srt_path.write_text(cap.srt_from_cues(cues), encoding="utf-8")

    print(f"\nGuide:    {guide}", file=sys.stderr)
    for item in rendered:
        print(f"  {item['cue']['role']:<11} {item['png']}", file=sys.stderr)
    print(f"Subtitles: {srt_path}\n", file=sys.stderr)

    json.dump({
        "guide": str(guide), "srt": str(srt_path),
        "cues": [{"role": r["cue"]["role"], "start": r["cue"]["start"],
                  "end": r["cue"]["end"], "png": str(r["png"])} for r in rendered],
        "safe": {k: v for k, v in plan["safe"].items() if k != "box"} | {
            "box": list(plan["safe"]["box"])},
    }, sys.stdout, indent=2, ensure_ascii=False)
    print()


def cmd_ad_board(args):
    ad, spec, plan, spec_path, workdir = _load_ad(args)
    story_path = _write_compiled_story(plan, workdir)
    cmd_story_board(_ad_story_args(args, story_path, workdir))


def cmd_ad_shots(args):
    ad, spec, plan, spec_path, workdir = _load_ad(args)
    story_path = _write_compiled_story(plan, workdir)
    cmd_story_shots(_ad_story_args(args, story_path, workdir))


def cmd_ad_regenerate(args):
    ad, spec, plan, spec_path, workdir = _load_ad(args)
    story_path = _write_compiled_story(plan, workdir)
    cmd_story_regenerate(_ad_story_args(args, story_path, workdir))


def cmd_ad_assemble(args):
    """Concatenate the clips, then burn the text unless --no-captions."""
    sys.path.insert(0, str(Path(__file__).parent))
    import captions as cap
    import storyboard as sb

    ad, spec, plan, spec_path, workdir = _load_ad(args)
    story_path = _write_compiled_story(plan, workdir)

    silent = workdir / f"{spec_path.stem}-silent.mp4"
    cmd_story_assemble(_ad_story_args(args, story_path, workdir, output=str(silent)))

    final = Path(args.output).expanduser() if args.output else \
        workdir / f"{spec_path.stem}.mp4"

    burn = plan["cues"] and not args.no_captions \
        and (spec.get("captions") or {}).get("burn", True)
    if not burn:
        shutil_move = silent.replace(final)
        print(f"\nFinal (no captions): {shutil_move}", file=sys.stderr)
        json.dump({"output": str(final), "captions": False}, sys.stdout, indent=2)
        print()
        return

    size = sb.probe_size(silent)
    width, height = size if size else _ad_platform_size(plan)
    # The safe box is fractional, so re-derive it at the clip's REAL size. A 768P draft
    # and a 2K finish are the same ad; the insets are the same percentages of a
    # different pixel count.
    safe = ad.safe_zone(plan["platform"], width, height)

    # Re-time against the clips that actually came back. The model does not honour the
    # duration it was asked for — H3 answers a 6s request with 6.583s — so cues timed on
    # the plan drift further out of sync with every beat.
    story_spec = sb.load_spec(story_path)
    clips_dir = workdir / "clips"
    measured = [
        sb.probe_duration(clips_dir / sb.clip_filename(entry))
        for entry in sb.clip_plan(story_spec)
    ]
    if all(seconds is not None for seconds in measured):
        planned = sum(beat["duration"] for beat in plan["beats"])
        real = sum(measured)
        if abs(real - planned) > 0.25:
            print(f"Note: clips total {real:.2f}s against a planned {planned}s — "
                  f"re-timing {len(plan['cues'])} cue(s) onto the real footage.",
                  file=sys.stderr)
        plan["cues"] = ad.retime_cues(plan["cues"], plan["beats"], measured)

    try:
        cues = cap.parse_cues(plan["cues"])
        cap.burn(silent, cues, final, width=width, height=height, safe=safe,
                 work_dir=workdir / "captions")
    except cap.CaptionError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    srt_path = final.with_suffix(".srt")
    srt_path.write_text(cap.srt_from_cues(cues), encoding="utf-8")

    print(f"\nFinal: {final}\nSubtitles: {srt_path}", file=sys.stderr)
    json.dump({"output": str(final), "silent": str(silent), "srt": str(srt_path),
               "captions": len(cues), "size": [width, height]},
              sys.stdout, indent=2)
    print()


def cmd_ad_hooks(args):
    """Render N alternate takes of the opening beat and assemble one ad per take.

    The hook is the only thing that varies, so the body clips are rendered once and
    reused by every variant: N variants of a five-beat ad cost `4 + N` clips, not `5N`.
    H3 has no seed, so re-submitting the same hook prompt returns a genuinely different
    performance — which is exactly what a hook test wants.
    """
    import video_providers as vp
    sys.path.insert(0, str(Path(__file__).parent))
    import admux as mx
    import storyboard as sb

    ad, spec, plan, spec_path, workdir = _load_ad(args)
    story_path = _write_compiled_story(plan, workdir)
    story_args = _ad_story_args(args, story_path, workdir)

    story_spec = sb.load_spec(story_path)
    entries = sb.clip_plan(story_spec)
    clip_ids = [entry["id"] for entry in entries]

    hook_index = 0
    if args.beat:
        if args.beat not in clip_ids:
            print(f"Error: --beat {args.beat!r} is not a clip in this ad "
                  f"(have: {', '.join(clip_ids)})", file=sys.stderr)
            sys.exit(2)
        hook_index = clip_ids.index(args.beat)

    try:
        vplan = mx.variant_plan(clip_ids, args.count, hook_index=hook_index)
    except mx.MuxError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    clips_dir = workdir / "clips"
    body = [clips_dir / sb.clip_filename(entries[i])
            for i in range(len(entries)) if i != hook_index]
    missing = [str(p) for p in body if not p.is_file()]
    if missing:
        print("Error: the body clips must exist before varying the hook: "
              + ", ".join(missing) + "\n  Run `ad shots` first.", file=sys.stderr)
        sys.exit(2)

    requests, model_spec, provider_name, _ = _story_clip_requests(
        sb, story_spec, _ad_story_args(args, story_path, workdir,
                                       only=[clip_ids[hook_index]]), workdir)
    if not requests:
        print(f"Error: no clip request for beat {clip_ids[hook_index]!r}", file=sys.stderr)
        sys.exit(2)
    request = requests[0]

    print(f"\n{args.count} take(s) of {clip_ids[hook_index]!r}; "
          f"{len(body)} body clip(s) reused.\n"
          f"  render {vplan['clips_to_render']} clips instead of "
          f"{vplan['clips_if_naive']} — {vplan['clips_saved']} saved.",
          file=sys.stderr)
    _confirm_spend(request["cost"] * args.count,
                   f"{args.count} hook take(s) via {provider_name}", args.yes)

    try:
        provider = vp.get_provider(provider_name)
    except vp.VideoProviderError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    hooks_dir = clips_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    made, failures = [], []

    for variant in vplan["variants"]:
        label = variant["id"]
        print(f"\n[{variant['n']}/{args.count}] {label} "
              f"({request['entry']['duration']}s, ~${request['cost']:.2f})",
              file=sys.stderr)
        try:
            job_id = provider.submit(
                request["request"], model_spec.backend_ids[provider_name],
                model_spec.caps[provider_name],
            )
            print(f"  Job: {job_id}", file=sys.stderr)
            state = vp.wait_for(provider, job_id, timeout=args.timeout)
            video_bytes = provider.download(state["url"])
        except vp.VideoProviderError as e:
            print(f"  Failed: {e}", file=sys.stderr)
            failures.append({"id": label, "error": str(e)})
            continue

        take = hooks_dir / f"{hook_index + 1:02d}-{label}.mp4"
        take.write_bytes(video_bytes)
        billed = state.get("cost")
        _track_cost_usd(billed if billed is not None else request["cost"])

        ordered = list(body)
        ordered.insert(hook_index, take)
        out = workdir / mx.variant_filename(spec_path.stem, label)
        try:
            mx.assemble_variant(ordered, out)
        except mx.MuxError as e:
            print(f"  Assembly failed: {e}", file=sys.stderr)
            failures.append({"id": label, "error": str(e)})
            continue

        print(f"  Saved: {out}", file=sys.stderr)
        record = {"id": label, "take": str(take), "file": str(out),
                  "job_id": job_id, "sha256": sb.sha256_of(out),
                  "timestamp": datetime.now().isoformat()}
        made.append(record)
        sb.merge_manifest(workdir, {"hook_variants": {label: record}})

    _print_cost()
    json.dump({"variants": made, "failed": failures,
               "clips_saved": vplan["clips_saved"]}, sys.stdout, indent=2)
    print()
    if failures and not made:
        sys.exit(1)


def cmd_ad_voice(args):
    """Mux a voiceover (and optional music bed) onto a finished ad."""
    sys.path.insert(0, str(Path(__file__).parent))
    import admux as mx

    video = Path(args.video).expanduser().resolve()
    if not video.is_file():
        print(f"Error: video not found: {video}", file=sys.stderr)
        sys.exit(2)

    output = Path(args.output).expanduser() if args.output else \
        video.with_name(f"{video.stem}-vo{video.suffix}")

    try:
        if args.music:
            mx.mix_tracks(video, output,
                          voice=Path(args.voice).expanduser() if args.voice else None,
                          music=Path(args.music).expanduser(), duck=not args.no_duck)
        else:
            if not args.voice:
                print("Error: pass --voice, --music, or both", file=sys.stderr)
                sys.exit(2)
            mx.mux_voice(video, Path(args.voice).expanduser(), output,
                         normalize=not args.no_normalize,
                         keep_original=args.keep_original)
    except mx.MuxError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    print(f"\nSaved: {output}", file=sys.stderr)
    json.dump({"output": str(output), "duration": mx.duration_of(output)},
              sys.stdout, indent=2)
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Generate media assets: images, video clips, voiceovers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── image ──
    img = sub.add_parser("image", help="Generate images via Gemini")
    img.add_argument("prompt", help="Image generation prompt")
    img.add_argument("--model", choices=["flash", "pro"], default="flash")
    img.add_argument("--aspect", choices=VALID_ASPECT_RATIOS, default="1:1")
    img.add_argument("--size", choices=VALID_IMAGE_SIZES, default="2K")
    img.add_argument("--count", type=int, default=1, choices=range(1, 11), metavar="1-10")
    img.add_argument("--reference", nargs="+", help="Reference images (up to 14)")
    img.add_argument("--transparent", action="store_true", help="Transparent background via difference matting")
    img.add_argument("--output", help="Output directory")
    img.add_argument("--prefix", default="generated")
    img.add_argument("--vectorize", action="store_true", help="Trace to SVG (vtracer + svgo) — flat art only")
    img.set_defaults(func=cmd_image)

    # ── video ──
    vid = sub.add_parser("video", help="Generate video clips (Veo, Hailuo)")
    vid.add_argument("prompt", help="Video generation prompt")
    vid.add_argument("--model", choices=ALL_VIDEO_MODELS, default="fast",
                     help="fast/standard = Veo via Gemini; hailuo-* = HTTP backends")
    vid.add_argument("--provider", choices=VIDEO_PROVIDERS, default="auto",
                     help="Backend for --model; 'auto' picks the model's default")
    vid.add_argument("--duration", type=int, default=4,
                     help="Seconds; validated against the model's supported set")
    vid.add_argument("--aspect", default="16:9")
    vid.add_argument("--resolution", help="e.g. 2K, 768P (HTTP backends only)")
    vid.add_argument("--from-image", help="Image path for image-to-video (first frame)")
    vid.add_argument("--first-frame", help="Opening frame image — path or https URL")
    vid.add_argument("--last-frame", help="Closing frame image — path or https URL")
    vid.add_argument("--reference", nargs="+",
                     help="Style/identity reference images (cannot combine with frames)")
    vid.add_argument("--no-audio", dest="audio", action="store_false",
                     help="Skip the native audio track")
    vid.add_argument("--timeout", type=int, default=900,
                     help="Seconds to wait for an async job (default 900)")
    vid.add_argument("--yes", "-y", action="store_true",
                     help="Skip the spend confirmation")
    vid.add_argument("--output", help="Output directory")
    vid.add_argument("--prefix", default="generated")
    vid.set_defaults(func=cmd_video, audio=True)

    # ── caps ──
    caps = sub.add_parser("caps", help="Show a video model's capabilities (local + live)")
    caps.add_argument("--model", choices=ALL_VIDEO_MODELS, default="hailuo-3")
    caps.add_argument("--provider", choices=VIDEO_PROVIDERS, default="auto")
    caps.set_defaults(func=cmd_caps)

    # ── story ──
    story = sub.add_parser(
        "story", help="Storyboard-driven multi-shot video from a story spec"
    )
    story_sub = story.add_subparsers(dest="phase", required=True)

    def _story_common(parser_obj, *, video_flags=True):
        parser_obj.add_argument("spec", help="Path to the story JSON spec")
        parser_obj.add_argument("--workdir", help="Where panels/clips live "
                                                  "(default: <spec>-story/)")
        if video_flags:
            parser_obj.add_argument("--model", choices=HTTP_VIDEO_MODELS,
                                    help="Override the spec's model")
            parser_obj.add_argument("--provider", choices=VIDEO_PROVIDERS,
                                    help="Override the spec's provider")
            parser_obj.add_argument("--resolution",
                                    help="Override the spec's resolution (e.g. 768P to "
                                         "draft a 2K spec cheaply)")
            parser_obj.add_argument("--only", nargs="+", metavar="SHOT_ID",
                                    help="Limit to these shot ids (re-roll a failure)")
        return parser_obj

    plan_p = _story_common(story_sub.add_parser(
        "plan", help="Dry run: print every compiled prompt and the total cost"))
    plan_p.set_defaults(func=cmd_story_plan)

    board_p = _story_common(story_sub.add_parser(
        "board", help="Phase 1: generate the contact sheet and slice it into panels"),
        video_flags=False)
    board_p.add_argument("--yes", "-y", action="store_true",
                         help="Skip the spend confirmation")
    board_p.set_defaults(func=cmd_story_board)

    shots_p = _story_common(story_sub.add_parser(
        "shots", help="Phase 2: chain the panels into clips"))
    shots_p.add_argument("--timeout", type=int, default=900)
    shots_p.add_argument("--yes", "-y", action="store_true",
                         help="Skip the spend confirmation")
    shots_p.set_defaults(func=cmd_story_shots)

    regen_p = _story_common(story_sub.add_parser(
        "regenerate", help="Promote approved 768P drafts to 2K (MiniMax direct only)"))
    regen_p.add_argument("--timeout", type=int, default=900)
    regen_p.add_argument("--yes", "-y", action="store_true",
                         help="Skip the spend confirmation")
    regen_p.set_defaults(func=cmd_story_regenerate)

    assemble_p = _story_common(story_sub.add_parser(
        "assemble", help="Phase 4: concatenate the clips with ffmpeg"), video_flags=False)
    assemble_p.add_argument("--output", help="Output mp4 path")
    assemble_p.set_defaults(func=cmd_story_assemble)

    # ── ad ──
    advert = sub.add_parser(
        "ad", help="Social video ads: named formats, platform safe zones, burned text"
    )
    ad_sub = advert.add_subparsers(dest="phase", required=True)

    def _ad_common(parser_obj, *, video_flags=True):
        parser_obj.add_argument("spec", help="Path to the ad JSON spec")
        parser_obj.add_argument("--workdir", help="Where panels/clips live "
                                                  "(default: <spec>-ad/)")
        if video_flags:
            parser_obj.add_argument("--model", choices=HTTP_VIDEO_MODELS,
                                    help="Override the spec's model")
            parser_obj.add_argument("--provider", choices=VIDEO_PROVIDERS,
                                    help="Override the spec's provider")
            parser_obj.add_argument("--resolution",
                                    help="Override the spec's resolution (e.g. 768P)")
            parser_obj.add_argument("--only", nargs="+", metavar="BEAT_ID",
                                    help="Limit to these beat ids (re-roll a failure)")
        return parser_obj

    ad_plan_p = _ad_common(ad_sub.add_parser(
        "plan", help="Dry run: beat sheet, prompts, burned text and total cost"))
    ad_plan_p.set_defaults(func=cmd_ad_plan)

    ad_prev_p = _ad_common(ad_sub.add_parser(
        "preview", help="Render the safe-zone guide and caption stills — spends nothing"),
        video_flags=False)
    ad_prev_p.set_defaults(func=cmd_ad_preview)

    ad_board_p = _ad_common(ad_sub.add_parser(
        "board", help="Phase 1: one image call -> the panel grid"), video_flags=False)
    ad_board_p.add_argument("--yes", "-y", action="store_true",
                            help="Skip the spend confirmation")
    ad_board_p.set_defaults(func=cmd_ad_board)

    ad_shots_p = _ad_common(ad_sub.add_parser(
        "shots", help="Phase 2: chain the panels into clips"))
    ad_shots_p.add_argument("--timeout", type=int, default=900)
    ad_shots_p.add_argument("--yes", "-y", action="store_true",
                            help="Skip the spend confirmation")
    ad_shots_p.set_defaults(func=cmd_ad_shots)

    ad_hooks_p = _ad_common(ad_sub.add_parser(
        "hooks", help="Render N alternate opening takes; body clips are reused"))
    ad_hooks_p.add_argument("--count", type=int, default=3,
                            help="How many alternate takes (default 3)")
    ad_hooks_p.add_argument("--beat", help="Which beat varies (default: the first)")
    ad_hooks_p.add_argument("--timeout", type=int, default=900)
    ad_hooks_p.add_argument("--yes", "-y", action="store_true",
                            help="Skip the spend confirmation")
    ad_hooks_p.set_defaults(func=cmd_ad_hooks)

    ad_regen_p = _ad_common(ad_sub.add_parser(
        "regenerate", help="Promote approved 768P drafts to 2K (MiniMax direct only)"))
    ad_regen_p.add_argument("--timeout", type=int, default=900)
    ad_regen_p.add_argument("--yes", "-y", action="store_true",
                            help="Skip the spend confirmation")
    ad_regen_p.set_defaults(func=cmd_ad_regenerate)

    ad_asm_p = _ad_common(ad_sub.add_parser(
        "assemble", help="Concatenate the clips and burn the text"), video_flags=False)
    ad_asm_p.add_argument("--output", help="Output mp4 path")
    ad_asm_p.add_argument("--no-captions", action="store_true",
                          help="Leave the film clean; still writes the .srt")
    ad_asm_p.set_defaults(func=cmd_ad_assemble)

    ad_voice_p = ad_sub.add_parser(
        "voice", help="Mux a voiceover and/or a music bed onto a finished ad")
    ad_voice_p.add_argument("video", help="The finished mp4")
    ad_voice_p.add_argument("--voice", help="Voiceover audio file")
    ad_voice_p.add_argument("--music", help="Music bed audio file")
    ad_voice_p.add_argument("--output", help="Output mp4 path")
    ad_voice_p.add_argument("--keep-original", action="store_true",
                            help="Mix the clip audio under the voice instead of replacing it")
    ad_voice_p.add_argument("--no-normalize", action="store_true",
                            help="Skip loudness normalisation on the voice")
    ad_voice_p.add_argument("--no-duck", action="store_true",
                            help="Do not duck the music bed under speech")
    ad_voice_p.set_defaults(func=cmd_ad_voice)

    # ── voice ──
    vox = sub.add_parser("voice", help="Generate voiceovers via ElevenLabs")
    vox.add_argument("--text", help="Text to speak (inline)")
    vox.add_argument("--file", help="Read text from file")
    vox.add_argument("--voice", default="my-voice",
                     help="Preset (my-voice, arabic-male, arabic-female, english-male, english-female) or raw voice ID")
    vox.add_argument("--model", choices=["v3", "flash"], default="v3")
    vox.add_argument("--output", help="Output directory")
    vox.add_argument("--prefix", default="generated")
    vox.set_defaults(func=cmd_voice)

    # ── overlay ──
    ovl = sub.add_parser("overlay", help="Add text overlay to an image (proper Arabic support)")
    ovl.add_argument("image", help="Source image path")
    ovl.add_argument("--text", nargs="+", required=True, help="Text(s) to overlay")
    ovl.add_argument("--position", nargs="+", default=None,
                     help="Position per text: top, center, bottom, or x,y coords")
    ovl.add_argument("--color", default="#FFFFFF", help="Text color (hex)")
    ovl.add_argument("--size", type=int, default=60, help="Font size in pixels")
    ovl.add_argument("--stroke-color", default="#000000", help="Text outline color")
    ovl.add_argument("--stroke-width", type=int, default=3, help="Text outline width")
    ovl.add_argument("--bg-color", default=None, help="Background box color behind text (hex)")
    ovl.add_argument("--bg-padding", type=int, default=20, help="Padding around text background")
    ovl.add_argument("--output", help="Output directory")
    ovl.add_argument("--prefix", default="overlay")
    ovl.set_defaults(func=cmd_overlay)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
