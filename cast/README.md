# Cast bible — named characters, sets, poses

The recurring visual world for scenario panels. After this file, a new comic shot is
**one compose command + one render command** — cameras and lighting are already placed, the
cast already has names, outfits, and a pose vocabulary.

## The cast

| Name | Model | Identity mark | Role in stories |
|---|---|---|---|
| **Omar** | Male_Suit | the suit | Lead trainer. Patient, asks before telling. |
| **Sara** | Female_Casual | **teal shirt** | Sharp engineer, usually right, occasionally overconfident. |
| **Khalid** | Male_Casual | **amber shirt** | Junior admin, clicks before reading — the cautionary tale. |
| **Noor** | Female_Dress | the dress | Ops lead. Calm under incident, voice of the correct answer. |
| **Hamdan** | Male_LongSleeve | **slate shirt** | Security engineer. "But who approved this?" |

Shirt color IS identity — never restyle a character between panels. Faces are basic by design:
emotion comes from pose, silhouette, camera distance, and bubble shape.

## The sets (`sets/*.json`)

- **classroom** — board + sticky notes, student desk with laptop. Cameras: `wide`, `ots`
  (over student's shoulder at the board), `close` (student face-on).
- **desk** — terminal work: big monitor with a dark terminal screen, angled laptop, mug,
  papers. Cameras: `wide`, `screen` (over shoulder at the monitor), `side` (profile).
- **incident** — dark server room: four racks with status LEDs (two racks red), ops console.
  Cameras: `wide`, `racks` (down the aisle), `console`.

Each set is a `render_scene.py` spec with an empty `cast` array; its asset paths are relative
to the cast directory, so render sets through `compose.py` (which absolutizes them), not
directly. Words never go into
the pixels — boards, screens and notes are blank color fields; dialogue lives in the slide's
bubble fields (RTL-safe, PDF-selectable).

## The poses (`poses.json`)

Harvested by scrubbing all 12 pack actions at 6 frames each (both packs carry the same
retargeted motions, so one vocabulary serves men and women): `presenting`, `gesturing`, `idle`,
`standing`, `seated`, `hand-raised`, `pointing`, `walking`, `running`, `cheering`, `collapsed`.

## Making a shot

**Write the brief first.** Copy `briefs/_template.md`, fill the story beat, blocking, camera,
and physics checklist per `cinematography.md`, THEN compose. Render a draft, grade it against
the brief (fill "Draft verdict"), fix, and only then render final. The two failure modes this
kills: poses that read wrong from the chosen angle (a point that looks like a punch), and
characters not grounded (perched on chair edges).

```bash
./fetch_assets.sh        # once per machine — CC0 packs, ~40 MB, not committed

python3 compose.py --set classroom \
    --place omar:presenting@1.75,2.35,0:-35 \
    --place sara:seated@1.1,-1.30,0.02:184 \
    -o my-shot.json

blender -b --python ../scripts/render_scene.py -- my-shot.json --quality draft   # look first
blender -b --python ../scripts/render_scene.py -- my-shot.json --quality final
```

Placement is `character:pose@x,y,z[:rot_z_deg]`, metres, rot 0 ≈ facing −y. The composed spec
is plain `render_scene.py` input — open it and hand-edit cameras/props when a shot needs more.
Then upload with the `/media` upload bridge and put the printed relPath in the slide's `image`
field.

Reference placements (from the rendered demos): seated characters sit ~0.05 m in front of the
chair center at z 0.02 with rot 184; the classroom trainer spot is `1.75,2.35,0:-35`.

## Rules

- Licensing: every asset is recorded in `licenses.md`. Add a row BEFORE adding an asset.
- `assets/` is fetched, never committed. New packs go through `fetch_assets.sh`.
- Sets are versioned by git — edit in place, small diffs, never fork `classroom-v2.json`.
- No text, logos, or real-brand lookalikes in geometry or materials.
