# Prompting Hailuo 3 (MiniMax H3)

The rules the `story` prompt compiler encodes, written out so you can hand-write a prompt
for `video` and get the same discipline. Sources are MiniMax's own prompt guide plus the
community writeups linked at the bottom.

## The six-element order

Every good H3 prompt answers six things, in this order. The compiler emits exactly this:

1. **Format / style** — live action, animation, product film, motion design, plus the look
2. **Opening composition** — subject, environment, framing, lighting
3. **Observable action** — what actually changes over the clip's seconds
4. **Camera** — one motivated move, or an explicit static
5. **Sound** — three separate layers (below)
6. **Ending state** — the final composition

Anything missing gets invented by the model, and what it invents is usually the average of
its training data.

## One camera move. One.

The single most common cause of mush is stacking moves. "Zoom in as the drone orbits with
handheld tracking and a whip pan" is four instructions the camera cannot obey at once.

Vocabulary the model reads reliably (this is `CAMERA_VOCABULARY` in `storyboard.py`, and
the spec validator warns on anything outside it):

| Intent | Write |
|---|---|
| Physically approach | `slow push in` · `fast push in` |
| Change focal length | `controlled zoom in` · `controlled zoom out` |
| Follow the subject | `low tracking shot` · `high tracking shot` · `handheld follow` |
| Reveal horizontally | `slow pan left/right` · `fast pan left/right` · `truck left/right` |
| Move around the subject | `slow arc shot` |
| Vertical | `crane up` · `crane down` |
| Nothing | `locked static shot` |

## First-and-last-frame prompts describe the motion, not the pictures

The failure mode: describing two images, and getting a cut between them instead of a move.
Name the continuity explicitly and state that the clip must *land* on picture 2.

> Picture 1 aligns with the opening frame and Picture 2 aligns with the final frame.
> Render the single continuous motion that connects them. Preserve the characters,
> wardrobe, set dressing and lighting of Picture 1 throughout, and land exactly on the
> pose, spacing and composition of Picture 2.

That paragraph is emitted automatically for every `bridge`-mode shot.

## Frames and references are different modes — never both

**A first/last-frame request cannot also carry reference images, video, or audio, and a
reference request cannot carry frames.** This is enforced by the model, not by any one
provider: MiniMax rejects the combination, and OpenRouter silently drops
`input_references` and treats the call as image-to-video. `video_providers.validate()`
refuses it locally rather than letting either happen.

The way to get both identity control *and* frame control is to put identity into the frames
at the image stage:

```bash
# Cast references shape the panels...
uv run scripts/generate_media.py image "..." --reference cast/refs/ref-sara.jpg
# ...and the panels are what the video model gets as frames.
uv run scripts/generate_media.py video "..." --model hailuo-3 \
  --first-frame panel-01.png --last-frame panel-02.png
```

This is exactly what `story` does, and why the cast bible feeds the board rather than the
clips.

## Audio: three named layers

"Epic audio" carries no timing information and produces nothing useful. Name each layer:

- **Soundscape** — room tone, footsteps, weather, physical impacts. Give direction and
  time: "footsteps cross left to right", not "atmospheric".
- **Dialogue** — wrapped in `<d>[Language] text</d>`, attributed to a stable speaker, with
  optional delivery. Multiple speakers keep stable IDs across the whole film.
- **Non-diegetic music** — the score, or the literal string `N/A` for silence. Say `N/A`;
  omitting the layer invites the model to add music.

```
Soundscape: low open-plan hum, a distant printer cycling.
Sara, quietly, says: <d>[English] Come look at this before you push it.</d>
Non-diegetic music: N/A.
```

For voiceover, mark it `off-screen` and say the visible character's lips stay closed.

## Text in frame

H3 can render brand text and titles — but only reliably with a negative guard attached:

> Credits must be clean and legible. Do not introduce Chinese text, garbled characters, or
> misspellings.

By default the compiler does the opposite and forbids all on-screen text, because baked-in
text can't be edited and can't be localised. Opt in per shot with `"allow_text": true`, or
spec-wide with a top-level `"allow_text": true`.

## Timecode anything with more than one beat

```
[0–2 seconds] High-angle overhead. The character sits on a purple floor, looking up.
[2–4 seconds] Push in to her right arm.
```

Use cuts only to reveal new information. In a 5–6 second clip, one strong shot beats three
rushed ones — which is the argument for `story`: let the chain make the cuts, at panels you
approved, instead of asking one clip to contain them.

## Failure modes, in rough order of frequency

1. **Too many events.** Five transformations, three cuts, dialogue and a logo reveal do not
   fit in six seconds. Cut events, or add seconds (H3 goes to 15).
2. **Conflicting camera directions.** The camera cannot be locked while orbiting.
3. **Vague sound.** No timing, no direction, no layers.
4. **Negative-only prompts.** "No flicker, no morphing" doesn't describe a shot. Write the
   positive sequence first; add guards afterwards.
5. **Ambiguous references.** Two reference images with no stated role compete. Assign each
   one a job — "Image 1 for wardrobe, Image 2 for the product" — and state the exclusions:
   "use Video 1 for the walking rhythm only; do not copy its actor, background or lighting".
6. **Assuming references carry identity.** Name the hair, the garment, the accessory in
   words as well. Detail drift starts wherever the prompt went quiet.

## Model-specific facts worth remembering

- **No seed.** H3 has no seed parameter on either provider — reruns are not reproducible.
  Consistency has to come from frames and references, which is the whole design of `story`.
- **2K is the only resolution on OpenRouter.** The 768P tier ($0.08/s vs $0.13/s) and the
  4-second floor exist only on MiniMax direct.
- **Durations are a discrete set**, not a range: 5–15 on OpenRouter, 4–15 direct. An
  out-of-set value is a 400; `--duration` is validated locally first.
- **Generation is slow.** A 6-second 2K clip took ~350 seconds end to end in testing. The
  default `--timeout` is 900s for that reason.

Check the live capability record any time you suspect drift:

```bash
uv run scripts/generate_media.py caps --model hailuo-3
```

## Sources

- [MiniMax video generation API](https://platform.minimax.io/docs/guides/video-generation)
- [MiniMax H3 prompt guide](https://hailuo3.me/blog/minimax-h3-prompt-guide)
- [Hailuo 3.0 prompt guide: 40 prompts](https://www.imagine.art/blogs/hailuo-3-0-prompt-guide)
- [Runware: reference-driven consistency](https://runware.ai/docs/models/minimax-h3/guides/reference-driven-consistency)
- [OpenRouter video generation](https://openrouter.ai/docs/guides/overview/multimodal/video-generation)
