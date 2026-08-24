---
name: media
description: "Generate images, video clips, voiceovers, and text overlays. Use when the user says /media, wants to generate an image, create a thumbnail, make a video clip, generate a voiceover, add text overlay to an image, or create visual/audio content. Also trigger on phrases like 'generate image', 'create thumbnail', 'make a video', 'voiceover', 'media assets', 'visual content', 'create an image of', or 'generate a picture', 'upload to deck', 'slide asset', 'vectorize', 'svg trace'."
---

# Media Generator (/media)

Generate images, video clips, voiceovers, and text overlays using AI. All output goes to `~/generated_media/` by default (override with `--output`).

## Script Location

```
~/.claude/skills/media/scripts/generate_media.py
```

All commands: `uv run ~/.claude/skills/media/scripts/generate_media.py <subcommand> [args]`

## CRITICAL: Arabic Text in Images

AI image generators **CANNOT render Arabic text correctly**. They produce garbled, disconnected characters.

**Solution:** Generate the image without text, then use the `overlay` command to add proper Arabic text:

```bash
# Step 1: Background image (no text)
uv run ~/.claude/skills/media/scripts/generate_media.py image "Dark dramatic scene, no text" --aspect 16:9 --prefix bg

# Step 2: Add Arabic text overlay
uv run ~/.claude/skills/media/scripts/generate_media.py overlay ~/generated_media/bg_*.png \
  --text "تحذير!" "احذر قبل أن تشتري" \
  --position top bottom --size 80 --stroke-width 4 --prefix final
```

**English text works fine** — include it directly in the image prompt.

## Image Generation (Gemini)

```bash
# Basic — fast and cheap
uv run ~/.claude/skills/media/scripts/generate_media.py image "Bold YouTube thumbnail about AI tools" --aspect 16:9

# High quality
uv run ~/.claude/skills/media/scripts/generate_media.py image "Detailed product mockup" --model pro --size 4K

# With reference images
uv run ~/.claude/skills/media/scripts/generate_media.py image "Product in this style" --reference brand.png logo.png

# Transparent background
uv run ~/.claude/skills/media/scripts/generate_media.py image "Logo on transparent" --transparent

# Multiple variations
uv run ~/.claude/skills/media/scripts/generate_media.py image "Quote card design" --count 3 --aspect 1:1
```

**Models:**
- `flash` (default) — Gemini 2.5 Flash. Fast, ~$0.04/image.
- `pro` — Gemini 3 Pro. Highest quality, ~$0.12/image. Supports 4K.

**Aspect ratios:** `1:1`, `2:3`, `3:2`, `16:9`, `9:16`, `21:9`

**Sizes:** `1K`, `2K` (default), `4K` (pro only)

## Scenario panels (field-note style)

Scenario-panel art is generated through the image command in the **field-note style** with
character reference sheets — the full pipeline (style scaffold, no-text guard, critique
checklist, provenance) lives in `cast/fieldnote.md`.


## Video Generation (Veo 3.1)

```bash
# Quick preview
uv run ~/.claude/skills/media/scripts/generate_media.py video "Aerial shot of Dubai at sunset" --model fast --duration 4

# Higher quality
uv run ~/.claude/skills/media/scripts/generate_media.py video "Professional office time-lapse" --model standard --duration 8

# Vertical for TikTok/Reels
uv run ~/.claude/skills/media/scripts/generate_media.py video "Eye-catching product reveal" --aspect 9:16

# Image-to-video
uv run ~/.claude/skills/media/scripts/generate_media.py video "Animate this scene" --from-image hero.png
```

**Models:**
- `fast` (default) — Veo 3.1 Fast. ~$0.15/sec ($0.60-1.20/clip).
- `standard` — Veo 3.1. ~$0.40/sec ($1.60-3.20/clip).

**Durations:** `4`, `6`, `8` seconds

## Voice Generation (ElevenLabs)

