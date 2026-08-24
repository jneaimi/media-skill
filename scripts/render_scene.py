"""render_scene.py — declarative JSON scene spec -> comic-panel stills + manifest.

Runs INSIDE Blender (stdlib + bpy + mathutils only, no pip deps):

    blender -b --python render_scene.py -- <spec.json> [--quality draft|final] [--only <camera>]

Paths inside the spec (blend files, outdir) resolve relative to THE SPEC FILE's directory,
not the current working directory. `blend` paths may point anywhere on disk, including
outside the repo — this is a trusted local tool and the spec author's own machine is the
injection surface. `render.outdir` is different: it must resolve to somewhere under the
spec file's own directory (a spec cannot be used to write files elsewhere on disk).

Spec schema (version 1):

    {
      "version": 1,
      "units": { "character_height": 1.75 },        // metres; auto-scale reference
      "world":  { "color": [0.85, 0.87, 0.90], "strength": 0.55 },
      "materials": {                                 // name -> flat material
        "wall": { "rgb": [0.88, 0.86, 0.82], "rough": 0.8 }
      },
      "boxes": [                                     // primitive set-dressing
        { "name": "floor", "size": [14,14,0.1], "loc": [0,0,-0.05], "rot_deg": [0,0,0], "material": "wall" }
      ],
      "props": [                                     // appended furniture blends
        { "blend": "assets/furniture/Blends/Chair.blend", "height": 0.95, "loc": [1.1,-1.35,0], "rot_z_deg": 180 }
      ],
      "cast": [
        { "name": "student", "blend": "assets/animatedmen/Blends/Male_Casual.blend",
          "action": "Man_Sitting", "frame": 11, "loc": [1.1,-1.30,0.02], "rot_z_deg": 184,
          "floor_snap": false,                       // default true; sitting poses set false + explicit z
          "recolor": [ { "slot_prefix": "Shirt", "rgb": [0.07,0.28,0.34] } ] }
      ],
      "lights": {
        "sun":  { "energy": 3.2, "angle_deg": 20, "rot_deg": [48,-18,30] },
        "fill": { "energy": 250, "size": 6, "loc": [-3,-4,3.5], "rot_deg": [55,0,-30] }
      },
      "cameras": [                                   // at least one; names unique;
                                                       // each needs exactly one of target/target_bone
        { "name": "wide", "loc": [-3.6,-3.6,1.8], "target": [0.5,0.5,1.05], "lens": 30 },
        { "name": "close", "loc": [0.7,-0.1,1.35],
          "target_bone": { "cast": "student", "bone": "head", "offset": [0,-0.12,-0.02] }, "lens": 50 }
      ],
      "render": { "resolution": [1600, 900],
                  "samples": { "draft": 24, "final": 128 },
                  "draft_scale": 0.5,                // draft renders at half resolution
                  "outdir": "shots", "prefix": "panel" }
    }

Unknown top-level keys, wrong field types/shapes, and dangling references (a box material that
isn't defined, a camera target_bone.cast that isn't a cast member) are all hard errors, exit 2,
naming the offending path (e.g. "cast[0].loc: expected 3 numbers, got string"). All of this is
checked BEFORE any bpy scene mutation — a malformed spec never leaves a half-built .blend state
behind. Checks that require inspecting a blend's contents (does this action/bone actually exist)
can only run once that file is loaded, so those still surface during scene-build, not validation.

Output: <outdir>/<prefix>-<camera>.png per rendered camera, plus <outdir>/manifest.json — sha256
of the spec and every input blend, and a "frames" map from output name to {file, px, quality}.
A full run (no --only) rewrites the whole manifest. A `--only <camera>` run loads any existing
manifest.json in outdir and updates just the rendered camera's frame entry, leaving the others
(and their quality) untouched — so a mixed draft/final panel set stays honest about which frame
was rendered at which quality. Byte-stable across re-runs given identical spec+inputs+quality;
pixel identity of the rendered PNGs is NOT promised (Cycles sampling/denoise is not guaranteed
bit-identical run to run).
"""
import bpy
import hashlib
import json
import math
import os
import sys

from mathutils import Vector

FRAME = 1  # arbitrary global scene frame; every cast member holds its own action frame here via NLA

