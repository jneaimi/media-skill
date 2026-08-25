<div align="center">

<img src="docs/banner.jpg" alt="media-skill — images, video, voice and multi-shot films from one Claude Code skill" width="100%">

<p>
  <img src="https://img.shields.io/badge/license-MIT-B0604A?style=flat-square" alt="MIT license">
  <img src="https://img.shields.io/badge/Claude%20Code-skill-8A7A5C?style=flat-square" alt="Claude Code skill">
  <img src="https://img.shields.io/badge/python-3.11+-6E7B5B?style=flat-square" alt="Python 3.11+">
  <img src="https://img.shields.io/github/last-commit/jneaimi/media-skill?style=flat-square&color=A08768" alt="Last commit">
</p>

<p>
  <a href="#quick-start">Quick start</a> ·
  <a href="#story-mode">Story mode</a> ·
  <a href="#video">Video</a> ·
  <a href="#arabic-text">Arabic text</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="#cost">Cost</a>
</p>

</div>

A [Claude Code](https://claude.com/claude-code) skill that generates images, video clips and
voiceovers — and chains them into continuous multi-shot films from a single JSON shot list.

Say `/media` in Claude Code, or ask for a thumbnail, a voiceover, a product clip. The skill
picks the command, the model and the cost tier.

<div align="center">
  <img src="docs/demo.gif" alt="Eight seconds of a generated film, spanning a cut between two clips" width="90%">
  <br>
  <sub><b>Eight seconds of a generated film</b> — one JSON spec, two commands, no editing.<br>
  The cut in the middle is invisible because both clips were pinned to the same approved frame.</sub>
</div>

---

## Quick start

**Prerequisites:** [`uv`](https://docs.astral.sh/uv/) and a Gemini API key. Everything else is
optional and only needed for the backend you actually call.

```bash
git clone https://github.com/jneaimi/media-skill.git ~/.claude/skills/media
export GEMINI_API_KEY=...   # aistudio.google.com
```

Your first image, about ten seconds and four cents:

```bash
uv run ~/.claude/skills/media/scripts/generate_media.py \
  image "A lighthouse at dusk, long exposure, muted palette" --aspect 16:9
```

Every example below uses `$S` for that path. Set it once and the rest are copy-pasteable:

```bash
S=~/.claude/skills/media/scripts/generate_media.py
```

Scripts declare their own dependencies inline, so `uv` installs them on first run. There is no
virtualenv to create and nothing to `pip install`.

---

## Story mode

The consistency problem in AI video is drift: generate nine clips and you get nine subtly
different characters. This solves it in two moves.

**One image, every shot.** All panels are drawn as cells of a *single* grid image, so they
share one palette, one lighting setup and one rendering of each character by construction —
not by asking nine separate generations to match. One image call, not nine.

**Both ends pinned.** Clip *i* is generated with panel *i* as its first frame and panel *i+1*
as its last, so every cut lands on art you already approved.

<div align="center">
  <img src="docs/chain.jpg" alt="Four panels with three clips bridging each adjacent pair" width="100%">
</div>

Four phases, so you approve between them:

```bash
uv run $S story plan     spec.json   # every compiled prompt + total cost — spends nothing
uv run $S story board    spec.json   # one image call → grid → sliced panels
uv run $S story shots    spec.json   # chain the panels into clips
uv run $S story assemble spec.json   # ffmpeg concat → final film
```

**The cheap loop** (MiniMax direct only): draft every clip at 768P, keep what works, then
re-render only the keepers at 2K. Regeneration reuses the original result rather than
generating afresh, so the take you approved is the take you get — H3 has no seed, so a
plain re-submit at 2K would hand you a *different performance*.

```bash
uv run $S story shots      spec.json --provider minimax --resolution 768P   # $0.48/6s
uv run $S story regenerate spec.json                                        # $0.30/6s
```

Drafts are moved to `clips/drafts/` rather than overwritten. A keeper costs the same as
going straight to 2K ($0.78 for 6s); a **reject costs $0.48 instead of $0.78**.

A `manifest.json` records a sha256 of the spec, the board, every panel and every clip as it
goes, so a failure on clip 6 never loses clips 1–5. Re-roll a single shot with
`story shots spec.json --only <id>`.

The spec is plain JSON — a `style` for the whole film, a `cast` of reference images, and a
list of shots each carrying what to draw, what happens, one camera move and its sound layers:

```jsonc
{
  "version": 1, "model": "hailuo-3", "aspect": "16:9", "duration": 6,
  "style": "field-note illustration, loose ink over watercolour, muted earth palette",
  "cast":  { "sara": "cast/refs/ref-sara.jpg" },
  "shots": [
    { "id": "desk",
      "panel":  "wide shot of an open-plan office. Sara sits at a cluttered desk…",
      "action": "Sara stops typing and lifts her gaze",
      "camera": "slow push in",
      "sound":  { "ambience": "low office hum, a distant printer", "music": "N/A" } }
  ]
}
```

[`examples/story-spec.json`](examples/story-spec.json) is a complete working spec — the film
above came from it. [`references/hailuo-prompting.md`](references/hailuo-prompting.md)
documents the prompt discipline the compiler encodes.

---

## Video

`--model` picks the model, `--provider` picks who serves it. Veo runs through the Gemini SDK;
Hailuo 3 runs over HTTP against **either** OpenRouter or MiniMax direct.

| `--model` | Backend | Resolution | Durations | Frames | Audio | Price |
|---|---|---|---|---|---|---|
| `fast` *(default)* | Gemini SDK · Veo 3.1 Lite | 720p / 1080p | 4, 6, 8 | first + last | ✅ | ~$0.15/s |
| `standard` | Gemini SDK · Veo 3.1 | 720p / 1080p / 4K | 4, 6, 8 | first + last | ✅ | ~$0.40/s |
| `hailuo-3` | OpenRouter · MiniMax | 2K *(768P direct)* | 5–15 *(4–15 direct)* | first + last | ✅ | $0.13/s *(768P $0.08/s)* |
| `hailuo-2.3` | OpenRouter | 1080p | 6, 10 | first only | ❌ | $0.0817/s |

```bash
uv run $S video "Aerial shot of a harbour at sunrise" --model fast --duration 6
uv run $S video "..." --model hailuo-3 --duration 6 --aspect 21:9
uv run $S video "..." --model hailuo-3 --provider minimax --resolution 768P --duration 4
uv run $S video "She opens the umbrella as the camera pulls back" \
  --model hailuo-3 --first-frame a.png --last-frame b.png
```

Parameters are checked against each backend's real capability set *before* anything is
submitted, so an unsupported duration fails instantly and free instead of as a paid 400.
Run `uv run $S caps --model hailuo-3` to see the live capability record.

> [!IMPORTANT]
> **Frames and references are different modes and cannot be combined.** That is a model-level
> rule, not a provider quirk — MiniMax rejects it and OpenRouter silently drops your
> references. The CLI refuses the combination instead. To get identity *and* frame control,
> bake identity into the frame images at the image stage, which is exactly what story mode does.

---

## Arabic text

AI image generators cannot render Arabic — they produce garbled, disconnected glyphs. The fix
is built in: generate the image *without* text, then composite real Arabic type on top with
`overlay`, which runs it through `arabic-reshaper` + `python-bidi` first.

```bash
# 1. background, no text
uv run $S image "Dark dramatic scene, no text" --aspect 16:9 --prefix bg

# 2. real Arabic on top — connected letters, correct RTL
uv run $S overlay ~/generated_media/bg_*.png \
  --text "تحذير!" "احذر قبل أن تشتري" \
  --position top bottom --size 80 --stroke-width 4
```

English text in prompts works fine and needs none of this.

---

## Configuration

Set only the keys for backends you call. Each is checked where it is used and names itself in
the error.

| Variable | Needed for | Get one |
|---|---|---|
| `GEMINI_API_KEY` | images, Veo video, story boards | [aistudio.google.com](https://aistudio.google.com) |
| `ELEVENLABS_API_KEY` | voiceover | [elevenlabs.io](https://elevenlabs.io) |
| `OPENROUTER_API_KEY` | `--model hailuo-*` | [openrouter.ai/keys](https://openrouter.ai/keys) |
| `MINIMAX_API_KEY` | `--provider minimax`, 768P tier | [platform.minimax.io](https://platform.minimax.io) |

Optional tools: `ffmpeg` (assembling films, normalising voice clips), `blender` (panel
rendering), `vtracer` + `svgo` (`--vectorize` for flat art).

<details>
<summary><b>Voice presets</b></summary>

<br>

Presets default to stock ElevenLabs voices; no voice ID is hardcoded to a person. Point any of
them at your own clone:

```bash
export ELEVENLABS_VOICE_MY_VOICE=<your voice id>
# also _ARABIC_MALE, _ARABIC_FEMALE, _ENGLISH_MALE, _ENGLISH_FEMALE
```

</details>

<details>
<summary><b>Everything else it does</b></summary>

<br>

| Command | Backend | Notes |
|---|---|---|
| `image` | Gemini Flash Image / 3 Pro | aspect ratios, 4K, up to 14 reference images, transparent background via difference matting, batch variations |
| `voice` | ElevenLabs v3 / Flash v2.5 | preset or raw voice IDs, EN + AR |
| `overlay` | Pillow + arabic-reshaper | correct Arabic text on images |
| `caps` | OpenRouter | live model capabilities |
| `render_scene.py` | headless Blender | declarative JSON scene spec → comic panels, with a sha256 provenance manifest |

`cast/` holds a reusable visual world for comic-style panels: named characters with pinned
voices, CC0 set and pose vocabularies, and a licence record for every asset pack. Character
models are not committed — `fetch_assets.sh` pulls the CC0 packs from Quaternius.

All output lands in `~/generated_media/` (override with `--output`), each file paired with a
`.meta.json` sidecar recording prompt, model, references and timestamp.

</details>

---

## Cost

| | |
|---|---|
| Image — `flash` / `pro` | ~$0.04 / ~$0.12 |
| Video — Veo `fast` / `standard` | ~$0.15/s / ~$0.40/s |
| Video — Hailuo 3 at 2K / 768P | $0.13/s / $0.08/s |
| Video — Hailuo 3 768P→2K regeneration *(direct only)* | $0.05/s |
| Voice — `v3` / `flash` | ~$0.30 / ~$0.15 per 1K chars |
| **The film at the top** — 1 board + 3 clips at 2K | **~$2.46** |

> [!WARNING]
> Video costs 10–50× an image. Every paid call prints its estimate and waits for confirmation;
> `--yes` skips the prompt and is required in non-interactive runs. Run `story plan` first — it
> prices a whole film without spending anything.

Hailuo keepers cost the same on either provider; **rejects are what differ.** Drafting at 768P
on MiniMax direct and paying 2K only for shots you keep is the cheaper loop when you expect to
re-roll — which, on a chain, you will.

---

## License

MIT — see [LICENSE](LICENSE). Third-party assets referenced by the cast bible carry their own
licences, documented in [`cast/licenses.md`](cast/licenses.md).