```bash
# Inline text
uv run ~/.claude/skills/media/scripts/generate_media.py voice --text "Welcome to our channel" --voice english-male

# From file
uv run ~/.claude/skills/media/scripts/generate_media.py voice --file script.txt --voice arabic-male

# Quick draft
uv run ~/.claude/skills/media/scripts/generate_media.py voice --text "Test narration" --model flash
```

**Models:**
- `v3` (default) — ElevenLabs v3. ~$0.30/1K chars.
- `flash` — ElevenLabs Flash v2.5. ~$0.15/1K chars.

**Voice presets:** `arabic-male`, `arabic-female`, `english-male`, `english-female` (or pass raw ElevenLabs voice ID)

### Pinned cast voices (comic scenes)

Recurring characters should have PINNED voices — chosen once by audition and stable across
every episode. `cast/voices.json` is that record; look up the `voice_id` and pass it raw:

```bash
VOICE=$(jq -r .cast.sara.voice_id ~/.claude/skills/media/cast/voices.json)
uv run ~/.claude/skills/media/scripts/generate_media.py \
  voice --text "Run the doctor first." --voice "$VOICE" --model v3 --output OUT
```

Cast: `sara` `noor` `khalid` `hamdan` (bubble dialogue) · `narrator` (scene-setting beats).

**Production clips** must be loudness-normalized so several characters share one scene volume:

```bash
ffmpeg -i in.mp3 -af loudnorm=I=-16:TP=-1.5:LRA=11 -ar 44100 -ac 1 -b:a 128k out.mp3
```

Naming convention: `d<day>-s<slide#>-b<bubble#>-<speaker>.mp3`.


## Text Overlay (Pillow + arabic-reshaper)

Add proper Arabic or English text to any image with correct letter connections and RTL.

```bash
# Basic overlay
uv run ~/.claude/skills/media/scripts/generate_media.py overlay image.png \
  --text "تحذير!" --position top --size 80

# Multiple text lines
uv run ~/.claude/skills/media/scripts/generate_media.py overlay image.png \
  --text "العنوان" "النص الثانوي" --position top bottom --size 90 --stroke-width 4

# With background box
uv run ~/.claude/skills/media/scripts/generate_media.py overlay image.png \
  --text "احذر قبل أن تشتري" --position center --bg-color "#CC000000" --bg-padding 25
```

**Options:** `--text`, `--position` (top/center/bottom/x,y), `--color`, `--size`, `--stroke-color`, `--stroke-width`, `--bg-color`, `--bg-padding`

## Prompt Templates

See `~/.claude/skills/media/prompt-templates.md` for 20+ ready-to-use templates covering thumbnails, quote cards, carousels, video hooks, b-roll, ads, LinkedIn banners, and Instagram stories.

## Cost Estimates

| Asset | Model | Cost |
|-------|-------|------|
| Image (flash) | Gemini 2.5 Flash | ~$0.04 |
| Image (pro) | Gemini 3 Pro | ~$0.12 |
| Video 4s (fast) | Veo 3.1 Fast | ~$0.60 |
| Video 8s (fast) | Veo 3.1 Fast | ~$1.20 |
| Video 4s (standard) | Veo 3.1 | ~$1.60 |
| Video 8s (standard) | Veo 3.1 | ~$3.20 |
| Voice 500 chars (v3) | ElevenLabs v3 | ~$0.15 |
| Voice 500 chars (flash) | ElevenLabs Flash | ~$0.08 |

## Panel rendering (Blender)

Render comic-panel stills from a declarative JSON scene spec via headless Blender — authored
scenes become panels, words never get baked into pixels (speech bubbles are DOM overlays, added
later, so copy edits touch no image).

```bash
blender -b --python ~/.claude/skills/media/scripts/render_scene.py -- scene.json --quality draft
blender -b --python ~/.claude/skills/media/scripts/render_scene.py -- scene.json --quality final --only wide
```