ALLOWED_TOP_KEYS = {
    "version", "units", "world", "materials", "boxes", "props", "cast", "lights", "cameras", "render",
}


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        fail("usage: blender -b --python render_scene.py -- <spec.json> [--quality draft|final] [--only <camera>]")
    spec_path = argv[0]
    quality = "final"
    only = None
    i = 1
    while i < len(argv):
        if argv[i] == "--quality" and i + 1 < len(argv):
            quality = argv[i + 1]
            i += 2
        elif argv[i] == "--only" and i + 1 < len(argv):
            only = argv[i + 1]
            i += 2
        else:
            fail(f"unknown or incomplete argument '{argv[i]}'")
    if quality not in ("draft", "final"):
        fail(f"--quality must be 'draft' or 'final', got '{quality}'")
    return spec_path, quality, only


def load_spec(spec_path):
    if not os.path.isfile(spec_path):
        fail(f"spec file not found: {spec_path}")
    try:
        with open(spec_path) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        fail(f"invalid JSON in {spec_path}: {e}")


# ---- spec validation (pure Python + os.path only — no bpy, runs before any scene mutation) ----

def err(path, msg):
    fail(f"{path}: {msg}")


def type_name(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "object"
    if isinstance(v, (int, float)):
        return "number"
    return type(v).__name__


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def req(d, key, path):
    if key not in d:
        err(f"{path}.{key}", "required")
    return d[key]


def check_dict(path, val):
    if not isinstance(val, dict):
        err(path, f"expected an object, got {type_name(val)}")


def check_list(path, val):
    if not isinstance(val, list):
        err(path, f"expected a list, got {type_name(val)}")


def check_str(path, val):
    if not isinstance(val, str) or not val:
        err(path, f"expected a non-empty string, got {val!r}")


def check_bool(path, val):
    if not isinstance(val, bool):
        err(path, f"expected a boolean, got {type_name(val)}")


def check_number(path, val):
    if not is_num(val):
        err(path, f"expected a number, got {type_name(val)}")


def check_positive_number(path, val):
    if not is_num(val) or val <= 0:
        err(path, f"expected a positive number, got {val!r}")


def check_positive_int(path, val):
    if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
        err(path, f"expected a positive integer, got {val!r}")


def check_nonneg_int(path, val):
    if not isinstance(val, int) or isinstance(val, bool) or val < 0:
        err(path, f"expected an integer >= 0, got {val!r}")


def check_vec(path, val, n=3):
    if not isinstance(val, list):
        err(path, f"expected {n} numbers, got {type_name(val)}")
    if len(val) != n or not all(is_num(v) for v in val):
        err(path, f"expected {n} numbers, got {val!r}")


def check_rgb(path, val):
    if not isinstance(val, list):
        err(path, f"expected 3 numbers in 0..1, got {type_name(val)}")
    if len(val) != 3 or not all(is_num(v) and 0 <= v <= 1 for v in val):
        err(path, f"expected 3 numbers in 0..1, got {val!r}")


def validate_spec(spec, base):
    check_dict("<root>", spec)
    unknown = set(spec) - ALLOWED_TOP_KEYS
    if unknown:
        err("<root>", f"unknown top-level key(s) {sorted(unknown)}; allowed keys: {sorted(ALLOWED_TOP_KEYS)}")

    if "units" in spec:
        units = spec["units"]
        check_dict("units", units)
        if "character_height" in units:
            check_positive_number("units.character_height", units["character_height"])

    if "world" in spec:
        w = spec["world"]
        check_dict("world", w)
        if "color" in w:
            check_rgb("world.color", w["color"])
        if "strength" in w:
            check_positive_number("world.strength", w["strength"])

    materials = spec.get("materials", {})
    check_dict("materials", materials)
    for name, m in materials.items():
        p = f"materials.{name}"
        check_dict(p, m)
        check_rgb(f"{p}.rgb", req(m, "rgb", p))
        if "rough" in m:
            check_positive_number(f"{p}.rough", m["rough"])

    boxes = spec.get("boxes", [])
    check_list("boxes", boxes)
    for i, b in enumerate(boxes):
        p = f"boxes[{i}]"
        check_dict(p, b)
        check_str(f"{p}.name", req(b, "name", p))
        check_vec(f"{p}.size", req(b, "size", p))
        check_vec(f"{p}.loc", req(b, "loc", p))
        if "rot_deg" in b:
            check_vec(f"{p}.rot_deg", b["rot_deg"])
        mat_name = req(b, "material", p)
        check_str(f"{p}.material", mat_name)
        if mat_name not in materials:
            err(f"{p}.material", f"unknown material '{mat_name}'; defined materials: {sorted(materials)}")

    props = spec.get("props", [])
    check_list("props", props)
    for i, pr in enumerate(props):
        p = f"props[{i}]"
        check_dict(p, pr)
        blend = req(pr, "blend", p)
        check_str(f"{p}.blend", blend)
        if not os.path.isfile(os.path.join(base, blend)):
            err(f"{p}.blend", f"file not found: {blend}")
        check_positive_number(f"{p}.height", req(pr, "height", p))
        check_vec(f"{p}.loc", req(pr, "loc", p))
        if "rot_z_deg" in pr:
            check_number(f"{p}.rot_z_deg", pr["rot_z_deg"])

    cast = spec.get("cast", [])
    check_list("cast", cast)
    cast_names = []
    for i, c in enumerate(cast):
        p = f"cast[{i}]"
        check_dict(p, c)
        check_str(f"{p}.name", req(c, "name", p))
        blend = req(c, "blend", p)
        check_str(f"{p}.blend", blend)
        if not os.path.isfile(os.path.join(base, blend)):
            err(f"{p}.blend", f"file not found: {blend}")
        check_str(f"{p}.action", req(c, "action", p))
        check_nonneg_int(f"{p}.frame", req(c, "frame", p))
        check_vec(f"{p}.loc", req(c, "loc", p))
        if "rot_z_deg" in c:
            check_number(f"{p}.rot_z_deg", c["rot_z_deg"])
        if "floor_snap" in c:
            check_bool(f"{p}.floor_snap", c["floor_snap"])
        if "recolor" in c:
            check_list(f"{p}.recolor", c["recolor"])
            for j, rule in enumerate(c["recolor"]):
                rp = f"{p}.recolor[{j}]"
                check_dict(rp, rule)
                check_str(f"{rp}.slot_prefix", req(rule, "slot_prefix", rp))
                check_rgb(f"{rp}.rgb", req(rule, "rgb", rp))
        cast_names.append(c["name"])

    lights = spec.get("lights", {})
    check_dict("lights", lights)
    if "sun" in lights:
        s = lights["sun"]
        check_dict("lights.sun", s)
        if "energy" in s:
            check_positive_number("lights.sun.energy", s["energy"])
        if "angle_deg" in s:
            check_number("lights.sun.angle_deg", s["angle_deg"])
        if "rot_deg" in s:
            check_vec("lights.sun.rot_deg", s["rot_deg"])
    if "fill" in lights:
        f = lights["fill"]
        check_dict("lights.fill", f)
        if "energy" in f:
            check_positive_number("lights.fill.energy", f["energy"])
        if "size" in f:
            check_positive_number("lights.fill.size", f["size"])
        if "loc" in f:
            check_vec("lights.fill.loc", f["loc"])
        if "rot_deg" in f:
            check_vec("lights.fill.rot_deg", f["rot_deg"])

    cameras = spec.get("cameras", [])
    check_list("cameras", cameras)
    if not cameras:
        err("cameras", "expected a non-empty list")
    seen_names = set()
    for i, cd in enumerate(cameras):
        p = f"cameras[{i}]"
        check_dict(p, cd)
        name = req(cd, "name", p)
        check_str(f"{p}.name", name)
        if name in seen_names:
            err(f"{p}.name", f"duplicate camera name '{name}'")
        seen_names.add(name)
        check_vec(f"{p}.loc", req(cd, "loc", p))
        if "lens" in cd:
            check_positive_number(f"{p}.lens", cd["lens"])
        has_target = "target" in cd
        has_target_bone = "target_bone" in cd
        if has_target == has_target_bone:
            err(p, "expected exactly one of 'target' or 'target_bone'")
        if has_target:
            check_vec(f"{p}.target", cd["target"])
        else:
            tb = cd["target_bone"]
            check_dict(f"{p}.target_bone", tb)
            cast_ref = req(tb, "cast", f"{p}.target_bone")
            check_str(f"{p}.target_bone.cast", cast_ref)
            if cast_ref not in cast_names:
                err(f"{p}.target_bone.cast", f"unknown cast member '{cast_ref}'; cast: {sorted(cast_names)}")
            check_str(f"{p}.target_bone.bone", req(tb, "bone", f"{p}.target_bone"))
            if "offset" in tb:
                check_vec(f"{p}.target_bone.offset", tb["offset"])

    render = spec.get("render", {})
    check_dict("render", render)
    if "resolution" in render:
        res = render["resolution"]
        if not isinstance(res, list) or len(res) != 2 or not all(
            isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in res
        ):
            err("render.resolution", f"expected 2 positive integers, got {res!r}")
    if "samples" in render:
        samples = render["samples"]
        check_dict("render.samples", samples)
        for key in ("draft", "final"):
            if key in samples:
                check_positive_int(f"render.samples.{key}", samples[key])
    if "draft_scale" in render:
        check_positive_number("render.draft_scale", render["draft_scale"])
    if "outdir" in render:
        check_str("render.outdir", render["outdir"])
    if "prefix" in render:
        check_str("render.prefix", render["prefix"])

    outdir_rel = render.get("outdir", "shots")
    abs_base = os.path.realpath(base)
    abs_outdir = os.path.realpath(os.path.join(base, outdir_rel))
    if os.path.commonpath([abs_base, abs_outdir]) != abs_base:
        err("render.outdir", f"'{outdir_rel}' escapes the spec's directory")


# ---- scene construction (bpy) ----

def make_material(name, rgb, rough):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    bsdf.inputs["Roughness"].default_value = rough
    return mat


def build_materials(spec):
    return {
        name: make_material(name, m["rgb"], m.get("rough", 0.8))
        for name, m in spec.get("materials", {}).items()
    }


def build_world(spec, scene):
    w = spec.get("world", {})
    world = bpy.data.worlds.new("world")
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (*w.get("color", [0.5, 0.5, 0.5]), 1.0)
    bg.inputs[1].default_value = w.get("strength", 1.0)
    scene.world = world


def build_boxes(spec, materials, scene):
    for b in spec.get("boxes", []):
        rot = [math.radians(d) for d in b.get("rot_deg", [0, 0, 0])]
        # size=2 gives a ±1 cube, so scaling by size/2 yields FULL edge lengths in metres.
        # (size=1 here silently halved every box — spec sizes must mean what they say.)
        bpy.ops.mesh.primitive_cube_add(size=2, location=b["loc"], rotation=rot)
        o = bpy.context.active_object
        o.name = b["name"]
        size = b["size"]
        o.scale = (size[0] / 2, size[1] / 2, size[2] / 2)
        o.data.materials.append(materials[b["material"]])


def append_all(path, tag, scene):
    with bpy.data.libraries.load(path) as (src, dst):
        dst.objects = list(src.objects)
        dst.actions = list(src.actions)
    objs = [o for o in dst.objects if o is not None]
    for o in objs:
        scene.collection.objects.link(o)
        o.name = f"{tag}_{o.name}"
    actions = {a.name.split(".")[0]: a for a in dst.actions if a}
    return objs, actions


def build_props(spec, base, scene):
    input_blends = []
    for i, p in enumerate(spec.get("props", [])):
        rel = p["blend"]
        objs, _ = append_all(os.path.join(base, rel), f"prop{i}", scene)
        meshes = [o for o in objs if o.type == 'MESH']
        zs = [(o.matrix_world @ Vector(c)).z for o in meshes for c in o.bound_box]
        scale = p["height"] / (max(zs) - min(zs))
        loc = p["loc"]
        rot_z = math.radians(p.get("rot_z_deg", 0))
        for o in meshes:
            if o.parent is None:
                o.scale = (o.scale[0] * scale, o.scale[1] * scale, o.scale[2] * scale)
                o.location = (loc[0], loc[1], loc[2] - min(zs) * scale)
                o.rotation_euler = (o.rotation_euler[0], o.rotation_euler[1], rot_z)
        input_blends.append(rel)
    return input_blends


def eval_bbox(mesh_obj, depsgraph):
    ev = mesh_obj.evaluated_get(depsgraph)
    m = ev.to_mesh()
    ws = [ev.matrix_world @ v.co for v in m.vertices]
    zs = [v.z for v in ws]
    ev.to_mesh_clear()
    return min(zs), max(zs)


def freeze(arm, action, at_frame):
    """Hold `action`'s frame `at_frame` on the global FRAME via an NLA strip offset.

    Never assign the action directly to animation_data.action: two cast members holding
    different frames of different actions at the SAME scene frame need independent strip
    offsets, and a shared/assigned action couples every armature to one timeline.
    """
    ad = arm.animation_data or arm.animation_data_create()
    ad.action = None
    tr = ad.nla_tracks.new()
    start = int(FRAME - at_frame + action.frame_range[0])
    st = tr.strips.new("pose", start, action)
    st.extrapolation = 'HOLD'


def bone_world(arm, substr):
    for pb in arm.pose.bones:
        if substr.lower() in pb.name.lower():
            return arm.matrix_world @ pb.head
    return None


def recolor(mesh_obj, rules):
    # These library-appended materials carry TWO Material Output nodes with the live shader
    # behind the second one; reassigning material_slots[i].material after material.copy()
    # silently no-ops. Clearing the node tree and rebuilding a single BSDF -> Output is the
    # only reliable recolor (cost three render cycles to find).
    for rule in rules:
        prefix = rule["slot_prefix"]
        rgb = rule["rgb"]
        matched = False
        for ms in mesh_obj.material_slots:
            if ms.material and ms.material.name.startswith(prefix):
                matched = True
                nt = ms.material.node_tree
                nt.nodes.clear()
                out = nt.nodes.new('ShaderNodeOutputMaterial')
                sh = nt.nodes.new('ShaderNodeBsdfDiffuse')
                sh.inputs['Color'].default_value = (*rgb, 1.0)
                nt.links.new(sh.outputs['BSDF'], out.inputs['Surface'])
        if not matched:
            print(f"WARN: recolor slot_prefix '{prefix}' matched no material slot on {mesh_obj.name}")


def build_cast(spec, base, scene, units):
    char_height = units.get("character_height", 1.75)
    cast_objs = {}
    input_blends = []
    for c in spec.get("cast", []):
        name = c["name"]
        rel = c["blend"]
        objs, acts = append_all(os.path.join(base, rel), name, scene)
        arm = next((o for o in objs if o.type == 'ARMATURE'), None)
        mesh = next((o for o in objs if o.type == 'MESH'), None)
        if arm is None or mesh is None:
            fail(f"cast '{name}' blend '{rel}' has no ARMATURE+MESH pair")
        if c["action"] not in acts:
            fail(f"cast '{name}' action '{c['action']}' not found; available actions: {sorted(acts)}")

        # Auto-scale, measured not assumed: rest-pose bbox height of THIS rig, not a shared constant.
        dg = bpy.context.evaluated_depsgraph_get()
        lo, hi = eval_bbox(mesh, dg)
        scale = char_height / (hi - lo)
        arm.scale = (scale, scale, scale)

        freeze(arm, acts[c["action"]], c["frame"])
        arm.location = c["loc"]
        arm.rotation_euler = (0, 0, math.radians(c.get("rot_z_deg", 0)))
        cast_objs[name] = {"arm": arm, "mesh": mesh, "spec": c}
        input_blends.append(rel)

    scene.frame_set(FRAME)  # freeze strips only take effect once the scene is at FRAME
    for obj in cast_objs.values():
        c = obj["spec"]
        if c.get("floor_snap", True):
            dg = bpy.context.evaluated_depsgraph_get()
            lo, hi = eval_bbox(obj["mesh"], dg)
            obj["arm"].location.z -= lo
        recolor(obj["mesh"], c.get("recolor", []))
    return cast_objs, input_blends


def build_lights(spec, scene):
    lights = spec.get("lights", {})
    if "sun" in lights:
        s = lights["sun"]
        data = bpy.data.lights.new("sun", 'SUN')
        data.energy = s.get("energy", 3.0)
        data.angle = math.radians(s.get("angle_deg", 20))
        obj = bpy.data.objects.new("sun", data)
        obj.rotation_euler = [math.radians(d) for d in s.get("rot_deg", [45, 0, 30])]
        scene.collection.objects.link(obj)
    if "fill" in lights:
        f = lights["fill"]
        data = bpy.data.lights.new("fill", 'AREA')
        data.energy = f.get("energy", 250)
        data.size = f.get("size", 6)
        obj = bpy.data.objects.new("fill", data)
        obj.location = f.get("loc", [-3, -4, 3.5])
        obj.rotation_euler = [math.radians(d) for d in f.get("rot_deg", [55, 0, -30])]
        scene.collection.objects.link(obj)


def build_cameras(spec, cast_objs, scene):
    cams = {}
    for cdef in spec.get("cameras", []):
        name = cdef["name"]
        if "target_bone" in cdef:
            tb = cdef["target_bone"]
            arm = cast_objs[tb["cast"]]["arm"]
            pos = bone_world(arm, tb["bone"])
            if pos is None:
                bone_names = [pb.name for pb in arm.pose.bones]
                fail(f"camera '{name}' target_bone '{tb['bone']}' matches no bone on '{tb['cast']}'; available bones: {bone_names}")
            off = tb.get("offset", [0, 0, 0])
            tgt_loc = (pos.x + off[0], pos.y + off[1], pos.z + off[2])
        else:
            tgt_loc = cdef["target"]

        # Cameras aim via TRACK_TO at an empty — never hand-set camera rotation.
        empty = bpy.data.objects.new(f"target_{name}", None)
        empty.location = tgt_loc
        scene.collection.objects.link(empty)

        cam_data = bpy.data.cameras.new(name)
        cam_data.lens = cdef.get("lens", 50)
        cam_obj = bpy.data.objects.new(name, cam_data)
        cam_obj.location = cdef["loc"]
        tr = cam_obj.constraints.new('TRACK_TO')
        tr.target = empty
        tr.track_axis = 'TRACK_NEGATIVE_Z'
        tr.up_axis = 'UP_Y'
        scene.collection.objects.link(cam_obj)
        cams[name] = cam_obj
    return cams


def render_cameras(spec, cams, base, quality, only):
    r = spec.get("render", {})
    outdir_rel = r.get("outdir", "shots")
    prefix = r.get("prefix", "panel")
    outdir = os.path.join(base, outdir_rel)
    os.makedirs(outdir, exist_ok=True)

    res = r.get("resolution", [1600, 900])
    samples_map = r.get("samples", {"draft": 24, "final": 128})
    draft_scale = r.get("draft_scale", 0.5) if quality == "draft" else 1.0

    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    scene.cycles.device = 'CPU'
    scene.cycles.use_denoising = True
    scene.cycles.samples = samples_map.get(quality, 24 if quality == "draft" else 128)
    scene.render.resolution_x = int(res[0] * draft_scale)
    scene.render.resolution_y = int(res[1] * draft_scale)

    cam_names = [only] if only is not None else list(cams.keys())

    frames = {}
    for name in cam_names:
        scene.camera = cams[name]
        out_name = f"{prefix}-{name}.png"
        scene.render.filepath = os.path.join(outdir, out_name)
        bpy.ops.render.render(write_still=True)
        print(f"RENDERED {name}")
        frames[f"{prefix}-{name}"] = {"file": out_name, "px": [scene.render.resolution_x, scene.render.resolution_y]}
    print(f"DONE {len(frames)} frames -> {outdir_rel}")
    return outdir, frames


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_existing_manifest(outdir):
    path = os.path.join(outdir, "manifest.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def write_manifest(spec_path, base, input_rel_paths, quality, frames, outdir, only):
    # Partial (--only) runs merge into whatever manifest is already there, so a mixed
    # draft/final panel set stays honest per-frame; full runs rewrite the whole thing.
    frames_out = {}
    if only is not None:
        existing = load_existing_manifest(outdir)
        if existing:
            frames_out.update(existing.get("frames", {}))
    for key, val in frames.items():
        frames_out[key] = {**val, "quality": quality}

    manifest = {
        "generated_by": "render_scene.py v1",
        "blender": bpy.app.version_string,
        "spec_sha256": sha256_file(spec_path),
        "spec_path": spec_path,
        "inputs": {rel: sha256_file(os.path.join(base, rel)) for rel in sorted(set(input_rel_paths))},
        "frames": frames_out,
    }
    with open(os.path.join(outdir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)


def main():
    spec_path, quality, only = parse_args()
    spec = load_spec(spec_path)
    base = os.path.dirname(os.path.abspath(spec_path))
    validate_spec(spec, base)

    declared_cams = [c["name"] for c in spec["cameras"]]
    if only is not None and only not in declared_cams:
        fail(f"--only '{only}' is not a known camera; available cameras: {sorted(declared_cams)}")

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene

    materials = build_materials(spec)
    build_world(spec, scene)
    build_boxes(spec, materials, scene)
    input_blends = build_props(spec, base, scene)
    cast_objs, cast_blends = build_cast(spec, base, scene, spec.get("units", {}))
    input_blends += cast_blends
    build_lights(spec, scene)
    cams = build_cameras(spec, cast_objs, scene)

    outdir, frames = render_cameras(spec, cams, base, quality, only)
    write_manifest(spec_path, base, input_blends, quality, frames, outdir, only)


if __name__ == "__main__":
    main()
