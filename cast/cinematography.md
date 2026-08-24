# Cinematography & staging — the knowledge behind every panel

Read this BEFORE composing a shot. A panel fails when the picture doesn't tell the story by
itself — poses, distances, eyelines, and the camera do the acting; bubbles only confirm what
the image already says. (Distilled from standard shot/blocking grammar: StudioBinder's shot
and blocking guides, Celtx's camera-angle guide, comic-craft eyeline/180° references.)

## 1. The shot-brief step (mandatory)

Never compose straight from an idea. Write a brief first (template in `briefs/_template.md`):

1. **Story beat** — one sentence: what changes in this panel?
2. **Emotion per character** — one word each (tempted, defensive, challenging, observing…).
3. **Who does what to whom** — the action line. If no character acts on another, say what
   each attends to.
4. **Spatial map** — a 3-line ASCII top-view: who is where, facing which way, distances.
5. **Camera** — shot size + angle + lens, and WHY (what should the viewer feel).
6. **Bubble space** — which corners the dialogue will take; keep faces/action clear of them.
7. **Physics checklist** — the §5 list, ticked per character.

Then compose, render a DRAFT, and grade the draft against the brief before going final.

## 2. Shot grammar (sizes, lenses, angles)

| Shot | Use | Lens (our 1600×900 frame) | Camera height |
|---|---|---|---|
| Establishing / wide | New location, spatial orientation — open every scene with one | 28–32 | ~1.5–1.8 m |
| Medium two-shot | Dialogue, relationships — the workhorse | 38–45 | chest/eye of subjects (~1.1–1.3 m) |
| Over-the-shoulder | One character's view of another / of a screen or board | 30–40 | shoulder of the near character |
| Close-up | Emotion beat, decision moment | 50–85 | subject's eye height |
| Insert | An object that matters (screen, LED, diff on a monitor) | 50+ | object height |

Angles carry psychology: **eye-level = neutral**; **low angle looking up = subject has power**;
**high angle looking down = subject vulnerable/exposed**. A seated character shot from the
standing character's eye height automatically reads as "on the back foot" — use that
deliberately, not accidentally. Tilted (Dutch) angles = something is wrong; save for incident
panels.

## 3. Blocking & body language

- **Distance IS relationship**: 0.5–0.9 m = intimate/confrontational; 1.2–2 m = working
  conversation; 3 m+ = detached observer. Pick the distance from the emotion, then place.
- **Orientation**: characters in conversation face each other (±30°). A character reacting to
  a thing faces the thing. NOBODY faces empty space or the camera without a reason.
- **Eyelines**: every character's gaze must land on the thing their attention is on — the
  other character, the screen, the racks. In a group, background characters watch the action;
  that's what makes them read as present rather than pasted in.
- **Open vs closed posture**: upright + arms low = calm authority; hands raised = appeal or
  exasperation; leaning in = pressure; turned away = avoidance.
- **The 180° rule across panels**: keep each character on a consistent side of frame through a
  scene (Sara right, Khalid left in one panel → keep them there in the next) so readers track
  who is who without re-reading.
- **Silhouette test**: if the render were blacked out, could you still tell who does what?
  Overlapping characters fail this — never let one body occlude another's action or face.

## 4. Pose vocabulary — reading distance and angle warnings

Poses are held animation frames; the SAME pose reads differently by camera angle:

- `pointing` (Punch:13) — reads as **a punch** from any ¾-rear angle, especially near another
  character. Use ONLY in true profile with ≥1.5 m of clear space along the arm, pointing at a
  THING (rack, board), never at a person. For person-directed challenges use `presenting`.
- `presenting` (Clapping:5) — one hand raised: making a point, appeal, "who approved these?".
  Safe from most angles. Best conversational-challenge pose.
- `gesturing` (Clapping:24) — both hands up: exasperation/enthusiasm. Big; use sparingly.
- `seated` (Sitting:11) — REQUIRES seat-contact calibration (§5). Never floor-snap.
- `standing`/`idle` — calm baseline; rotate the body toward the point of attention.
- `hand-raised` (Jump:15) — student question; only frame chest-up (legs are mid-jump).
- `collapsed`, `running`, `cheering` — big story beats only; they dominate any frame they're in.

## 5. Physics / grounding checklist (every character, every shot)

- [ ] **Feet contact**: standing characters' both feet on the floor plane (floor_snap does
  this — verify in the draft anyway; some poses carry a lifted heel).
- [ ] **Seat contact**: for `seated`, hips rest ON the seat surface, thighs supported by the
  seat depth, feet reaching the floor. Calibrate per chair: place at the chair's center
  (not its front edge), then render the §6 contact probe and adjust z until there is no gap
  and no interpenetration. For our furniture-pack Chair at height 0.95, the validated
  placement is **character y = chair y − 0.02 (i.e. 3–5 cm behind chair center), z = 0.02**.
- [ ] **No interpenetration**: limbs vs table/chair/other characters.
- [ ] **Personal space is intentional**: if two characters are closer than 0.9 m, the story
  demanded it.
- [ ] **Weight reads**: a leaning/pointing pose needs the feet under the mass — if a pose
  looks like it would tip over, change the pose rather than the camera.

## 6. The contact probe

Before finalizing any seated/interacting character, render a cheap verification close-up:

```bash
# add a probe camera to the composed spec, then:
#   { "name": "probe", "loc": [<side-on, ~1.5m away>], "target": [<hip height>], "lens": 60 }
blender -b --python ../scripts/render_scene.py -- shot.json --quality draft --only probe
```

Look at ONE thing: the contact points. Fix placement, re-probe, THEN render real cameras.

## 7. Per-set camera notes

- **classroom** — open scenes on `wide` (30 mm, from the empty corner). Dialogue across the
  student desk: medium two-shot from the side wall (~40 mm, eye height 1.2 m) keeps board +
  both faces. The board is at y≈3, so shots toward it need the camera south of the desk.
- **desk** — `side` (40 mm) is the story shot (person + screen in profile); `screen` (OTS at
  the monitor) for what-they-see; keep the monitor glow inside frame — it's the light source
  story-wise.
- **incident** — dark set: silhouettes matter more than faces. Racks read best from a low
  ¾ angle (lens 28–32, height ≤1.4 m). A pointing character must be in PROFILE against the
  rack wall. The console glow is the second light anchor; put a character's face near it when
  they deliver the line.

## 8. Bubble space

Bubbles live in corners (tl/tr/bl/br). Compose so the corner a speaker's bubble occupies is
EMPTY sky/wall above-ish that speaker, on their side of frame (Sara's tr bubble → Sara on the
right). Never place a head in a bubble corner. Panel 16:9 at 1600×900: the top fifth and the
outer sixths are bubble-safe zones in the wide shots.

## Sources

Shot/blocking grammar per StudioBinder (camera shots guide; blocking & staging), Celtx camera
angles guide, PolarPro filmmaking 101, comic eyeline/flow guides (Salgood Sam; nattosoup comic
craft), Wikipedia: 180-degree rule. Local validation: every rule above was checked against our
own failed drafts (the "punch" read, the perched sit, the pasted-in observer).
