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


## Video Generation

Two backends behind one command. `--model` picks the model; `--provider` picks who serves
it (`auto` uses the model's default). Adding a model does not change any existing call.

| `--model` | Backend | Res | Durations | Frames | Audio | Price |
|---|---|---|---|---|---|---|
| `fast` (default) | Gemini SDK (Veo 3.1 Lite) | 720p/1080p | 4, 6, 8 | first + last | yes | ~$0.15/s |
| `standard` | Gemini SDK (Veo 3.1) | 720p/1080p/4K | 4, 6, 8 | first + last | yes | ~$0.40/s |
| `hailuo-3` | OpenRouter *or* MiniMax direct | 2K (768P direct) | 5–15 (4–15 direct) | first + last | yes | $0.13/s (768P $0.08/s) |
| `hailuo-2.3` | OpenRouter | 1080p | 6, 10 | **first only** | no | $0.0817/s |

```bash
# Unchanged — the original Veo path
uv run ~/.claude/skills/media/scripts/generate_media.py video "Aerial shot of Dubai at sunset" --model fast --duration 4

# Hailuo 3 via OpenRouter (needs OPENROUTER_API_KEY)
uv run ~/.claude/skills/media/scripts/generate_media.py video "A lighthouse beam sweeps the harbour" \
  --model hailuo-3 --duration 6 --aspect 21:9

# Hailuo 3 direct — unlocks the cheaper 768P draft tier (needs MINIMAX_API_KEY)
uv run ~/.claude/skills/media/scripts/generate_media.py video "..." \
  --model hailuo-3 --provider minimax --resolution 768P --duration 4

# First-and-last-frame: the clip starts here and lands there
uv run ~/.claude/skills/media/scripts/generate_media.py video "She opens the umbrella as the camera pulls back" \
  --model hailuo-3 --first-frame a.png --last-frame b.png --duration 6

# Style/identity references (a DIFFERENT mode — cannot combine with frames)
uv run ~/.claude/skills/media/scripts/generate_media.py video "..." --model hailuo-3 --reference sara.png
```

**Every paid call is gated.** The cost is printed and confirmed before submission; pass
`--yes` to skip the prompt (required in non-interactive runs). Generation is async and
slow — a 6s 2K Hailuo clip takes ~350s; `--timeout` defaults to 900s.

**Frames vs references are mutually exclusive modes** — a model rule, not a provider quirk.
The CLI refuses the combination rather than letting OpenRouter silently drop your
references. Put identity into the frame images instead (generate them with `image
--reference`). See `references/hailuo-prompting.md`.

Check live capabilities any time: `generate_media.py caps --model hailuo-3`.

## Story mode — storyboard-driven multi-shot video

One image call draws every shot as a cell in a single grid, so all panels share one palette,
one lighting setup and one rendering of each character. Those panels are then chained
through the video model: clip *i* runs from panel *i* to panel *i+1*, pinning both ends of
every cut to art you approved. N panels → N-1 clips → one continuous film.

```bash
S=~/.claude/skills/media/scripts/generate_media.py
uv run $S story plan     spec.json          # dry run: every prompt + total cost, no spend
uv run $S story board    spec.json          # 1 image call → contact sheet → sliced panels
uv run $S story shots    spec.json          # chain the panels into clips (cost-gated)
uv run $S story shots    spec.json --only desk turn   # re-roll just these shots
uv run $S story regenerate spec.json        # 768P drafts → 2K, same take (MiniMax only)
uv run $S story assemble spec.json          # ffmpeg concat → final mp4
```

`regenerate` re-renders an approved 768P draft at 2K by reusing the original result
(`$0.05/s`), not by re-prompting. H3 has no seed, so re-submitting would return a different
take. Drafts move to `clips/drafts/`. Direct-only; OpenRouter does not expose it.

`assemble` stream-copies when clips share a frame size and re-encodes when they don't —
regeneration can return 2592x1440 and 2560x1440 in the same batch, and `-c copy` would
write one size into the header and let a segment disagree with it.

Phases are separate so you approve between them, and `manifest.json` records a sha256 of
the spec, the sheet, every panel and every clip as it goes — a crash on clip 6 never loses
clips 1–5. See `examples/story-spec.json` for a complete spec.

Spec shape: `style` (one look for the whole film), `cast` (name → reference image),
`board` (`cols`/`rows`/`image_model`/`size`/`inset`/`autotrim`), and `shots[]`, each with
`id`, `panel` (what gets drawn), `action`, `camera`, optional `sound`
(`ambience`/`dialogue`/`music`) and `duration`.

`chain` is `bridge` (default — both ends pinned; the last shot is the closing frame and
generates no clip of its own) or `anchor` (only the opening frame pinned, N panels → N
clips, for shots that end somewhere you can't draw in advance).

Panels are sliced with a fixed `inset` to absorb gutter wobble, then `autotrim` removes any
frame the model drew inside the cell — image models add keylines and paper margins however
firmly the prompt forbids them, and a drawn border becomes a bar baked into every frame of
the clip.

## Ad mode — social video ads

An ad spec **compiles down into a story spec**, so the whole board → shots → assemble
chain above runs unchanged. `ad` adds what a story does not have: a named format with a
beat skeleton, a platform safe zone, a product that must stay on-model, and burned-in
text (correct Arabic included).

```bash
uv run ~/.claude/skills/media/scripts/generate_media.py ad plan     spec.json  # beats, prompts, cues, cost — spends nothing
uv run ~/.claude/skills/media/scripts/generate_media.py ad preview  spec.json  # safe-zone guide + caption stills — spends nothing
uv run ~/.claude/skills/media/scripts/generate_media.py ad board    spec.json  # one image call → the panel grid
uv run ~/.claude/skills/media/scripts/generate_media.py ad shots    spec.json  # chain the panels into clips
uv run ~/.claude/skills/media/scripts/generate_media.py ad assemble spec.json  # concat + burn the text
```

**Formats** (`"format"`), each supplying an ordered beat skeleton and a duration split:

| actor-led | faceless |
|---|---|
| `problem-solution` · `testimonial` · `unboxing` · `before-after` · `tutorial` | `hero-product` · `asmr` · `kinetic-text` · `explainer` · `hands-demo` |

`custom` skips the role check and takes any beat ids.

**Platforms** (`"platform"`) set the aspect ratio *and* the safe zone: `tiktok`, `reels`,
`shorts`, `feed`, `youtube`. Text is laid out inside the safe box, never the frame —
TikTok covers the top 10%, right 10% and bottom 20% with its own UI, and copy under that
is copy nobody reads. `ad preview` renders the guide so you can check a layout before
paying for a single clip.

**Product fidelity.** Video models cannot hold a product on-model from text — ask for
"a woman holding a bottle of X" and the label is invented. So `product.refs` are carried
into the board call as reference images, and the product instruction is threaded through
both the board prompt and every shot prompt. The panel is drawn with the real packaging,
and the clip animates those pixels.

**Hook variants** — the performance loop. The hook is beat 1, which is clip 1, so the
body clips are rendered once and reused:

```bash
uv run ... ad hooks spec.json --count 6        # 6 takes of the opening beat
```

Six variants of a five-beat ad render **10 clips, not 30**. H3 has no seed, so
re-submitting the same hook prompt returns a genuinely different performance — which is
what a hook test wants. `--beat <id>` varies a different beat.

**Voiceover** — mux a narration and an optional music bed onto a finished ad. The bed is
ducked under speech with a sidechain compressor, and the voice is loudness-normalised to
the same `I=-16:TP=-1.5:LRA=11` the `voice` command uses:

```bash
uv run ... ad voice ad.mp4 --voice vo.mp3 --music bed.mp3
```

**Captions and Arabic.** Text is never drawn by the image model — the compiled story
always sets `allow_text: false`. Captions are rendered with Pillow and burned in with one
ffmpeg pass, which is what makes correct Arabic possible: each line is wrapped first, then
reshaped and bidi-reordered, so ligatures are never cut. A `.srt` is written alongside in
**logical** order, because an SRT consumer does its own shaping.

**Disclosure.** `"disclosure": true` burns an `AI-generated` mark at the top of the safe
zone for the whole film (or pass your own string). EU AI Act Article 50 has required
marking of synthetic media since 2026-08-02.

`examples/ad-spec.json` is a complete working 30s spec.

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
| Video 6s (hailuo-3, 2K) | MiniMax H3 | ~$0.78 |
| Video 6s (hailuo-3, 768P direct) | MiniMax H3 | ~$0.48 |
| Video 4s (hailuo-3, 768P direct) | MiniMax H3 | ~$0.32 |
| Voice 500 chars (v3) | ElevenLabs v3 | ~$0.15 |
| Voice 500 chars (flash) | ElevenLabs Flash | ~$0.08 |
| Story: 4-panel board + 3 clips (2K) | Gemini + H3 | ~$2.46 |

Hailuo keepers cost the same on either provider; **rejects are what differ**. Drafting at
768P direct and only paying 2K for shots you keep is the cheaper loop when you expect to
re-roll — which on a chain, you will.

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
- `GEMINI_API_KEY` — Gemini images + Veo video, and the `story` board (aistudio.google.com)
- `ELEVENLABS_API_KEY` — Voiceover TTS (elevenlabs.io)
- `OPENROUTER_API_KEY` — `--model hailuo-*` on `--provider openrouter` (openrouter.ai/keys)
- `MINIMAX_API_KEY` — `--provider minimax`, incl. the 768P tier (platform.minimax.io)

Only the keys for backends you actually call are needed; each is checked at the point of use
and names itself in the error.

Voice presets default to stock ElevenLabs voices. Point one at your own clone with
`ELEVENLABS_VOICE_MY_VOICE` (also `_ARABIC_MALE`, `_ARABIC_FEMALE`, `_ENGLISH_MALE`,
`_ENGLISH_FEMALE`).

## Output

All files go to `~/generated_media/` with metadata sidecars:
- Images: `{prefix}_{timestamp}.png` + `.meta.json`
- Videos: `{prefix}_{timestamp}.mp4` + `.meta.json`
- Voice: `{prefix}_{timestamp}.mp3` + `.meta.json`
