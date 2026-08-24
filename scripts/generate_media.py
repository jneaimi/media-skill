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

VOICE_MODELS = {
    "v3": "eleven_v3",
    "flash": "eleven_flash_v2_5",
}

VALID_ASPECT_RATIOS = ["1:1", "2:3", "3:2", "16:9", "9:16", "21:9"]
VALID_IMAGE_SIZES = ["1K", "2K", "4K"]
VALID_VIDEO_DURATIONS = [4, 6, 8]

# Voice presets — overridable via env vars
VOICE_PRESETS = {
    "my-voice": os.getenv("ELEVENLABS_VOICE_MY_VOICE", "xd6ta1jjl6XtFRVgqndO"),             # Custom voice clone (default)
    "arabic-male": os.getenv("ELEVENLABS_VOICE_ARABIC_MALE", "xd6ta1jjl6XtFRVgqndO"),       # Custom voice clone
    "arabic-female": os.getenv("ELEVENLABS_VOICE_ARABIC_FEMALE", "21m00Tcm4TlvDq8ikWAM"),   # Rachel
    "english-male": os.getenv("ELEVENLABS_VOICE_ENGLISH_MALE", "xd6ta1jjl6XtFRVgqndO"),     # Custom voice clone
    "english-female": os.getenv("ELEVENLABS_VOICE_ENGLISH_FEMALE", "21m00Tcm4TlvDq8ikWAM"), # Rachel
}


def _track_cost(cost_key: str, multiplier: float = 1.0):
    global _api_calls, _estimated_cost
    _api_calls += 1
    _estimated_cost += _COST_MAP.get(cost_key, 0) * multiplier


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

# Font search paths (macOS + Linux)
_ARABIC_FONT_PATHS = [
    # User-installed Noto Sans Arabic (variable weight)
    os.path.expanduser("~/Library/Fonts/NotoSansArabic[wdth,wght].ttf"),
    os.path.expanduser("~/Library/Fonts/NotoSansArabic-Bold.ttf"),
    # macOS system
    "/System/Library/Fonts/GeezaPro.ttc",
    "/System/Library/Fonts/Supplemental/Muna.ttc",
    "/System/Library/Fonts/Supplemental/Damascus.ttc",
    # Linux
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
    "/usr/share/fonts/noto/NotoSansArabic-Bold.ttf",
]

_LATIN_FONT_PATHS = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]


def _find_font(paths: list[str]) -> str | None:
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def _has_arabic(text: str) -> bool:
    return bool(__import__("re").search(r"[\u0600-\u06FF]", text))


def _reshape_arabic(text: str) -> str:
    """Reshape Arabic text for correct rendering: connected letters + RTL."""
    import arabic_reshaper
    from bidi.algorithm import get_display
    reshaped = arabic_reshaper.reshape(text)
    return get_display(reshaped)


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
    from google.genai import types

    client = _get_google_client()
    model_name = VIDEO_MODELS[args.model]
    output_dir = Path(args.output) if args.output else OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    # Build request
    generate_config = {
        "aspect_ratio": args.aspect,
        "number_of_videos": 1,
    }

    # Image-to-video
    image_ref = None
    if args.from_image:
        from PIL import Image
        image_ref = Image.open(args.from_image)
        print(f"Using reference image: {args.from_image}", file=sys.stderr)

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
            "duration": args.duration,
            "aspect_ratio": args.aspect,
            "from_image": args.from_image,
            "timestamp": datetime.now().isoformat(),
        }
        _save_metadata(filepath, metadata)
        results.append({"file": str(filepath), "metadata": metadata})

    _print_cost()
    json.dump({"generated": results}, sys.stdout, indent=2)
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


# ─── CLI ─────────────────────────────────────────────────────

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
    vid = sub.add_parser("video", help="Generate video clips via Veo 3.1")
    vid.add_argument("prompt", help="Video generation prompt")
    vid.add_argument("--model", choices=["standard", "fast"], default="fast")
    vid.add_argument("--duration", type=int, choices=VALID_VIDEO_DURATIONS, default=4)
    vid.add_argument("--aspect", choices=["16:9", "9:16"], default="16:9")
    vid.add_argument("--from-image", help="Image path for image-to-video")
    vid.add_argument("--output", help="Output directory")
    vid.add_argument("--prefix", default="generated")
    vid.set_defaults(func=cmd_video)

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
