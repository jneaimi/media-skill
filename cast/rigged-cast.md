# Rigged cast — Lollipop Characters headless recipe

How to assemble, dress, pose, and render a Lollipop (Auto-Rig-Pro-style) character from a
headless Blender script. Every rule below was earned by a broken render; don't skip steps.
The Quaternius cast in `cast.json` stays the fallback for background extras — Lollipop is the
hero cast for close and medium shots.

## Appending a character

- `bpy.data.libraries.load("Characters/<Name>.blend")`, keep every object EXCEPT names starting
  `cs_` (rig widget meshes). Link all into the scene.
- The rig is the only ARMATURE object. The skin mesh is found by EXACT name, not vertex count —
  prefix or max-verts matching grabs gums/hair-emitter planes:
  `James → James`, `Mira → Mira`, `Farida → Farida - Body`, `Femi → Femi_Body`, `Mike → Mike`.
- Parent any character mesh that has `parent is None` to the rig
  (`matrix_parent_inverse.identity()`) — loose accessories otherwise stay behind at world origin.
- Strip `COLLISION`, `PARTICLE_SYSTEM`, `CLOTH`, `SOFT_BODY` from every character mesh.
  Fur/hair particle systems (e.g. `Femi - Jacket`) expand to strand geometry at render time and
  will OOM a small VM; a cloth sim (`Farida - Hair Sim`) tries to simulate during the render.
- Scale ONLY the rig (all meshes are parented to it): measure the body's evaluated z-span,
  `S = target_height / span`, `rig.scale = (S, S, S)`. Then set rig loc/rot.

## Dressing (the part that bites)

Garments' native fit is a cloth sim — unusable for stills. Do what the pack's own addon does
(see `Addon/Lollipop_Characters.py`, the `settarget` operator), not surface-deform binding:

1. `garment.hide_render = False; garment.hide_viewport = False` — some garments SHIP HIDDEN
   (Mike's polo) and silently never render.
2. Parent to the rig, `matrix_parent_inverse.identity()`.
3. Strip `CLOTH`, `COLLISION`, `SOFT_BODY`, `SURFACE_DEFORM`, `PARTICLE_SYSTEM`, and any
   existing `ARMATURE` modifier.
4. KEEP the authored `SHRINKWRAP`; set `target = body` if it ships with none. If `offset < 0.012`
   set `wrap_mode = 'OUTSIDE_SURFACE'`, `offset = 0.012` — skin-tight garments otherwise sit
   inside the skin (invisible) or z-fight.
5. Weight transfer: add a `DATA_TRANSFER` modifier (`object = body`, `use_vert_data`,
   `data_types_verts = {'VGROUP_WEIGHTS'}`), move it to the TOP of the stack, then
   `bpy.ops.object.datalayout_transfer(...)` + `modifier_apply` under a
   `temp_override(object=o, active_object=o, selected_objects=[o])`.
6. Add an `ARMATURE` modifier (`object = rig`) and move it to index 0 — or index 1 when the
   first modifier is `MIRROR` (mirror first so `.l/.r` groups flip onto the mirrored half).

Surface-deform binding (bind at rest, scale later) *appears* to work on the fitted garments
(James, Farida, Femi) but explodes Mike's pattern-panel garments into spikes and cannot follow
strong pose changes at the shoulder. The weight-transfer recipe works for every character.

## Posing (IK)

- `ik_fk_switch` is already 0.0 (IK) on `c_hand_ik.*` / `c_foot_ik.*`. Set `auto_stretch = 0.0`
  on all four or limbs go spaghetti.
- Pose-bone `b.matrix` is in armature-data space: place a controller at character-local `v` with
  `m.translation = Vector(v) / rig.scale[0]`; call `view_layer.update()` after EVERY placement.
- Character-local axes: x right, −y forward, z up. Controllers: `c_root_master.x`,
  `c_leg_pole.l/r`, `c_foot_ik.l/r`, `c_arms_pole.l/r`, `c_hand_ik.l/r`.
- Sit (seat plane `SEAT`, measured — see below): root `(0, 0.02, SEAT+0.09)`, feet
  `(±0.11, −0.40, 0.03)`, leg poles `(±0.13, −1.1, SEAT+0.25)`, hands `(±0.15, −0.30, SEAT+0.16)`
  (lap) or `(±0.14, −0.44, 0.77)` with poles `(±0.55, 0.15, SEAT+0.15)` (hands on a 0.74 desk).
- Gestures: translating a hand IK target keeps the rest rotation → limp wrist. Aim the bone:
  build the matrix from `direction.to_track_quat('Y', 'Z')` so fingers continue the arm's line.
- Ground standing characters: legs vary per character, so after posing evaluate the body's min z
  and shift `rig.location.z -= min_z` — otherwise short-legged characters float on dangling IK.
- Seat plane measurement (per chair asset, once): largest upward-facing (`normal.z > 0.85`)
  polygon-area band below 70 % of chair height. Scale chairs to ADULT proportions against the
  desk: 1.15 m chair → seat z ≈ 0.44 against a 0.74 m desk (the pack's 0.95 m default seats
  at 0.36 — child-sized next to a 1.75 m character).
- **Never trust remembered furniture orientation — measure it.** The furniture-pack chairs
  open along **+X at rot 0** (backrest on the −X side), not along Y as the silhouette suggests;
  a sitter facing +Y needs the chair at rot 90. A wrong chair rotation hides perfectly from a
  side camera (the backrest tucks behind the character's silhouette) while the character's arm
  passes straight through it — only the BVH overlap check and a top-down QA view expose it.

## The verifier loop (run before every final render)

Never ship a panel on eyeballs alone — render QA views AND assert the physics:

1. **QA cameras**: besides the shot camera, render front, side, and top-down views of the cast
   centroid. Staging errors that hide from the shot camera (a backrest inside a torso, a
   character standing inside a desk) are obvious from the other angles.
2. **BVH overlap** (`mathutils.bvhtree`, trees built from evaluated meshes in world space):
   character body vs every furniture/prop mesh, and cast vs cast. **Erode the character mesh
   ~1.5 cm along vertex normals first** — legitimate surface contact (back against backrest,
   palms on desk) stops counting, true interpenetration survives.
3. **Contact assertions**: standing minz within [−0.02, 0.04] of the floor; seated hip band on
   the measured seat plane; palms-on-desk overlap confined to the tabletop z band.
4. Print both sides' overlap regions (x/y/z extents) on failure — the region tells you WHICH
   body part hit WHICH furniture part; without it you tune blind. Beware low-poly furniture:
   face centers of giant faces sit far from the actual contact zone.
5. IK placement self-corrects: assign the controller matrix, measure achieved vs requested,
   re-aim with the error, iterate ≤4×; report `PLACE-MISS` beyond 1 cm.

Iterate staging against the verifier with `NORENDER=1` (seconds per cycle), then QA renders,
then finals. A shot ships only when the verifier is silent and the QA views read correctly.

## Rendering on small machines

Cycles CPU + two rigged characters peaks ~3 GB RSS with particles stripped — and hard-freezes
an 8 GB WSL2 VM when they aren't. Always: `threads_mode='FIXED', threads=2`, `nice -n 19`,
and run under a watchdog that kills Blender when `MemAvailable` drops below a floor
(`cast/` ships `memguard.sh` in the working scratchpad; 1 GB floor). A silent no-output death
is the OOM-kill signature — check the guard's log line before debugging the scene.
