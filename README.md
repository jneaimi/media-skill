# media-skill

A [Claude Code](https://claude.com/claude-code) skill for generating images, video clips,
voiceovers, and text overlays — plus a headless Blender pipeline for rendering comic-panel
stills from a declarative JSON scene spec.

Say `/media` in Claude Code (or just ask for an image, a thumbnail, a voiceover), and the
skill picks the right command, model, and cost tier.

## What it does

| Command | Backend | Notes |
|---|---|---|
| `image` | Gemini (2.5 Flash Image / 3 Pro) | aspect ratios, 4K, reference images, transparent background, batch variations |
| `video` | Veo 3.1 (fast / standard) | 4–8s clips, 16:9 or 9:16, image-to-video |
| `voice` | ElevenLabs (v3 / Flash v2.5) | preset or raw voice IDs, EN + AR |
| `overlay` | Pillow + arabic-reshaper | **correct Arabic text on images** — connected letters, proper RTL |
| `render_scene.py` | headless Blender | JSON scene spec → comic panels, with a sha256 provenance manifest |

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
export GEMINI_API_KEY=...      # aistudio.google.com — images + video
export ELEVENLABS_API_KEY=...  # elevenlabs.io — voiceover
```

Scripts run under [`uv`](https://docs.astral.sh/uv/) with inline dependency metadata — no
virtualenv to manage:

```bash
uv run ~/.claude/skills/media/scripts/generate_media.py image "a lighthouse at dusk" --aspect 16:9
```

Optional extras: `blender` (panel rendering), `vtracer` + `svgo` (`--vectorize` for flat art),
`ffmpeg` (loudness-normalizing voice clips).

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

Roughly: image `flash` ~$0.04, image `pro` ~$0.12, video `fast` ~$0.15/sec, video `standard`
~$0.40/sec, voice `v3` ~$0.30/1K chars. Every run prints an estimate to stderr. Default to the
cheap tier unless quality actually demands otherwise.

## License

MIT — see [LICENSE](LICENSE). Third-party assets referenced by the cast bible carry their own
licenses, documented in `cast/licenses.md`.
