#!/usr/bin/env python3
"""Compose a render spec from the cast bible: set fragment + named characters + named poses.

  python3 compose.py --set classroom \
      --place omar:presenting@1.75,2.35,0:-35 \
      --place sara:seated@1.1,-1.30,0.02:184 \
      -o shot.json
  blender -b --python ../scripts/render_scene.py -- shot.json --quality draft

A placement is  <character>:<pose>@x,y,z[:rot_z_deg]  — character from cast.json, pose from
poses.json (falling back to the character's pack prefix), position in metres. The output spec
is plain render_scene.py input; edit it further by hand if the shot needs it.
"""
import argparse, copy, json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name):
    with open(HERE / name, encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", required=True, dest="set_name", help="a fragment in sets/ (e.g. classroom)")
    ap.add_argument("--place", action="append", default=[], metavar="CHAR:POSE@X,Y,Z[:ROT]",
                    help="place a cast member; repeatable")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    available = sorted(p.stem for p in (HERE / "sets").glob("*.json"))
    if args.set_name not in available:  # allowlist, so '../x' traversal can't name a set
        sys.exit(f"unknown set '{args.set_name}' — available: " + ", ".join(available))
    set_path = HERE / "sets" / f"{args.set_name}.json"
    spec = json.loads(set_path.read_text(encoding="utf-8"))
    cast_book = load("cast.json")
    pose_book = load("poses.json")

    # render_scene.py resolves blend paths relative to the SPEC file, and the composed spec can
    # be written anywhere — so every pack path is absolutized against the cast directory here.
    for p in spec.get("props", []):
        if "blend" in p and not Path(p["blend"]).is_absolute():
            p["blend"] = str((HERE / p["blend"]).resolve())

    spec.setdefault("cast", [])
    for raw in args.place:
        try:
            who_pose, at = raw.split("@", 1)
            who, pose = who_pose.split(":", 1)
            parts = at.split(":")
            if len(parts) > 2:
                raise ValueError("trailing fields after rotation")
            x, y, z = (float(v) for v in parts[0].split(","))
            rot = float(parts[1]) if len(parts) > 1 else 0.0
        except ValueError:
            sys.exit(f"bad --place '{raw}' — expected CHAR:POSE@X,Y,Z[:ROT]")

        if who not in cast_book["characters"]:
            sys.exit(f"unknown character '{who}' — cast: {', '.join(sorted(cast_book['characters']))}")
        member = copy.deepcopy(cast_book["characters"][who])

        prefix = member.pop("action_prefix")
        poses = pose_book["poses"]
        if pose not in poses:
            sys.exit(f"unknown pose '{pose}' — poses: {', '.join(sorted(poses))}")
        entry = {
            "name": who,
            "blend": str((HERE / member.pop("blend")).resolve()),
            "action": f"{prefix}_{poses[pose]['action']}",
            "frame": poses[pose]["frame"],
            "loc": [x, y, z],
            "rot_z_deg": rot,
        }
        if not poses[pose].get("floor_snap", True):
            entry["floor_snap"] = False
        if "recolor" in member:
            entry["recolor"] = member.pop("recolor")
        member.pop("bio", None)
        entry.update(member)  # any remaining per-character overrides
        spec["cast"].append(entry)

    Path(args.out).write_text(json.dumps(spec, indent=2), encoding="utf-8")
    print(f"wrote {args.out}: set={args.set_name} cast={[c['name'] for c in spec['cast']]}")


if __name__ == "__main__":
    main()
