# Field-note panels — the generated-art pipeline for scenario beats

Scenario-panel art is GENERATED (nano banana / `gemini-2.5-flash-image`), not rendered.
This doc is the whole pipeline: prompt scaffold, references, the
critique checklist, and provenance. The shot-brief step (`briefs/_template.md`) is unchanged —
a brief still comes before any image.

## The one-command generate

```bash
uv run $MEDIA/scripts/generate_media.py image \
    "<STYLE SCAFFOLD> Scene: <the brief's staging paragraph>. <NO-TEXT GUARD> Wide 16:9 composition." \
    --reference cast/refs/style-01.jpg cast/refs/ref-<character>.jpg … \
    --aspect 16:9 --prefix day5-<beat> --output <workdir>
```

- `GEMINI_API_KEY` comes from the environment at run time (a secrets manager, or your
  shell profile) — never from the repo or a committed dotfile.
- The script writes a `.meta.json` sidecar (prompt, model, refs, timestamp) next to every
  PNG — that sidecar IS the provenance record; keep it with the accepted image.
- API note: image models return `NO_IMAGE` unless response modalities are TEXT+IMAGE —
  `generate_media.py` handles this; remember it when calling the REST API directly.

## Style scaffold (verbatim, every panel)

> Hand-drawn field-note style illustration, warm ink linework on cream paper with loose
> watercolor washes, like a designer's sketchbook journal.

## No-text guard (verbatim, every panel — words never in pixels)

> No text, no words, no letters anywhere in the image.

Speech bubbles, labels, and beats stay LAYOUT TEXT in the slide fields. Generated marks that
read as writing must be illegible squiggles — the checklist verifies this at full resolution.

## References — continuity without a rig

- `cast/refs/style-01.jpg` … — style anchors: accepted panels that define the look. Pass at
  least one on every call.
- `cast/refs/ref-khalid.jpg`, `ref-sara.jpg`, `ref-noor.jpg`, `ref-hamdan.jpg` — character
  sheets (front/side/three-quarter). Pass the sheet of every character IN the shot and name
  them in the prompt ("the man from the first reference image…"). Character continuity is
  reference-sheet discipline — skipping the sheet is how a character changes faces between
  panels.
- `--reference` takes up to 14 images; order them style-first, then characters.

## The critique checklist (the verifier loop, 2D edition)

Generation is cheap; shipping unreviewed is not. Before any upload, check the image at FULL
resolution against the brief:

1. **No legible text** — zoom the boards/screens/papers; regenerate if any real letters formed.
2. **Physics** — seated characters actually on chairs, feet plausible, hands touching what
   they hold; pointing/gesture arms read at silhouette level.
3. **Cast match** — each character against their sheet: hair, build, clothing colors.
4. **Story** — the beat's action is unambiguous from the image alone (the brief's verdict line).
5. **Style match** — linework/wash consistent with the style anchors; no photoreal drift.
6. **Count the people** — the model likes adding background figures; extras must be wanted.

One failed check → adjust the prompt (or crop/regenerate), never ship-and-hope. Record what
failed in the brief's verdict, same as the 3D drafts did.

## Provenance + archival

- Generation is NOT deterministic across model versions: the **accepted PNG is the canonical
  asset**, archived via the upload bridge (`--upload --day N`) and kept in the deck.
- Keep the `.meta.json` sidecar with the working files; copy its prompt into the shot brief's
  verdict block so a future regeneration starts from the accepted prompt.
- Runtime never calls generative APIs: generation happens here, at authoring time,
  and only human-accepted drafts reach a class.

## When to fall back to 3D

The Blender pipeline (`rigged-cast.md`) stays for cases generation handles badly: exact
repeatable staging across many shots, precise camera continuity, or measured spatial layouts.
Default for panels is generation.
