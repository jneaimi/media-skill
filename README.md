# media-skill

A [Claude Code](https://claude.com/claude-code) skill for generating images, video clips,
voiceovers, and text overlays — plus a storyboard-driven pipeline that turns a JSON shot
list into a continuous multi-shot film, and a headless Blender renderer for comic panels.

Say `/media` in Claude Code (or just ask for an image, a thumbnail, a voiceover), and the
skill picks the right command, model, and cost tier.

## What it does

| Command | Backend | Notes |
|---|---|---|
| `image` | Gemini (Flash Image / 3 Pro) | aspect ratios, 4K, reference images, transparent background, batch variations |
| `video` | Veo 3.1 · MiniMax Hailuo 3 | one command, three backends — see below |
| `story` | Gemini + Hailuo + ffmpeg | storyboard → chained clips → one film |
| `voice` | ElevenLabs (v3 / Flash v2.5) | preset or raw voice IDs, EN + AR |
| `overlay` | Pillow + arabic-reshaper | **correct Arabic text on images** — connected letters, proper RTL |
| `caps` | OpenRouter | live model capabilities, so you never guess a parameter |
| `render_scene.py` | headless Blender | JSON scene spec → comic panels, with a sha256 provenance manifest |

### Video: three backends, one command

`--model` picks the model, `--provider` picks who serves it. Veo runs through the Gemini
SDK; Hailuo 3 runs over HTTP against **either** OpenRouter or MiniMax direct — the same
model, and direct adds a cheaper 768P draft tier and a 4-second floor.

```bash
video "..." --model fast                                  # Veo 3.1 Lite (default)
video "..." --model hailuo-3 --duration 6 --aspect 21:9   # H3 2K via OpenRouter
video "..." --model hailuo-3 --provider minimax --resolution 768P --duration 4
video "..." --model hailuo-3 --first-frame a.png --last-frame b.png
```

Parameters are validated against each backend's real capability set before anything is
submitted, and every paid call prints its cost and waits for a yes.

### Story mode

The consistency problem in AI video is drift: generate nine clips and you get nine slightly
different characters. `story` solves it in two moves.

**One image, every shot.** All panels are drawn as cells of a single grid image, so they
share one palette, one lighting setup and one rendering of each character by construction —
not by asking nine separate generations to match. One image call, not nine.

**Both ends pinned.** Clip *i* is generated with panel *i* as its first frame and panel
*i+1* as its last, so every cut lands on art you already approved and drift cannot
accumulate down the chain.

```bash
generate_media.py story plan     spec.json   # every compiled prompt + total cost, no spend
generate_media.py story board    spec.json   # contact sheet → sliced panels
generate_media.py story shots    spec.json   # chain panels into clips
generate_media.py story assemble spec.json   # ffmpeg concat → final mp4
```

Four phases so you can approve between them, with a sha256 manifest written as it goes.
`examples/story-spec.json` is a working spec; `references/hailuo-prompting.md` explains the
prompt discipline the compiler encodes.

### The Arabic problem this solves

AI image generators cannot render Arabic text — they produce garbled, disconnected glyphs.
The fix baked into this skill: generate the image *without* text, then composite real Arabic
type on top with `overlay`, which runs the text through `arabic-reshaper` + `python-bidi`
first. English text in prompts works fine and needs none of this.

## Install

Drop it into your Claude Code skills directory:

```bash
git clone https://github.com/jneaimi/media-skill.git ~/.claude/skills/media
```

Then set two keys in your environment (`~/.zshrc`, `~/.env`, or a secrets manager):

```bash
export GEMINI_API_KEY=...      # aistudio.google.com — images, Veo video, story boards
export ELEVENLABS_API_KEY=...  # elevenlabs.io — voiceover
export OPENROUTER_API_KEY=...  # openrouter.ai/keys — optional, for --model hailuo-*
export MINIMAX_API_KEY=...     # platform.minimax.io — optional, adds the 768P tier
```

Only the keys for backends you call are needed; each is checked where it's used and names
itself in the error.

Scripts run under [`uv`](https://docs.astral.sh/uv/) with inline dependency metadata — no
virtualenv to manage:

```bash
uv run ~/.claude/skills/media/scripts/generate_media.py image "a lighthouse at dusk" --aspect 16:9
```

Optional extras: `blender` (panel rendering), `vtracer` + `svgo` (`--vectorize` for flat art),
`ffmpeg` (loudness-normalizing voice clips, and assembling `story` films).

## Output

Everything lands in `~/generated_media/` (override with `--output`), each file paired with a
`.meta.json` sidecar recording the prompt, model, references, and timestamp — that sidecar is
the provenance record.

## Cast bible

`cast/` holds a reusable visual world for comic-style scenario panels: named characters with
pinned ElevenLabs voices, CC0 set and pose vocabularies, a field-note art style scaffold, and
a licensing record for every asset pack. Character models are **not** committed — `fetch_assets.sh`
pulls the CC0 packs from Quaternius. See `cast/README.md` and `cast/licenses.md`.

## Cost

Roughly: image `flash` ~$0.04, image `pro` ~$0.12, Veo `fast` ~$0.15/sec, Veo `standard`
~$0.40/sec, Hailuo 3 $0.13/sec at 2K or $0.08/sec at 768P (direct only), voice `v3`
~$0.30/1K chars. Every run prints an estimate to stderr and every paid video call waits for
a confirmation before submitting. Default to the cheap tier unless quality demands otherwise.

Video is 10–50x the price of an image, so `story plan` exists to price a whole film before
spending anything on it.

## License

MIT — see [LICENSE](LICENSE). Third-party assets referenced by the cast bible carry their own
licenses, documented in `cast/licenses.md`.