`--quality draft|final` (default `final`) trades render time for samples/resolution; `--only <camera>`
renders a single named camera. Paths inside the spec (blend files, `render.outdir`) resolve relative
to the spec file's own directory, not the working directory. `blend` paths may point anywhere on
disk, including outside the repo — this is a trusted local tool, and the injection surface is the
spec author's own machine. `render.outdir` is different: it's checked to stay under the spec file's
own directory (exit 2 otherwise), since a spec shouldn't be able to write files elsewhere on disk.
See `~/.claude/skills/media/scripts/render_scene.py`'s module docstring for the full schema; trimmed:

```jsonc
{
  "version": 1,
  "units": { "character_height": 1.75 },
  "materials": { "wall": { "rgb": [0.88, 0.86, 0.82], "rough": 0.8 } },
  "boxes": [ { "name": "floor", "size": [14,14,0.1], "loc": [0,0,-0.05], "material": "wall" } ],
  "cast": [
    { "name": "student", "blend": "assets/animatedmen/Blends/Male_Casual.blend",
      "action": "Man_Sitting", "frame": 11, "loc": [1.1,-1.30,0.02], "rot_z_deg": 184 }
  ],
  "cameras": [ { "name": "wide", "loc": [-3.6,-3.6,1.8], "target": [0.5,0.5,1.05], "lens": 30 } ],
  "render": { "resolution": [1600, 900], "samples": { "draft": 24, "final": 128 }, "outdir": "shots" }
}
```

Unknown top-level keys, wrong field shapes, and dangling references (undefined material, unknown
`target_bone.cast`) are hard errors, exit 2, naming the offending path — checked in full before any
scene gets built. Every render (draft or final) writes `<outdir>/manifest.json` — sha256 of the spec
and every input blend it referenced, plus a `frames` map of output filename, pixel size, and quality
per camera — so a panel is always traceable back to the exact spec + assets that produced it. A full
run rewrites the whole manifest; a `--only <camera>` run merges into whatever manifest is already
there, updating just that camera's frame entry so a mixed draft/final panel set stays honest. The
manifest is byte-stable across re-runs; pixel identity of the PNGs is not (Cycles sampling/denoise
isn't bit-deterministic run to run).

See `~/.claude/skills/media/examples/classroom-spec.json` for a complete working example. Its asset
paths are relative to that file and only resolve on the machine that has the referenced CC0 character/
furniture packs on disk — point `blend` paths at your own assets elsewhere.

Requires: Blender (no pip deps — stdlib + bpy only, no network).

## Vectorize (`--vectorize`) — flat art only

Traces a raster to SVG with `vtracer`, then optimizes it with `svgo`. **Flat art only —
silhouettes, icons, tokens, solid-color shapes.** Never run this on shaded illustration,
photos, or gradient renders: they trace into megabyte blob-SVGs that look worse than the
raster they came from. The flag itself doesn't refuse anything — tracing a photo is allowed,
this is the rule to follow, not an enforced check.

If `vtracer` or `svgo` is missing, vectorize degrades gracefully: no `vtracer` skips
vectorizing entirely (warns on stderr, the raster is kept as-is); no `svgo` (and no `npx` to
fall back to) keeps the unoptimized SVG (warns on stderr). Install:

```bash
cargo install vtracer   # or download a release binary
npm i -g svgo
```

## Required API Keys

Set these in `~/.zshrc` (or `~/.env`):
- `GEMINI_API_KEY` — For Gemini image + Veo video (aistudio.google.com)
- `ELEVENLABS_API_KEY` — For voiceover TTS (elevenlabs.io)

## Output

All files go to `~/generated_media/` with metadata sidecars:
- Images: `{prefix}_{timestamp}.png` + `.meta.json`
- Videos: `{prefix}_{timestamp}.mp4` + `.meta.json`
- Voice: `{prefix}_{timestamp}.mp3` + `.meta.json`
